"""TCPProxy unit tests."""

from __future__ import annotations

import asyncio
import unittest

from load_balancer.config_parser import (
    AppConfig,
    BackendConfig,
    BalancerConfig,
    ListenerConfig,
    RuleConfig,
    TimeoutConfig,
)
from load_balancer.metrics import Metrics
from load_balancer.pool import BackendPool
from load_balancer.proxy import TCPProxy


class ProxyUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.backend_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.backend_port = int(self.backend_server.sockets[0].getsockname()[1])
        self.config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 0),
            balancer=BalancerConfig(strategy="round_robin", max_connections=10),
            timeouts=TimeoutConfig(connect_seconds=1, idle_seconds=0.2),
            backends=(
                BackendConfig("echo", "127.0.0.1", self.backend_port),
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

    async def test_set_strategy_and_update_config(self):
        self.proxy.set_strategy("least_connections")
        self.assertEqual(self.proxy.strategy_name, "least_connections")
        new_config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 0),
            balancer=BalancerConfig(
                strategy="weighted_round_robin",
                virtual_nodes_per_weight=128,
            ),
            backends=self.config.backends,
        )
        self.proxy.update_config(new_config)
        self.assertEqual(self.proxy.strategy_name, "weighted_round_robin")

    async def test_rule_override_strategy(self):
        rule_config = RuleConfig(
            source='if client.ip == "127.0.0.1" then return "least_connections"'
        )
        self.proxy.update_config(
            AppConfig(
                listener=ListenerConfig("127.0.0.1", 0),
                balancer=BalancerConfig(strategy="round_robin"),
                timeouts=TimeoutConfig(connect_seconds=1, idle_seconds=2),
                backends=self.config.backends,
                rules=rule_config,
            )
        )
        host, port = self.proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"hi")
        await writer.drain()
        await reader.readexactly(2)
        writer.close()
        await writer.wait_closed()

    async def test_max_connections_rejects_second_client(self):
        self.proxy.update_config(
            AppConfig(
                listener=ListenerConfig("127.0.0.1", 0),
                balancer=BalancerConfig(strategy="round_robin", max_connections=1),
                timeouts=TimeoutConfig(connect_seconds=1, idle_seconds=2),
                backends=self.config.backends,
            )
        )
        host, port = self.proxy.address
        _, w1 = await asyncio.open_connection(host, port)
        w1.write(b"hold")
        await w1.drain()
        _, w2 = await asyncio.open_connection(host, port)
        w2.close()
        await w2.wait_closed()
        await asyncio.sleep(0.15)
        self.assertGreaterEqual(
            self.metrics._counters.get("connection_rejected", 0), 1
        )
        w1.close()
        await w1.wait_closed()

    async def test_idle_timeout_closes_connection(self):
        host, port = self.proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"x")
        await writer.drain()
        await reader.readexactly(1)
        data = await asyncio.wait_for(reader.read(10), timeout=2)
        self.assertEqual(data, b"")
        writer.close()
        await writer.wait_closed()

    def test_set_strategy_invalid_raises(self):
        with self.assertRaises(ValueError):
            self.proxy.set_strategy("not-a-strategy")

    def test_connection_slots_property(self):
        self.assertEqual(self.proxy.connection_slots_in_use, 0)


if __name__ == "__main__":
    unittest.main()
