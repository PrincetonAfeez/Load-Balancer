"""Extended strategy tests."""

from __future__ import annotations

import unittest

from load_balancer.config_parser import BackendConfig
from load_balancer.errors import NoHealthyBackendError
from load_balancer.pool import BackendPool, BackendState
from load_balancer.strategies import (
    ConnectionContext,
    ConsistentHashStrategy,
    SmoothWeightedRoundRobinStrategy,
    create_strategy,
)


class StrategyExtendedTests(unittest.TestCase):
    def test_create_strategy_all_names(self):
        for name in (
            "round_robin",
            "weighted_round_robin",
            "least_connections",
            "consistent_hash",
        ):
            strategy = create_strategy(name, 32)
            self.assertEqual(strategy.name, name)

    def test_create_strategy_unknown(self):
        with self.assertRaises(ValueError):
            create_strategy("invalid")

    def test_consistent_hash_invalid_vnodes(self):
        with self.assertRaises(ValueError):
            ConsistentHashStrategy(0)

    def test_consistent_hash_rebuild_on_ineligible_owner(self):
        pool = BackendPool.from_configs(
            [
                BackendConfig("b1", "127.0.0.1", 9001),
                BackendConfig("b2", "127.0.0.1", 9002),
            ]
        )
        strategy = ConsistentHashStrategy(8)
        ctx = ConnectionContext("192.0.2.1", sticky_key="client-1")
        first = strategy.select(pool, ctx)
        pool.require(first.id).state = BackendState.UNHEALTHY
        second = strategy.select(pool, ctx)
        self.assertNotEqual(first.id, second.id)

    def test_wrr_on_success_and_failure_noop(self):
        pool = BackendPool.from_configs(
            [BackendConfig("b1", "127.0.0.1", 9001, weight=2)]
        )
        strategy = SmoothWeightedRoundRobinStrategy()
        backend = pool.require("b1")
        strategy.on_success(backend)
        strategy.on_failure(backend)

    def test_excluded_backends_in_context(self):
        pool = BackendPool.from_configs(
            [
                BackendConfig("b1", "127.0.0.1", 9001),
                BackendConfig("b2", "127.0.0.1", 9002),
            ]
        )
        ctx = ConnectionContext("192.0.2.1")
        ctx.excluded_backend_ids.add("b1")
        strategy = create_strategy("round_robin")
        self.assertEqual(strategy.select(pool, ctx).id, "b2")
        ctx.excluded_backend_ids.add("b2")
        with self.assertRaises(NoHealthyBackendError):
            strategy.select(pool, ctx)


if __name__ == "__main__":
    unittest.main()
