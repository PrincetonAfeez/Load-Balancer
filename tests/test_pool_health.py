""" Test the pool and health modules """

from __future__ import annotations

import unittest

from load_balancer.config_parser import BackendConfig
from load_balancer.pool import Backend, BackendPool, BackendState


class PoolAndHealthTests(unittest.TestCase):
    def test_connection_accounting_is_idempotent(self):
        backend = Backend("one", "127.0.0.1", 9001)
        backend.connection_opened("c1")
        backend.connection_opened("c1")
        self.assertEqual(backend.active_connections, 1)
        self.assertEqual(backend.total_connections, 1)
        backend.connection_closed("c1")
        backend.connection_closed("c1")
        self.assertEqual(backend.active_connections, 0)

    def test_active_health_hysteresis(self):
        backend = Backend("one", "127.0.0.1", 9001)
        self.assertIsNone(backend.record_active_check(False, 3, 2))
        self.assertIsNone(backend.record_active_check(False, 3, 2))
        transition = backend.record_active_check(False, 3, 2)
        self.assertEqual(transition, (BackendState.HEALTHY, BackendState.UNHEALTHY))
        self.assertIsNone(backend.record_active_check(True, 3, 2))
        transition = backend.record_active_check(True, 3, 2)
        self.assertEqual(transition, (BackendState.UNHEALTHY, BackendState.HEALTHY))

    def test_passive_failure_suspicion(self):
        backend = Backend("one", "127.0.0.1", 9001)
        self.assertIsNone(backend.record_passive_failure(3))
        self.assertEqual(backend.state, BackendState.HEALTHY)
        self.assertIsNone(backend.record_passive_failure(3))
        transition = backend.record_passive_failure(3)
        self.assertEqual(transition, (BackendState.HEALTHY, BackendState.UNHEALTHY))

    def test_reload_drains_inflight_removed_backend(self):
        pool = BackendPool.from_configs(
            [
                BackendConfig("one", "127.0.0.1", 9001),
                BackendConfig("two", "127.0.0.1", 9002),
            ]
        )
        one = pool.require("one")
        one.connection_opened("c1")
        changes = pool.apply_configs(
            [BackendConfig("two", "127.0.0.1", 9002, 3)]
        )
        self.assertIn("one", changes["draining"])
        self.assertEqual(one.state, BackendState.DRAINING)
        self.assertIs(pool.get("one"), one)
        one.connection_closed("c1")
        pool.prune_retired()
        self.assertIsNone(pool.get("one"))


if __name__ == "__main__":
    unittest.main()

