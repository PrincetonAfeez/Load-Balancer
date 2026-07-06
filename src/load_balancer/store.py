"""SQLite persistence kept off the per-connection hot path."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

from .config_parser import AppConfig
from .metrics import MetricEvent, Metrics
from .pool import BackendPool

LOG = logging.getLogger(__name__)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=2000;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    source TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backend_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    backend_id TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    weight INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    tags_json TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES config_snapshots(id)
);

CREATE TABLE IF NOT EXISTS health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    event_type TEXT NOT NULL,
    backend_id TEXT,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    event_type TEXT NOT NULL,
    connection_id TEXT,
    backend_id TEXT,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reload_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER,
    created_at REAL NOT NULL,
    source TEXT NOT NULL,
    compiled_json TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES config_snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_health_backend_time
    ON health_events(backend_id, created_at);
CREATE INDEX IF NOT EXISTS idx_connection_backend_time
    ON connection_events(backend_id, created_at);
CREATE INDEX IF NOT EXISTS idx_connection_id
    ON connection_events(connection_id);
CREATE INDEX IF NOT EXISTS idx_metrics_time
    ON metrics_snapshots(created_at);
"""

# Current schema version. `SCHEMA` above is the v1 baseline. When the schema
# changes, bump this and register the upgrade statements in MIGRATIONS.
SCHEMA_VERSION = 1

# Forward-only migrations keyed by the version they upgrade *to*. v1 is the
# baseline (created by SCHEMA), so it has no entry; a future change adds e.g.
#   MIGRATIONS[2] = ("ALTER TABLE backend_config ADD COLUMN zone TEXT",)
# Migrations run in ascending version order on initialize().
MIGRATIONS: dict[int, tuple[str, ...]] = {}


