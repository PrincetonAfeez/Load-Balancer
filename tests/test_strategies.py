""" Test the strategies module """

from __future__ import annotations

from collections import Counter
import unittest

from load_balancer.config_parser import BackendConfig
from load_balancer.errors import NoHealthyBackendError
from load_balancer.pool import BackendPool, BackendState
from load_balancer.strategies import (
    ConnectionContext,
    ConsistentHashStrategy,
    LeastConnectionsStrategy,
    RoundRobinStrategy,
    SmoothWeightedRoundRobinStrategy,
)


def pool_with(*weights: int) -> BackendPool:
    return BackendPool.from_configs(
        BackendConfig(f"b{index}", "127.0.0.1", 9000 + index, weight)
        for index, weight in enumerate(weights, start=1)
    )


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.context = ConnectionContext("192.0.2.1")

    def test_round_robin_stable_order(self):
        pool = pool_with(1, 1, 1)
        strategy = RoundRobinStrategy()
        selected = [strategy.select(pool, self.context).id for _ in range(7)]
        self.assertEqual(selected, ["b1", "b2", "b3", "b1", "b2", "b3", "b1"])

    def test_unhealthy_and_draining_excluded(self):
        pool = pool_with(1, 1, 1)
        pool.require("b1").state = BackendState.UNHEALTHY
        pool.require("b2").state = BackendState.DRAINING
        strategy = RoundRobinStrategy()
        self.assertEqual(strategy.select(pool, self.context).id, "b3")
        pool.require("b3").state = BackendState.DISABLED
        with self.assertRaises(NoHealthyBackendError):
            strategy.select(pool, self.context)

    def test_smooth_weighted_distribution(self):
        pool = pool_with(5, 3, 2)
        strategy = SmoothWeightedRoundRobinStrategy()
        counts = Counter(
            strategy.select(pool, self.context).id for _ in range(10_000)
        )
        self.assertEqual(counts, {"b1": 5000, "b2": 3000, "b3": 2000})

    def test_least_connections_and_round_robin_ties(self):
        pool = pool_with(1, 1, 1)
        pool.require("b1").active_connections = 3
        pool.require("b2").active_connections = 1
        pool.require("b3").active_connections = 1
        strategy = LeastConnectionsStrategy()
        self.assertEqual(strategy.select(pool, self.context).id, "b2")
        self.assertEqual(strategy.select(pool, self.context).id, "b3")

    def test_consistent_hash_minimizes_remapping(self):
        pool = pool_with(1, 1, 1)
        strategy = ConsistentHashStrategy(128)
        keys = [f"client-{index}" for index in range(3000)]
        before = {
            key: strategy.select(
                pool, ConnectionContext("192.0.2.1", sticky_key=key)
            ).id
            for key in keys
        }
        pool.add(BackendConfig("b4", "127.0.0.1", 9004))
        after = {
            key: strategy.select(
                pool, ConnectionContext("192.0.2.1", sticky_key=key)
            ).id
            for key in keys
        }
        remapped = sum(before[key] != after[key] for key in keys) / len(keys)
        self.assertLess(remapped, 0.4)
        self.assertGreater(remapped, 0.1)


if __name__ == "__main__":
    unittest.main()

