"""Tests for Metrics and MetricEvent."""

from __future__ import annotations

import unittest

from load_balancer.metrics import MetricEvent, Metrics


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    def test_emit_and_snapshot(self):
        metrics = Metrics(10)
        metrics.active_connections = 2
        metrics.total_connections = 5
        metrics.no_eligible_backend = 1
        metrics.all_connects_failed = 2
        metrics.emit("connection_accepted", {"id": "a"})
        snap = metrics.snapshot()
        self.assertEqual(snap["active_connections"], 2)
        self.assertEqual(snap["no_backend_available"], 3)
        self.assertIn("connection_accepted", snap["counters"])

    def test_no_backend_available_property(self):
        metrics = Metrics(10)
        metrics.no_eligible_backend = 2
        metrics.all_connects_failed = 3
        self.assertEqual(metrics.no_backend_available, 5)

    async def test_critical_queue_separate_from_normal(self):
        metrics = Metrics(2)
        metrics.emit("connection_accepted", {"id": "1"})
        metrics.emit("connection_accepted", {"id": "2"})
        metrics.emit("reload_success", {"ok": True}, critical=True)
        self.assertEqual(metrics.queue.qsize(), 2)
        self.assertEqual(metrics.critical_queue.qsize(), 1)

    async def test_queue_full_drops_normal_events(self):
        metrics = Metrics(1)
        metrics.emit("a", {})
        metrics.emit("b", {})
        self.assertEqual(metrics.dropped_events, 1)
        metrics.emit("critical", {}, critical=True)
        self.assertEqual(metrics.critical_queue.qsize(), 1)

    def test_metric_event_fields(self):
        event = MetricEvent("kind", 1.0, {"x": 1}, critical=True)
        self.assertEqual(event.kind, "kind")
        self.assertTrue(event.critical)


if __name__ == "__main__":
    unittest.main()
