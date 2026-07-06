-- Load Balancer SQLite tables (schema version 1)
-- Run this file before 002_indexes.sql.

PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=2000;
PRAGMA foreign_keys=ON;

BEGIN;

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
    FOREIGN KEY (snapshot_id) REFERENCES config_snapshots(id)
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
    FOREIGN KEY (snapshot_id) REFERENCES config_snapshots(id)
);

INSERT OR REPLACE INTO schema_meta (key, value)
VALUES ('schema_version', '1');

COMMIT;
