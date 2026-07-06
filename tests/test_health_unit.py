"""HealthChecker unit tests."""

from __future__ import annotations

import asyncio
import unittest

from load_balancer.config_parser import BackendConfig, HealthConfig
from load_balancer.health import HealthChecker
from load_balancer.metrics import Metrics
from load_balancer.pool import BackendPool, BackendState


class HealthCheckerUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_disabled_and_draining(self):
        async def accept(reader, writer):
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        pool = BackendPool.from_configs(
            [
                BackendConfig("ok", "127.0.0.1", port),
                BackendConfig("disabled", "127.0.0.1", port + 1),
            ]
        )
        pool.disable("disabled")
        pool.drain("ok")
        checker = HealthChecker(
            pool,
            HealthConfig(interval_seconds=0.05, timeout_seconds=0.05),
            Metrics(50),
        )
        task = asyncio.create_task(checker.run())
        await asyncio.sleep(0.15)
        checker.stop()
        await task
        self.assertEqual(pool.require("ok").state, BackendState.DRAINING)
        server.close()
        await server.wait_closed()

    async def test_update_config_and_request_check_wake(self):
        pool = BackendPool.from_configs(
            [BackendConfig("b", "127.0.0.1", 65530)]
        )
        checker = HealthChecker(
            pool,
            HealthConfig(interval_seconds=10, timeout_seconds=0.1),
            Metrics(50),
        )
        checker.update_config(
            HealthConfig(interval_seconds=0.05, timeout_seconds=0.05)
        )
        checker.request_check()
        checker.stop()


if __name__ == "__main__":
    unittest.main()
