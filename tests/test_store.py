"""SQLite store and metrics writer tests."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest

from load_balancer.config_parser import (
    AppConfig,
    BackendConfig,
    ListenerConfig,
    MetricsConfig,
)
from load_balancer.metrics import MetricEvent, Metrics
from load_balancer.pool import BackendPool
from load_balancer.store import MetricsWriter, SQLiteStore


def sample_config() -> AppConfig:
    return AppConfig(
        listener=ListenerConfig("127.0.0.1", 8080),
        backends=(BackendConfig("one", "127.0.0.1", 9001, weight=2),),
        metrics=MetricsConfig(database_path="ignored.db"),
    )


class StoreTests(unittest.TestCase):
    def test_initialize_and_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.db"
            store = SQLiteStore(path)
            store.initialize()
            with closing(store.connect()) as conn:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                self.assertEqual(int(row[0]), 1)

    def test_save_config_snapshot_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.db"
            store = SQLiteStore(path)
            store.initialize()
            config = sample_config()
            compiled = [{"op": "RETURN", "arg": "default"}]
            ids = [
                store.save_config_snapshot(config, "cfg.toml", compiled)
                for _ in range(3)
            ]
            self.assertEqual(len(ids), 3)
            with closing(store.connect()) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM config_snapshots"
                ).fetchone()[0]
                self.assertLessEqual(count, SQLiteStore.CONFIG_SNAPSHOT_RETENTION)

    def test_write_events_all_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.db"
            store = SQLiteStore(path)
            store.initialize()
            now = time.time()
            events = [
                MetricEvent("health_check", now, {"backend_id": "b1"}, False),
                MetricEvent("health_transition", now, {"backend_id": "b1"}, True),
                MetricEvent("connection_accepted", now, {"connection_id": "c1"}, False),
                MetricEvent("no_eligible_backend", now, {"connection_id": "c2"}, False),
                MetricEvent("admin_accepted", now, {"command": "status"}, False),
                MetricEvent("reload_success", now, {"ok": True}, True),
                MetricEvent("process_started", now, {"pid": 1}, True),
            ]
            store.write_events(events)
            summary = store.metrics_summary()
            self.assertIn("connection_event_counts", summary)
            health = store.health_history(10)
            self.assertTrue(health)
            routing = store.routing_history(10)
            self.assertTrue(any(e["event"] == "no_eligible_backend" for e in routing))

    def test_write_metrics_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.db"
            store = SQLiteStore(path)
            store.initialize()
            store.write_metrics_snapshot({"active_connections": 0})
            summary = store.metrics_summary()
            self.assertIsNotNone(summary["latest_snapshot"])


class MetricsWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_flushes_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.db"
            store = SQLiteStore(path)
            store.initialize()
            pool = BackendPool.from_configs(sample_config().backends)
            metrics = Metrics(100)
            writer = MetricsWriter(store, metrics, pool, 0.05, 0.1, 5)
            metrics.emit("connection_accepted", {"connection_id": "x"})
            metrics.emit("process_started", {"pid": 1}, critical=True)
            task = asyncio.create_task(writer.run())
            await asyncio.sleep(0.2)
            writer.stop()
            await task
            summary = store.metrics_summary()
            self.assertGreater(
                summary["connection_event_counts"].get("connection_accepted", 0), 0
            )


if __name__ == "__main__":
    unittest.main()