class SQLiteStore:
    # Keep config history bounded: every reload/startup writes a snapshot.
    CONFIG_SNAPSHOT_RETENTION = 50
    EVENT_TABLE_RETENTION = 100_000

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA)  # idempotent v1 baseline
            current = self._current_version(connection)
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS.get(version, ()):
                    connection.execute(statement)
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            connection.commit()

    @staticmethod
    def _current_version(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row[0]) if row else 0

    def save_config_snapshot(
        self, config: AppConfig, source: str, compiled_rule: list[dict[str, Any]]
    ) -> int:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO config_snapshots(created_at, source, config_json)
                VALUES(?, ?, ?)
                """,
                (time.time(), source, config.to_json()),
            )
            snapshot_id = cursor.lastrowid
            assert snapshot_id is not None
            connection.executemany(
                """
                INSERT INTO backend_config(
                    snapshot_id, backend_id, host, port, weight, enabled, tags_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        backend.name,
                        backend.host,
                        backend.port,
                        backend.weight,
                        int(backend.enabled),
                        json.dumps(list(backend.tags)),
                    )
                    for backend in config.backends
                ],
            )
            connection.execute(
                """
                INSERT INTO rule_versions(
                    snapshot_id, created_at, source, compiled_json
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    time.time(),
                    config.rules.source,
                    json.dumps(compiled_rule, separators=(",", ":")),
                ),
            )
            self._prune_config_snapshots(connection)
            connection.commit()
            return snapshot_id

    def _prune_config_snapshots(self, connection: sqlite3.Connection) -> None:
        # Foreign keys are not enforced by default, so delete children explicitly.
        stale = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM config_snapshots ORDER BY id DESC LIMIT -1 OFFSET ?",
                (self.CONFIG_SNAPSHOT_RETENTION,),
            ).fetchall()
        ]
        if not stale:
            return
        placeholders = ",".join("?" * len(stale))
        connection.execute(
            f"DELETE FROM backend_config WHERE snapshot_id IN ({placeholders})", stale
        )
        connection.execute(
            f"DELETE FROM rule_versions WHERE snapshot_id IN ({placeholders})", stale
        )
        connection.execute(
            f"DELETE FROM config_snapshots WHERE id IN ({placeholders})", stale
        )

    def _prune_event_tables(self, connection: sqlite3.Connection) -> None:
        for table in (
            "health_events",
            "connection_events",
            "admin_events",
            "reload_events",
            "process_events",
            "metrics_snapshots",
        ):
            stale = [
                row[0]
                for row in connection.execute(
                    f"SELECT id FROM {table} ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (self.EVENT_TABLE_RETENTION,),
                ).fetchall()
            ]
            if not stale:
                continue
            placeholders = ",".join("?" * len(stale))
            connection.execute(
                f"DELETE FROM {table} WHERE id IN ({placeholders})", stale
            )

    def write_events(self, events: Iterable[MetricEvent]) -> None:
        grouped: dict[str, list[tuple[Any, ...]]] = {
            "health_events": [],
            "connection_events": [],
            "admin_events": [],
            "reload_events": [],
            "process_events": [],
        }
        for event in events:
            data_json = json.dumps(event.data, sort_keys=True, default=str)
            if event.kind.startswith("health"):
                grouped["health_events"].append(
                    (
                        event.timestamp,
                        event.kind,
                        event.data.get("backend_id"),
                        data_json,
                    )
                )
            elif event.kind.startswith("connection") or event.kind in {
                "no_eligible_backend",
                "all_connects_failed",
                "no_backend_available",
                "backend_connect_failure",
                "connection_rejected",
            }:
                grouped["connection_events"].append(
                    (
                        event.timestamp,
                        event.kind,
                        event.data.get("connection_id"),
                        event.data.get("backend_id"),
                        data_json,
                    )
                )
            elif event.kind.startswith("admin"):
                grouped["admin_events"].append(
                    (event.timestamp, event.kind, data_json)
                )
            elif event.kind.startswith("reload"):
                grouped["reload_events"].append(
                    (event.timestamp, event.kind, data_json)
                )
            else:
                grouped["process_events"].append(
                    (event.timestamp, event.kind, data_json)
                )

        with closing(self.connect()) as connection:
            connection.executemany(
                """
                INSERT INTO health_events(created_at, event_type, backend_id, data_json)
                VALUES(?, ?, ?, ?)
                """,
                grouped["health_events"],
            )
            connection.executemany(
                """
                INSERT INTO connection_events(
                    created_at, event_type, connection_id, backend_id, data_json
                ) VALUES(?, ?, ?, ?, ?)
                """,
                grouped["connection_events"],
            )
            for table in ("admin_events", "reload_events", "process_events"):
                connection.executemany(
                    f"""
                    INSERT INTO {table}(created_at, event_type, data_json)
                    VALUES(?, ?, ?)
                    """,
                    grouped[table],
                )
            self._prune_event_tables(connection)
            connection.commit()

    def write_metrics_snapshot(self, data: dict[str, Any]) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO metrics_snapshots(created_at, data_json) VALUES(?, ?)",
                (time.time(), json.dumps(data, sort_keys=True, default=str)),
            )
            self._prune_event_tables(connection)
            connection.commit()

    def metrics_summary(self) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT created_at, data_json
                FROM metrics_snapshots ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            event_counts = dict(
                connection.execute(
                    """
                    SELECT event_type, COUNT(*) FROM connection_events
                    GROUP BY event_type
                    """
                ).fetchall()
            )
        return {
            "latest_snapshot_at": row[0] if row else None,
            "latest_snapshot": json.loads(row[1]) if row else None,
            "connection_event_counts": event_counts,
        }

    def health_history(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT created_at, event_type, backend_id, data_json
                FROM health_events ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "timestamp": row[0],
                "event": row[1],
                "backend_id": row[2],
                "data": json.loads(row[3]),
            }
            for row in rows
        ]

    ROUTING_EVENT_TYPES = (
        "no_eligible_backend",
        "all_connects_failed",
        "no_backend_available",
        "backend_connect_failure",
        "connection_rejected",
    )

    def routing_history(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        placeholders = ",".join("?" * len(self.ROUTING_EVENT_TYPES))
        with closing(self.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT created_at, event_type, connection_id, backend_id, data_json
                FROM connection_events
                WHERE event_type IN ({placeholders})
                ORDER BY created_at DESC LIMIT ?
                """,
                (*self.ROUTING_EVENT_TYPES, limit),
            ).fetchall()
        return [
            {
                "timestamp": row[0],
                "event": row[1],
                "connection_id": row[2],
                "backend_id": row[3],
                "data": json.loads(row[4]),
            }
            for row in rows
        ]


