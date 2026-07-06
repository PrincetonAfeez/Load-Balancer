"""Extended backend pool tests."""

from __future__ import annotations

import unittest

from load_balancer.config_parser import BackendConfig
from load_balancer.pool import Backend, BackendPool, BackendState


class PoolExtendedTests(unittest.TestCase):
    def test_duplicate_id_on_construct(self):
        backends = [
            Backend("a", "127.0.0.1", 9001),
            Backend("a", "127.0.0.1", 9002),
        ]
        with self.assertRaises(ValueError):
            BackendPool(backends)

    def test_enable_disable_drain_remove(self):
        pool = BackendPool.from_configs([BackendConfig("one", "127.0.0.1", 9001)])
        pool.disable("one")
        self.assertEqual(pool.require("one").state, BackendState.DISABLED)
        pool.enable("one")
        self.assertEqual(pool.require("one").state, BackendState.HEALTHY)
        pool.drain("one")
        self.assertEqual(pool.require("one").state, BackendState.DRAINING)
        removed = pool.remove("one")
        self.assertTrue(removed.retired)

    def test_update_config_preserves_draining(self):
        backend = Backend("one", "127.0.0.1", 9001, state=BackendState.DRAINING)
        BackendPool([backend])
        backend.update_config(BackendConfig("one", "127.0.0.1", 9001, weight=5))
        self.assertEqual(backend.state, BackendState.DRAINING)
        self.assertEqual(backend.weight, 5)

    def test_update_config_disabled_from_file(self):
        backend = Backend("one", "127.0.0.1", 9001)
        backend.update_config(
            BackendConfig("one", "127.0.0.1", 9001, enabled=False)
        )
        self.assertEqual(backend.state, BackendState.DISABLED)

    def test_pending_connection_tracking(self):
        backend = Backend("one", "127.0.0.1", 9001)
        backend.begin_connection_attempt()
        self.assertEqual(backend.load_connections, 1)
        backend.end_connection_attempt()
        self.assertEqual(backend.load_connections, 0)

    def test_eligible_excludes_retired(self):
        pool = BackendPool.from_configs([BackendConfig("one", "127.0.0.1", 9001)])
        pool.drain("one", retired=True)
        self.assertEqual(pool.eligible(), [])

    def test_status_snapshot(self):
        backend = Backend("one", "127.0.0.1", 9001)
        status = backend.status()
        self.assertEqual(status["id"], "one")
        self.assertIn("tags", status)

    def test_require_missing_raises(self):
        pool = BackendPool.from_configs([BackendConfig("one", "127.0.0.1", 9001)])
        with self.assertRaises(KeyError):
            pool.require("missing")


if __name__ == "__main__":
    unittest.main()
