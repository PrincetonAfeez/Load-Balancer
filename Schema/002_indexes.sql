-- Load Balancer SQLite indexes (schema version 1)
-- Run after 001_tables.sql.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_health_backend_time
    ON health_events (backend_id, created_at);

CREATE INDEX IF NOT EXISTS idx_connection_backend_time
    ON connection_events (backend_id, created_at);

CREATE INDEX IF NOT EXISTS idx_connection_id
    ON connection_events (connection_id);

CREATE INDEX IF NOT EXISTS idx_metrics_time
    ON metrics_snapshots (created_at);

COMMIT;