class MetricsWriter:
    def __init__(
        self,
        store: SQLiteStore,
        metrics: Metrics,
        pool: BackendPool,
        flush_interval: float,
        snapshot_interval: float,
        batch_size: int,
    ) -> None:
        self.store = store
        self.metrics = metrics
        self.pool = pool
        self.flush_interval = flush_interval
        self.snapshot_interval = snapshot_interval
        self.batch_size = batch_size
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        last_snapshot = time.monotonic()
        pending: list[MetricEvent] = []
        while (
            not self._stop.is_set()
            or not self.metrics.queue.empty()
            or not self.metrics.critical_queue.empty()
            or pending
        ):
            # Drain critical events first, then top up from the normal queue.
            while True:
                try:
                    pending.append(self.metrics.critical_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                event = await asyncio.wait_for(
                    self.metrics.queue.get(), timeout=self.flush_interval
                )
                pending.append(event)
                while len(pending) < self.batch_size:
                    try:
                        pending.append(self.metrics.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except TimeoutError:
                pass

            if pending and (
                len(pending) >= self.batch_size or self._stop.is_set()
                or self.metrics.queue.empty()
            ):
                if await self._write_with_retry(pending):
                    pending.clear()
                elif self._stop.is_set():
                    self.metrics.dropped_events += sum(
                        1 for event in pending if not event.critical
                    )
                    self.metrics.dropped_critical_events += sum(
                        1 for event in pending if event.critical
                    )
                    pending.clear()
                elif len(pending) > self.batch_size * 4:
                    # Bound memory during sustained DB pressure, but never drop
                    # critical events: keep them all plus the newest non-critical.
                    criticals = [event for event in pending if event.critical]
                    non_criticals = [event for event in pending if not event.critical]
                    if len(non_criticals) > self.batch_size:
                        dropped = len(non_criticals) - self.batch_size
                        non_criticals = non_criticals[-self.batch_size:]
                        self.metrics.dropped_events += dropped
                    pending = criticals + non_criticals

            now = time.monotonic()
            if now - last_snapshot >= self.snapshot_interval:
                snapshot = self.metrics.snapshot()
                snapshot["backends"] = [
                    backend.status() for backend in self.pool.all()
                ]
                await self._snapshot_with_retry(snapshot)
                last_snapshot = now

    async def _write_with_retry(self, events: list[MetricEvent]) -> bool:
        for attempt in range(3):
            try:
                await asyncio.to_thread(self.store.write_events, list(events))
                return True
            except sqlite3.OperationalError as exc:
                LOG.warning("metrics database busy (attempt %s): %s", attempt + 1, exc)
                await asyncio.sleep(0.05 * (2**attempt))
        return False

    async def _snapshot_with_retry(self, snapshot: dict[str, Any]) -> None:
        for attempt in range(3):
            try:
                await asyncio.to_thread(self.store.write_metrics_snapshot, snapshot)
                return
            except sqlite3.OperationalError as exc:
                LOG.warning("snapshot database busy (attempt %s): %s", attempt + 1, exc)
                await asyncio.sleep(0.05 * (2**attempt))
        # Give up rather than block; record it so the loss is observable.
        self.metrics.dropped_snapshots += 1
        LOG.warning("dropping metrics snapshot after repeated database errors")
