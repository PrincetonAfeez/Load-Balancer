"""Repeatable local backends and clients used by demos and integration tests."""

from __future__ import annotations

import asyncio
import logging
import random
import time

LOG = logging.getLogger(__name__)


async def run_dummy_backend(
    mode: str,
    host: str,
    port: int,
    *,
    delay_ms: int = 500,
    fail_rate: float = 0.3,
    name: str | None = None,
) -> None:
    label = name or f"{mode}:{port}"

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        LOG.info("%s accepted %s", label, peer)
        try:
            if mode == "close-immediately":
                return
            if mode == "flaky" and random.random() < fail_rate:
                return
            while True:
                data = await reader.read(64 * 1024)
                if not data:
                    break
                if mode == "slow":
                    await asyncio.sleep(delay_ms / 1000)
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    print(f"dummy backend {label} listening on {addresses}", flush=True)
    async with server:
        await server.serve_forever()


async def send_messages(
    host: str,
    port: int,
    message: str,
    count: int,
    timeout: float = 5.0,
    max_concurrency: int = 50,
) -> list[dict[str, object]]:
    payload = message.encode("utf-8")
    # Bound in-flight connections so a large --count cannot exhaust file
    # descriptors all at once.
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def send_one(index: int) -> dict[str, object]:
        async with semaphore:
            return await _send_one(index)

    async def _send_one(index: int) -> dict[str, object]:
        started = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        try:
            writer.write(payload)
            await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
            complete = True
            try:
                response = await asyncio.wait_for(
                    reader.readexactly(len(payload)), timeout=timeout
                )
            except asyncio.IncompleteReadError as exc:
                # Backend closed after echoing only part of the payload; report
                # what arrived rather than letting the traceback escape.
                response = exc.partial
                complete = False
            return {
                "index": index,
                "response": response.decode("utf-8", errors="replace"),
                "complete": complete,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.gather(*(send_one(index) for index in range(count)))


async def hold_open(host: str, port: int, seconds: float) -> None:
    _, writer = await asyncio.open_connection(host, port)
    print(f"connection open for {seconds:g} seconds", flush=True)
    try:
        await asyncio.sleep(seconds)
    finally:
        writer.close()
        await writer.wait_closed()

