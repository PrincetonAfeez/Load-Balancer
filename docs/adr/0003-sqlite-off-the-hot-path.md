# 3. SQLite persistence kept off the connection hot path

Status: Accepted

## Context

The project records config snapshots, health/connection events, metrics
snapshots, and admin/process/reload events. SQLite is the natural stdlib store,
but synchronous disk I/O on the per-connection path would add unbounded latency
and could block the event loop.

## Decision

No SQLite query or write happens on the accept → select → connect → relay path.
Events are emitted into bounded in-memory `asyncio.Queue`s (a normal queue plus
a dedicated critical queue). A single background writer task batches them and
writes through `asyncio.to_thread`, retrying transient busy errors with backoff
and dropping non-critical telemetry under sustained pressure. Config is read
only at startup and reload.

## Consequences

- The relay never waits on the database; telemetry is best-effort and every
  drop is counted (`dropped_events`, `dropped_critical_events`,
  `dropped_snapshots`) and surfaced in `status`.
- Critical lifecycle events get a separate intake queue so a flood of
  connection events cannot crowd them out at enqueue time.
- WAL mode plus `busy_timeout` lets the CLI read history while the daemon writes.
- Trade-off: under extreme pressure some telemetry is lost by design. SQLite is
  appropriate for capstone-scale history, not high-volume metrics.
