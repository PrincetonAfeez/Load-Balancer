"""Length-prefixed JSON admin protocol and HMAC-authenticated control server."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import json
import logging
import secrets
import struct
import time
from typing import Any

from .crypto import (
    ReplayCache,
    ResponseNonceCache,
    authenticate_payload,
    sign_payload,
    sign_response,
    verify_response,
)
from .errors import AdminProtocolError
from .metrics import Metrics

LOG = logging.getLogger(__name__)

# Admin frames are tiny; a client that opens a connection and stalls should not
# hold a handler task open indefinitely.
CLIENT_READ_TIMEOUT = 10.0

SUPPORTED_COMMANDS = {
    "status",
    "stop",
    "reload",
    "backends.list",
    "backends.add",
    "backends.remove",
    "backends.enable",
    "backends.disable",
    "backends.drain",
    "strategy.get",
    "strategy.set",
}


def encode_frame(payload: Mapping[str, Any], max_size: int = 65_536) -> bytes:
    try:
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdminProtocolError("payload is not JSON serializable") from exc
    if len(body) > max_size:
        raise AdminProtocolError("admin frame is too large")
    return struct.pack(">I", len(body)) + body


async def read_frame(
    reader: asyncio.StreamReader, max_size: int = 65_536
) -> dict[str, Any]:
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as exc:
        raise AdminProtocolError("incomplete admin frame header") from exc
    (length,) = struct.unpack(">I", header)
    if length <= 0:
        raise AdminProtocolError("admin frame cannot be empty")
    if length > max_size:
        raise AdminProtocolError("admin frame is too large")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise AdminProtocolError("incomplete admin frame body") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdminProtocolError("admin frame contains malformed JSON") from exc
    if not isinstance(payload, dict):
        raise AdminProtocolError("admin payload must be a JSON object")
    return payload


def validate_command_shape(payload: Mapping[str, Any]) -> None:
    command = payload.get("command")
    args = payload.get("args")
    if not isinstance(command, str) or command not in SUPPORTED_COMMANDS:
        raise AdminProtocolError(f"unknown admin command: {command!r}")
    if not isinstance(args, dict):
        raise AdminProtocolError("admin args must be an object")
    no_arg_commands = {
        "status",
        "stop",
        "reload",
        "backends.list",
        "strategy.get",
    }
    backend_id_commands = {
        "backends.remove",
        "backends.enable",
        "backends.disable",
        "backends.drain",
    }
    if command in no_arg_commands and args:
        raise AdminProtocolError(f"{command} does not accept arguments")
    if command in backend_id_commands:
        if set(args) != {"backend_id"} or not isinstance(
            args.get("backend_id"), str
        ):
            raise AdminProtocolError(
                f"{command} requires one string backend_id argument"
            )
    if command == "strategy.set":
        if set(args) != {"name"} or not isinstance(args.get("name"), str):
            raise AdminProtocolError(
                "strategy.set requires one string name argument"
            )
    if command == "backends.add":
        required = {"name", "host", "port"}
        allowed = required | {"weight", "tags"}
        if not required.issubset(args) or not set(args).issubset(allowed):
            raise AdminProtocolError(
                "backends.add requires name, host, and port"
            )
        if (
            not isinstance(args["name"], str)
            or not isinstance(args["host"], str)
            or not isinstance(args["port"], int)
            or isinstance(args["port"], bool)
            or (
                "weight" in args
                and (
                    not isinstance(args["weight"], int)
                    or isinstance(args["weight"], bool)
                )
            )
            or (
                "tags" in args
                and (
                    not isinstance(args["tags"], list)
                    or not all(isinstance(tag, str) for tag in args["tags"])
                )
            )
        ):
            raise AdminProtocolError("backends.add contains invalid argument types")


CommandHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ControlServer:
    def __init__(
        self,
        host: str,
        port: int,
        secret: str,
        max_frame_bytes: int,
        max_clock_skew_seconds: int,
        handler: CommandHandler,
        metrics: Metrics,
    ) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.max_frame_bytes = max_frame_bytes
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self.handler = handler
        self.metrics = metrics
        self.replay_cache = ReplayCache(max_clock_skew_seconds * 2)
        self.server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        LOG.info("control socket listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        clients = set(self._clients)
        if clients:
            # Let in-flight commands finish first (notably the `stop` that
            # triggered this shutdown, which still needs to send its reply),
            # then cancel any connection still stalled after the grace window.
            _, pending = await asyncio.wait(clients, timeout=1.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._clients.clear()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        peer = writer.get_extra_info("peername")
        try:
            try:
                try:
                    payload = await asyncio.wait_for(
                        read_frame(reader, self.max_frame_bytes),
                        timeout=CLIENT_READ_TIMEOUT,
                    )
                except TimeoutError as exc:
                    raise AdminProtocolError(
                        "admin client timed out before sending a complete frame"
                    ) from exc
                authenticate_payload(
                    payload,
                    self.secret,
                    self.replay_cache,
                    self.max_clock_skew_seconds,
                )
                validate_command_shape(payload)
                command = payload["command"]
                response_data = await self.handler(command, payload["args"])
                response = {"ok": True, "data": response_data}
                self.metrics.emit(
                    "admin_accepted", {"command": command, "peer": str(peer)}
                )
                LOG.debug("admin command accepted from %s: %s", peer, command)
            except Exception as exc:
                LOG.warning("admin command rejected from %s: %s", peer, exc)
                self.metrics.emit(
                    "admin_rejected",
                    {"peer": str(peer), "error": str(exc)},
                    critical=True,
                )
                response = {"ok": False, "error": str(exc)}
            await self._send_response(writer, response)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            if task is not None:
                self._clients.discard(task)

    def _sign_response_body(self, response: dict[str, Any]) -> dict[str, Any]:
        signed: dict[str, Any] = {
            "version": 1,
            "timestamp": int(time.time()),
            "nonce": secrets.token_urlsafe(16),
            "ok": response["ok"],
        }
        if response["ok"]:
            signed["data"] = response.get("data", {})
        else:
            signed["error"] = str(response.get("error", "admin command failed"))
        signed["signature"] = sign_response(signed, self.secret)
        return signed

    async def _send_response(
        self, writer: asyncio.StreamWriter, response: dict[str, Any]
    ) -> None:
        try:
            frame = encode_frame(
                self._sign_response_body(response), self.max_frame_bytes
            )
        except AdminProtocolError:
            # The response (e.g. a status with very many connections) does not
            # fit in a frame. Send a structured error rather than silently
            # closing, which would surface to the client as a corrupt frame.
            LOG.warning("admin response too large to frame; sending error instead")
            try:
                frame = encode_frame(
                    self._sign_response_body(
                        {
                            "ok": False,
                            "error": "response exceeds max_frame_bytes; "
                            "raise control.max_frame_bytes or narrow the query",
                        }
                    ),
                    self.max_frame_bytes,
                )
            except AdminProtocolError:
                return
        try:
            writer.write(frame)
            await writer.drain()
        except (ConnectionError, OSError):
            pass


class ControlClient:
    def __init__(
        self,
        host: str,
        port: int,
        secret: str,
        max_frame_bytes: int = 65_536,
        timeout: float = 5.0,
        max_clock_skew_seconds: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.max_frame_bytes = max_frame_bytes
        self.timeout = timeout
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self._response_cache = ResponseNonceCache(max_clock_skew_seconds * 2)

    async def command(
        self, command: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": 1,
            "timestamp": int(time.time()),
            "nonce": secrets.token_urlsafe(24),
            "command": command,
            "args": args or {},
        }
        payload["signature"] = sign_payload(payload, self.secret)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        try:
            writer.write(encode_frame(payload, self.max_frame_bytes))
            await writer.drain()
            response = await asyncio.wait_for(
                read_frame(reader, self.max_frame_bytes), timeout=self.timeout
            )
        finally:
            writer.close()
            await writer.wait_closed()
        verify_response(
            response,
            self.secret,
            self.max_clock_skew_seconds,
            self._response_cache,
        )
        if not response.get("ok"):
            raise AdminProtocolError(str(response.get("error", "admin command failed")))
        data = response.get("data")
        if not isinstance(data, dict):
            raise AdminProtocolError("admin response data must be an object")
        return data
