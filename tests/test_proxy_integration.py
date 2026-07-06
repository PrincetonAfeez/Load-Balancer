""" Test the proxy integration """

from __future__ import annotations

import asyncio
import unittest

from load_balancer.config_parser import (
    AppConfig,
    BackendConfig,
    BalancerConfig,
    ControlConfig,
    HealthConfig,
    ListenerConfig,
    TimeoutConfig,
)
from load_balancer.metrics import Metrics
from load_balancer.pool import BackendPool
from load_balancer.proxy import TCPProxy


class ProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend_connections = 0

        async def echo(reader, writer):
            self.backend_connections += 1
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.backend_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        backend_port = self.backend_server.sockets[0].getsockname()[1]
        self.config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 0),
            control=ControlConfig("127.0.0.1", 0),
            balancer=BalancerConfig(
                strategy="round_robin", drain_timeout_seconds=1
            ),
            health=HealthConfig(),
            timeouts=TimeoutConfig(connect_seconds=1, idle_seconds=2),
            backends=(
                BackendConfig("echo", "127.0.0.1", backend_port),
            ),
        )
        self.pool = BackendPool.from_configs(self.config.backends)
        self.metrics = Metrics(100)
        self.proxy = TCPProxy(self.config, self.pool, self.metrics)
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.drain(1)
        self.backend_server.close()
        await self.backend_server.wait_closed()

    async def test_echo_end_to_end_and_accounting(self):
        host, port = self.proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"hello through proxy")
        await writer.drain()
        response = await reader.readexactly(len(b"hello through proxy"))
        self.assertEqual(response, b"hello through proxy")
        writer.close()
        await writer.wait_closed()
        for _ in range(20):
            if self.metrics.active_connections == 0:
                break
            await asyncio.sleep(0.01)
        backend = self.pool.require("echo")
        self.assertEqual(backend.active_connections, 0)
        self.assertEqual(backend.total_connections, 1)
        self.assertEqual(backend.bytes_in, len(response))
        self.assertEqual(backend.bytes_out, len(response))

    async def test_no_backend_does_not_crash_listener(self):
        self.pool.disable("echo")
        host, port = self.proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        self.assertEqual(await reader.read(), b"")
        writer.close()
        await writer.wait_closed()
        self.assertEqual(self.metrics.no_eligible_backend, 1)
        self.assertEqual(self.metrics.no_backend_available, 1)
        self.pool.enable("echo")
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"still alive")
        await writer.drain()
        self.assertEqual(await reader.readexactly(11), b"still alive")
        writer.close()
        await writer.wait_closed()

    async def test_many_simultaneous_clients_clean_up_counts(self):
        host, port = self.proxy.address

        async def one(index):
            payload = f"message-{index}".encode()
            reader, writer = await asyncio.open_connection(host, port)
            try:
                writer.write(payload)
                await writer.drain()
                return await reader.readexactly(len(payload))
            finally:
                writer.close()
                await writer.wait_closed()

        responses = await asyncio.gather(*(one(index) for index in range(100)))
        self.assertEqual(responses[42], b"message-42")
        for _ in range(50):
            if self.metrics.active_connections == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.metrics.active_connections, 0)
        self.assertEqual(self.pool.require("echo").active_connections, 0)
        self.assertEqual(self.pool.require("echo").total_connections, 100)

    async def test_force_close_backend_cleans_connection(self):
        host, port = self.proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        for _ in range(50):
            if self.metrics.active_connections == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.metrics.active_connections, 1)
        self.assertEqual(self.proxy.force_close_backend("echo"), 1)
        self.assertEqual(await reader.read(), b"")
        for _ in range(50):
            if self.metrics.active_connections == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.metrics.active_connections, 0)
        self.assertEqual(self.pool.require("echo").active_connections, 0)
        writer.close()
        await writer.wait_closed()

    async def test_forced_shutdown_cleans_active_connection(self):
        host, port = self.proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        for _ in range(50):
            if self.metrics.active_connections == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.metrics.active_connections, 1)
        await self.proxy.drain(0.01)
        self.assertEqual(await reader.read(), b"")
        self.assertEqual(self.metrics.active_connections, 0)
        self.assertEqual(self.pool.require("echo").active_connections, 0)
        writer.close()
        await writer.wait_closed()

    async def test_shutdown_reject_emits_connection_rejected(self):
        self.proxy._closing = True
        host, port = self.proxy.address
        _, writer = await asyncio.open_connection(host, port)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)
        self.assertGreaterEqual(
            self.metrics._counters.get("connection_rejected", 0), 1
        )


if __name__ == "__main__":
    unittest.main()
