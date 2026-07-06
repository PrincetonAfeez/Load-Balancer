""" Test the crypto and control modules """

from __future__ import annotations

import asyncio
import struct
import time
import unittest

from load_balancer.control import encode_frame, read_frame, validate_command_shape
from load_balancer.crypto import (
    ReplayCache,
    authenticate_payload,
    generate_secret,
    sign_payload,
)
from load_balancer.errors import (
    AdminProtocolError,
    AuthenticationError,
    ReplayError,
)


def payload(secret: str, **overrides):
    value = {
        "version": 1,
        "timestamp": int(time.time()),
        "nonce": "unique-nonce",
        "command": "status",
        "args": {},
    }
    value.update(overrides)
    value["signature"] = sign_payload(value, secret)
    return value


class CryptoTests(unittest.TestCase):
    def test_valid_tampered_expired_and_replayed(self):
        secret = generate_secret()
        cache = ReplayCache(60)
        valid = payload(secret)
        authenticate_payload(valid, secret, cache, 30)
        with self.assertRaises(ReplayError):
            authenticate_payload(valid, secret, cache, 30)

        tampered = payload(secret)
        tampered["command"] = "stop"
        with self.assertRaises(AuthenticationError):
            authenticate_payload(tampered, secret, ReplayCache(), 30)

        expired = payload(secret, timestamp=int(time.time()) - 100)
        with self.assertRaises(AuthenticationError):
            authenticate_payload(expired, secret, ReplayCache(), 30)

    def test_unknown_command(self):
        with self.assertRaises(AdminProtocolError):
            validate_command_shape({"command": "format-disk", "args": {}})

    def test_invalid_command_arguments(self):
        with self.assertRaises(AdminProtocolError):
            validate_command_shape(
                {"command": "backends.drain", "args": {"backend_id": 123}}
            )
        with self.assertRaises(AdminProtocolError):
            validate_command_shape(
                {"command": "status", "args": {"unexpected": True}}
            )


class FrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_round_trip(self):
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame({"ok": True}))
        reader.feed_eof()
        self.assertEqual(await read_frame(reader), {"ok": True})

    async def test_oversized_frame_rejected_before_body(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", 1000))
        with self.assertRaises(AdminProtocolError):
            await read_frame(reader, max_size=100)


if __name__ == "__main__":
    unittest.main()
