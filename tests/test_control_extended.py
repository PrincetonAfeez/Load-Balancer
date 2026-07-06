"""Extended control protocol tests."""

from __future__ import annotations

import asyncio
import struct
import unittest

from load_balancer.control import (
    ControlClient,
    ControlServer,
    encode_frame,
    read_frame,
    validate_command_shape,
)
from load_balancer.crypto import generate_secret
from load_balancer.errors import AdminProtocolError
from load_balancer.metrics import Metrics


class ValidateCommandShapeTests(unittest.TestCase):
    def test_all_supported_commands(self):
        for command in (
            "status",
            "stop",
            "reload",
            "backends.list",
            "strategy.get",
        ):
            validate_command_shape({"command": command, "args": {}})
        validate_command_shape(
            {"command": "backends.drain", "args": {"backend_id": "b1"}}
        )
        validate_command_shape(
            {
                "command": "backends.add",
                "args": {"name": "n", "host": "127.0.0.1", "port": 9001},
            }
        )
        validate_command_shape(
            {"command": "strategy.set", "args": {"name": "round_robin"}}
        )

    def test_backends_add_invalid_types(self):
        with self.assertRaises(AdminProtocolError):
            validate_command_shape(
                {
                    "command": "backends.add",
                    "args": {"name": "n", "host": "h", "port": True},
                }
            )
        with self.assertRaises(AdminProtocolError):
            validate_command_shape(
                {
                    "command": "backends.add",
                    "args": {"name": "n", "host": "h", "port": 1, "tags": [1]},
                }
            )

    def test_encode_frame_errors(self):
        with self.assertRaises(AdminProtocolError):
            encode_frame({"bad": {1, 2, 3}}, max_size=1000)
        huge = {"x": "y" * 2000}
        with self.assertRaises(AdminProtocolError):
            encode_frame(huge, max_size=100)


class ReadFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_and_malformed_frames(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", 0))
        with self.assertRaises(AdminProtocolError):
            await read_frame(reader)
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00\x00\x04{bad")
        with self.assertRaises(AdminProtocolError):
            await read_frame(reader)
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", 3) + b"bad")
        with self.assertRaises(AdminProtocolError):
            await read_frame(reader)

    async def test_incomplete_header_and_body(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00\x00")
        reader.feed_eof()
        with self.assertRaises(AdminProtocolError):
            await read_frame(reader)
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", 10) + b"short")
        reader.feed_eof()
        with self.assertRaises(AdminProtocolError):
            await read_frame(reader)


class ControlServerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_trip_success(self):
        secret = generate_secret()
        metrics = Metrics(100)

        async def handler(command: str, args: dict) -> dict:
            return {"echo": command, "args": args}

        server = ControlServer(
            "127.0.0.1",
            0,
            secret,
            65536,
            30,
            handler,
            metrics,
        )
        await server.start()
        assert server.server is not None
        port = int(server.server.sockets[0].getsockname()[1])
        try:
            client = ControlClient("127.0.0.1", port, secret, timeout=2)
            data = await client.command("status")
            self.assertEqual(data["echo"], "status")
        finally:
            await server.close()

    async def test_handler_error_returns_admin_protocol_error(self):
        secret = generate_secret()
        metrics = Metrics(100)

        async def handler(command: str, args: dict) -> dict:
            raise ValueError("simulated failure")

        server = ControlServer(
            "127.0.0.1",
            0,
            secret,
            65536,
            30,
            handler,
            metrics,
        )
        await server.start()
        assert server.server is not None
        port = int(server.server.sockets[0].getsockname()[1])
        try:
            client = ControlClient("127.0.0.1", port, secret, timeout=2)
            with self.assertRaises(AdminProtocolError):
                await client.command("status")
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
