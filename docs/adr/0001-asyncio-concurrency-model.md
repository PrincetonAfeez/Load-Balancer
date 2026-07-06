# 1. asyncio as the single concurrency model

Status: Accepted

## Context

A TCP load balancer is dominated by waiting on sockets: accepting clients,
connecting to backends, relaying bytes, probing health, and serving an admin
socket. The three realistic models are raw `selectors`, thread-per-connection,
and `asyncio`. We also need correct per-backend active-connection accounting,
which is easy to get wrong under preemptive concurrency.

## Decision

Use `asyncio` streams and tasks as the *only* concurrency model. All mutable
runtime state (the backend pool, connection records, counters, strategy state)
is owned by a single event loop. Blocking work (SQLite, config parsing) is
pushed to threads via `asyncio.to_thread`; nothing else runs off-loop.

## Consequences

- Selection and accounting are written as synchronous, lock-free functions that
  run to completion between `await` points, so counters cannot be torn by
  interleaving. Active counts increment only after a successful connect and
  decrement exactly once in an idempotent cleanup path.
- Backpressure comes for free via `StreamWriter.drain()`; cancellation is
  explicit (`task.cancel()`), used for drain timeouts and graceful shutdown.
- **Deadlock/starvation:** the single-owner model has no lock ordering to
  deadlock on. The only lock is one non-reentrant `asyncio.Lock` guarding
  config reload; it is never held across the relay hot path or a blocking call.
  Telemetry uses bounded queues with an explicit drop policy so a slow database
  can never starve the relay — the hot path always wins over observability. The
  health checker and metrics writer are cooperatively scheduled background
  tasks that yield on every `await`.
- Trade-off: raw `selectors` could shave abstraction overhead and
  thread-per-connection maps blocking APIs more directly, but both make exact
  cancellation and counter cleanup harder. We accept asyncio's overhead for one
  coherent, reason-about-able model.
