"""Failure-injection tests drawn from the project's own test plan (scope §14)."""

from __future__ import annotations

import asyncio
import sqlite3
import unittest

from load_balancer.config_parser import (
    AppConfig,
    BackendConfig,
    BalancerConfig,
    ControlConfig,
    ListenerConfig,
    TimeoutConfig,
)
from load_balancer.demo_tools import run_dummy_backend
from load_balancer.metrics import MetricEvent, Metrics
from load_balancer.pool import BackendPool
from load_balancer.proxy import TCPProxy
from load_balancer.store import MetricsWriter


def drain_events(metrics: Metrics) -> list[MetricEvent]:
    events: list[MetricEvent] = []
    for queue in (metrics.critical_queue, metrics.queue):
        while True:
            try:
                events.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
    return events


async def unused_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    server.close()
    await server.wait_closed()
    return port


class ProxyFailureInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def _make_proxy(self, handlers, **timeouts):
        servers = []
        ports = []
        for handler in handlers:
            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            servers.append(server)
            ports.append(int(server.sockets[0].getsockname()[1]))
        config = AppConfig(
            listener=ListenerConfig("127.0.0.1", 0),
            control=ControlConfig("127.0.0.1", 0),
            balancer=BalancerConfig(strategy="round_robin", drain_timeout_seconds=1),
            timeouts=TimeoutConfig(
                **{"connect_seconds": 1.0, "idle_seconds": 2.0, **timeouts}
            ),
            backends=tuple(
                BackendConfig(f"b{i}", "127.0.0.1", port)
                for i, port in enumerate(ports, start=1)
            ),
        )
        pool = BackendPool.from_configs(config.backends)
        metrics = Metrics(1000)
        proxy = TCPProxy(config, pool, metrics)
        await proxy.start()
        self.addAsyncCleanup(self._teardown, proxy, servers)
        return proxy, pool, metrics

    async def _teardown(self, proxy, servers):
        await proxy.drain(1)
        for server in servers:
            server.close()
            await server.wait_closed()

    async def _wait_zero(self, metrics):
        for _ in range(200):
            if metrics.active_connections == 0:
                return
            await asyncio.sleep(0.01)

    async def test_backend_accepts_then_hangs_hits_idle_timeout(self):
        async def hang(reader, writer):
            try:
                await reader.read()  # never echoes
            finally:
                writer.close()
                await writer.wait_closed()

        proxy, _pool, metrics = await self._make_proxy([hang], idle_seconds=0.3)
        host, port = proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        # No traffic in either direction: the idle watcher must close it.
        self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2), b"")
        writer.close()
        await writer.wait_closed()
        await self._wait_zero(metrics)
        self.assertEqual(metrics.active_connections, 0)
        reasons = [
            event.data.get("reason")
            for event in drain_events(metrics)
            if event.kind == "connection_closed"
        ]
        self.assertIn("timeout", reasons)

    async def test_backend_closes_immediately(self):
        async def close_now(reader, writer):
            writer.close()
            await writer.wait_closed()

        proxy, pool, metrics = await self._make_proxy([close_now])
        host, port = proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2), b"")
        writer.close()
        await writer.wait_closed()
        await self._wait_zero(metrics)
        self.assertEqual(metrics.active_connections, 0)
        # The backend connection did succeed, so it was counted exactly once.
        self.assertEqual(pool.require("b1").total_connections, 1)
        self.assertEqual(pool.require("b1").active_connections, 0)

    async def test_backend_dies_during_active_relay(self):
        async def echo_once_then_die(reader, writer):
            data = await reader.read(65536)
            if data:
                writer.write(data)
                await writer.drain()
            writer.close()  # disappear mid-conversation
            await writer.wait_closed()

        proxy, pool, metrics = await self._make_proxy([echo_once_then_die])
        host, port = proxy.address
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"ping")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(4), timeout=2), b"ping")
        self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2), b"")
        writer.close()
        await writer.wait_closed()
        await self._wait_zero(metrics)
        self.assertEqual(metrics.active_connections, 0)
        self.assertEqual(pool.require("b1").active_connections, 0)


class MetricsDBPressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_survives_db_errors_and_counts_drops(self):
        class LockedStore:
            def write_events(self, events):
                raise sqlite3.OperationalError("database is locked")

            def write_metrics_snapshot(self, data):
                raise sqlite3.OperationalError("database is locked")

        metrics = Metrics(1000)
        pool = BackendPool.from_configs([BackendConfig("b", "127.0.0.1", 9001)])
        writer = MetricsWriter(
            LockedStore(),
            metrics,
            pool,
            flush_interval=0.01,
            snapshot_interval=100.0,  # don't fire snapshots during the test
            batch_size=10,
        )
        for index in range(10):
            metrics.emit("connection_closed", {"index": index})
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.05)
        writer.stop()
        await asyncio.wait_for(task, timeout=3)
        # No crash; the un-writable events are dropped and counted.
        self.assertGreater(metrics.dropped_events, 0)


class DummyBackendModeTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self, mode, **kwargs):
        port = await unused_port()
        task = asyncio.create_task(
            run_dummy_backend(mode, "127.0.0.1", port, **kwargs)
        )
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.02)
        self.addAsyncCleanup(self._stop, task)
        return port, task

    async def _stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_echo_mode_round_trips(self):
        port, _ = await self._start("echo")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"hello")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5), timeout=2), b"hello")
        writer.close()
        await writer.wait_closed()

    async def test_close_immediately_mode_sends_nothing(self):
        port, _ = await self._start("close-immediately")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # The backend sends no data and drops the connection; observe EOF (or an
        # abrupt reset, which is equally valid for a close-immediately backend).
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2), b"")
        except (ConnectionError, OSError):
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def test_slow_mode_still_echoes(self):
        port, _ = await self._start("slow", delay_ms=20)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"slow")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(4), timeout=2), b"slow")
        writer.close()
        await writer.wait_closed()

    async def test_flaky_mode_zero_fail_rate_echoes(self):
        port, _ = await self._start("flaky", fail_rate=0.0)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"ok")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(2), timeout=2), b"ok")
        writer.close()
        await writer.wait_closed()

    async def test_flaky_mode_full_fail_rate_closes_immediately(self):
        port, _ = await self._start("flaky", fail_rate=1.0)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2), b"")
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
