"""Tests for demo backends and clients."""

from __future__ import annotations

import asyncio
import unittest

from load_balancer.demo_tools import hold_open, send_messages


class DemoToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.server = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_send_messages_round_trip(self):
        results = await send_messages("127.0.0.1", self.port, "ping", 3)
        self.assertEqual(len(results), 3)
        for result in results:
            self.assertEqual(result["response"], "ping")
            self.assertTrue(result["complete"])

    async def test_send_messages_partial_read(self):
        async def short(reader, writer):
            data = await reader.read(10)
            writer.write(data[:3])
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(short, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        try:
            results = await send_messages("127.0.0.1", port, "hello", 1)
            self.assertFalse(results[0]["complete"])
            self.assertEqual(results[0]["response"], "hel")
        finally:
            server.close()
            await server.wait_closed()

    async def test_hold_open(self):
        task = asyncio.create_task(
            hold_open("127.0.0.1", self.port, 0.05)
        )
        await task


if __name__ == "__main__":
    unittest.main()
