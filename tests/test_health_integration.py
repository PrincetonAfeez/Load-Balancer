""" Test the health integration """

from __future__ import annotations

import asyncio
import unittest

from load_balancer.config_parser import BackendConfig, HealthConfig
from load_balancer.health import HealthChecker
from load_balancer.metrics import Metrics
from load_balancer.pool import BackendPool, BackendState


class HealthIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_checker_marks_down_and_recovers_with_hysteresis(self):
        async def accept(reader, writer):
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(accept, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        pool = BackendPool.from_configs(
            [BackendConfig("backend", "127.0.0.1", port)]
        )
        checker = HealthChecker(
            pool,
            HealthConfig(
                interval_seconds=0.02,
                timeout_seconds=0.02,
                failures_to_unhealthy=2,
                successes_to_healthy=2,
            ),
            Metrics(100),
        )
        task = asyncio.create_task(checker.run())
        try:
            await asyncio.sleep(0.06)
            server.close()
            await server.wait_closed()
            for _ in range(100):
                if pool.require("backend").state is BackendState.UNHEALTHY:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(
                pool.require("backend").state, BackendState.UNHEALTHY
            )

            server = await asyncio.start_server(accept, "127.0.0.1", port)
            for _ in range(100):
                if pool.require("backend").state is BackendState.HEALTHY:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(pool.require("backend").state, BackendState.HEALTHY)
        finally:
            checker.stop()
            await task
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()

