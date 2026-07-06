# Architecture Decision Record
## App — Learning Load Balancer
**Traffic Distribution Systems Group | Document 1 of 5**
**Status: Accepted**

---

## Context

The Traffic Distribution Systems group requires a portfolio-ready Layer 4 load balancer that demonstrates TCP proxying, backend selection, connection accounting, active and passive health checks, draining, live administration, reload safety, and operational telemetry.

The project is **Learning Load Balancer**, a standard-library Python application exposed through the `load-balancer` command. It accepts TCP clients, chooses an eligible backend, connects with bounded retries, relays bytes in both directions, and coordinates health, metrics, admin control, reload, and shutdown through one asyncio event loop.

This is an educational systems-programming capstone. It is not internet-hardened infrastructure, an HTTP reverse proxy, a TLS terminator, or a multi-process production appliance.

---

## Decisions

### Decision 1 — Implement a Layer 4 TCP proxy

**Chosen:** Balance TCP connections and relay opaque byte streams.

**Rejected:** HTTP parsing, per-request routing, header rewriting, or application-aware Layer 7 behavior.

**Reason:** The core learning objective is socket lifecycle, backpressure, half-close behavior, timeouts, cancellation, and backend connection management.

---

### Decision 2 — Use one coherent asyncio concurrency model

**Chosen:** Own mutable runtime state in one asyncio event loop.

**Rejected:** Thread-per-connection, mixed selectors/threads/asyncio, or locks around every counter.

**Reason:** Proxy work is dominated by socket waits. Asyncio provides stream backpressure through `drain()`, task cancellation, monotonic timeouts, and one-owner mutable state.

---

### Decision 3 — Keep balancing strategies pure and separately testable

**Chosen:** Implement round robin, smooth weighted round robin, least connections, and consistent hashing as strategy objects over a backend pool.

**Rejected:** Embedding selection logic directly inside the relay loop.

**Reason:** Pure strategies can be tested independently from sockets and reused by retry logic.

---

### Decision 4 — Filter eligibility before strategy selection

**Chosen:** Only healthy, non-retired, non-draining, non-disabled, non-excluded backends are candidates.

**Rejected:** Letting each strategy independently interpret backend state.

**Reason:** A single eligibility boundary prevents inconsistent routing behavior across strategies.

---

### Decision 5 — Use smooth weighted round robin

**Chosen:** Maintain current weights and select the highest current value, then subtract total pool weight.

**Rejected:** Expanding a backend list by weight or serving large bursts to one backend.

**Reason:** Smooth weighting converges to configured ratios while avoiding bursty sequences.

---

### Decision 6 — Use pending plus active connections for least-load decisions

**Chosen:** Define backend load as `pending_connections + active_connections`.

**Rejected:** Counting only fully connected relays.

**Reason:** Concurrent connection attempts are real load. Ignoring pending attempts can stampede one backend.

---

### Decision 7 — Use a weighted SHA-256 consistent-hash ring

**Chosen:** Create `weight * virtual_nodes_per_weight` ring points and route by source IP or sticky key.

**Rejected:** Modulo hashing over the current backend count.

**Reason:** Consistent hashing limits remapping when backends enter or leave the pool. Validation caps weights and virtual-node multipliers so ring construction remains bounded.

---

### Decision 8 — Retry failed backend connects at most once per eligible backend

**Chosen:** Add failed backend IDs to the connection context and rerun selection until the remaining eligible set is exhausted.

**Rejected:** Repeatedly retrying the same backend or failing immediately after the first connect error.

**Reason:** A live pool should tolerate one backend failing between health-check intervals without looping forever.

---

### Decision 9 — Avoid mutating fairness state during retry-only selection

**Chosen:** Initial selection advances strategy state; retry selection uses `advance=False` where applicable.

**Rejected:** Counting every failed attempt as a normal scheduling decision.

**Reason:** Backend outages should not heavily distort round-robin indexes or smooth-weight state.

---

### Decision 10 — Use explicit connection states and idempotent cleanup

**Chosen:** Track accepted, selecting, connecting, relaying, half-closed, closing, closed, and failed states; route every exit through one cleanup path.

**Rejected:** Scattered counter decrements and writer cleanup in multiple exception branches.

**Reason:** Cancellation, idle timeout, connect failure, backend closure, client closure, drain deadlines, and shutdown must decrement accounting exactly once.

---

### Decision 11 — Support TCP half-close

**Chosen:** When one reader reaches EOF, attempt `write_eof()` on the opposite stream while allowing the reverse pump to continue.

**Rejected:** Closing both sides immediately on the first EOF.

**Reason:** TCP protocols can legitimately finish sending in one direction while still receiving data in the other.

---

### Decision 12 — Use active health hysteresis plus passive suspicion

**Chosen:** Mark a backend unhealthy only after consecutive failure threshold; recover only after consecutive success threshold. Live connect failures contribute passive failure evidence.

**Rejected:** Flapping state on one failed or successful probe.

**Reason:** Hysteresis tolerates transient failures while passive evidence reduces the delay between real connect failures and pool removal.

---

### Decision 13 — Drain before retirement

**Chosen:** Removed or drained backends receive no new traffic but remain in memory until active and pending counts reach zero; deadlines can force-close lingering connections.

**Rejected:** Deleting backend state immediately.

**Reason:** Existing connections need a stable backend object for accounting and graceful completion.

---

### Decision 14 — Make configuration reload transactional

**Chosen:** Parse, validate, compile the rule, apply candidate backend changes, update components, then persist the accepted snapshot.

**Rejected:** Mutating runtime state while parsing or persisting a candidate before the in-memory swap succeeds.

**Reason:** Invalid reloads must leave the current process and database history unchanged.

---

### Decision 15 — Treat listener/control rebinding and queue/database identity as restart-only

**Chosen:** Reject reload changes to listener address, control address, metrics database path, and metrics queue size.

**Rejected:** Hot-swapping every setting.

**Reason:** Those changes require replacing foundational resources and complicate rollback. Backend, strategy, timeout, health, rule, and writer cadence settings remain reloadable.

---

### Decision 16 — Use an HMAC-authenticated length-prefixed admin protocol

**Chosen:** Frame compact JSON with a four-byte big-endian length and authenticate canonical request/response fields with HMAC-SHA256.

**Rejected:** Plain unauthenticated localhost JSON or line-oriented ad hoc commands.

**Reason:** Localhost is not a sufficient authentication boundary on a shared host. Length framing gives strict message boundaries.

---

### Decision 17 — Defend both request and response directions against replay

**Chosen:** Require timestamps and secure nonces, use bounded TTL caches, sign responses, and have the client verify response signatures and nonces.

**Rejected:** Authenticating only requests.

**Reason:** A recorded signed response should not be accepted as a fresh status or command result.

---

### Decision 18 — Use a small rule DSL and bounded VM

**Chosen:** Tokenize, parse, compile, and execute a restricted routing policy language with no `eval` or `exec`.

**Rejected:** Executing Python expressions from configuration.

**Reason:** The rule system should remain inspectable, deterministic, and bounded. Config validation rejects programs that exceed the instruction limit.

---

### Decision 19 — Keep telemetry off the relay hot path

**Chosen:** Use non-blocking bounded asyncio queues and a background writer that performs SQLite operations through `asyncio.to_thread()`.

**Rejected:** Writing SQLite rows directly from connection handlers.

**Reason:** Client traffic should not wait for disk locks or database latency.

---

### Decision 20 — Prefer traffic continuity over telemetry completeness

**Chosen:** Count and drop telemetry under sustained queue/database pressure; give critical lifecycle events a separate intake queue.

**Rejected:** Blocking relay tasks until every metric is persisted.

**Reason:** The proxy is the product. Telemetry loss is observable through counters, but blocking the hot path would degrade the primary service.

---

## Consequences

**Positive:**
- Routing algorithms are independently testable.
- Mutable state has one event-loop owner.
- Backend failures can be retried safely.
- Active connection accounting is protected against double cleanup.
- Draining and removal preserve existing connections.
- Reload failures leave prior runtime state active.
- Admin messages are authenticated, bounded, and replay-resistant.
- The rule system cannot execute arbitrary Python.
- SQLite latency does not block client byte relay.
- Failure and drop conditions remain observable.

**Negative / Trade-offs:**
- One process and one event loop limit vertical scaling.
- No TLS, HTTP parsing, or kernel-level optimization.
- SQLite is not suitable for very high-volume telemetry.
- Consistent-hash ring construction occurs on the event loop, so configuration bounds are required.
- Runtime admin mutations are not automatically written back to TOML.
- Detached mode is convenience tooling, not a service manager.

---

## Alternatives Not Explored

- Multi-process workers with shared state.
- HAProxy/Nginx configuration compatibility.
- HTTP/1.1, HTTP/2, or HTTP/3 routing.
- TLS termination and certificate rotation.
- Kernel eBPF/XDP balancing.
- Zero-copy splice/sendfile relay.
- Distributed health state.
- Remote control plane.
- Persistent runtime override database.
- Production DDoS controls.

---

*Constitution reference: Article 1 (Python fundamentals and architectural thinking), Article 3.3 (scope discipline), Article 4 (quality proportional to scope), Article 5 (trade-off documentation), Article 6 (verification), and Article 7 (progressive complexity).*

---


# Technical Design Document
## App — Learning Load Balancer
**Traffic Distribution Systems Group | Document 2 of 5**

---

## Overview

Learning Load Balancer is an asyncio Layer 4 TCP proxy with pure routing strategies, event-loop-owned backend state, active/passive health checks, connection draining, transactional TOML reload, an authenticated admin protocol, a bounded policy VM, and SQLite operational history.

**Package:** `learning-load-balancer`  
**Import module:** `load_balancer`  
**Console command:** `load-balancer`  
**Python:** `>=3.11`  
**Runtime dependencies:** none  
**Development tools:** Ruff, mypy, pytest, pytest-asyncio, unittest

---

## System Context

```text
Client
  │
  ▼
asyncio TCP listener
  │
  ├── connection limit
  ├── policy VM strategy override
  ├── backend eligibility filter
  ├── routing strategy
  └── bounded backend-connect retry
        │
        ▼
Backend TCP service
        ▲
        │
 bidirectional pumps + backpressure + half-close

Background services
  ├── active health checker
  ├── metrics queues -> SQLite writer thread
  ├── HMAC control server
  └── signal/reload/shutdown coordinator
```

---

## Main Modules

```text
src/load_balancer/
  config_parser.py  # typed TOML parsing and validation
  pool.py           # Backend, BackendState, BackendPool
  strategies.py     # four selection algorithms
  proxy.py          # listener, connect retries, relay, cleanup
  health.py         # active TCP checks and hysteresis
  crypto.py         # canonical HMAC signing and replay caches
  control.py        # length-prefixed admin server/client
  rule_dsl.py       # tokenizer, parser, compiler
  vm.py             # bounded deterministic rule VM
  metrics.py        # in-memory counters and non-blocking queues
  store.py          # SQLite schema, history, metrics writer
  daemon.py         # lifecycle, reload, admin actions, shutdown
  cli.py            # operator commands and demo tools
  demo_tools.py     # dummy backends and test clients
  errors.py         # typed error hierarchy
```

---

## Configuration Model

`AppConfig` contains:
- listener host/port
- balancer strategy, drain timeout, PID file, virtual-node multiplier, max connections
- active-health interval/timeout and transition thresholds
- backend connect and idle timeouts
- metrics database, queue, batch, flush, and snapshot settings
- control host/port/frame/clock-skew limits
- admin-secret path
- rule source and instruction limit
- ordered backend entries

Validation rejects:
- unknown sections/keys
- listener/control address collision
- unknown strategy
- invalid ports and non-positive timeouts
- health timeout greater than health interval
- oversized ring, frame, queue, or batch settings
- duplicate backend IDs or addresses
- listener/control addresses reused as backends
- invalid weights or tags
- empty backend set
- invalid rule syntax or compiled rule over the instruction limit

---

## Backend Runtime Model

`BackendState`:
- `healthy`
- `unhealthy`
- `draining`
- `disabled`

`Backend` tracks:
- identity and address
- weight and tags
- active and pending connections
- lifetime connection count
- byte counters
- success/failure/passive-failure streaks
- last check result/time
- retirement state
- active connection tokens for idempotent accounting

Eligibility:
```text
state == healthy AND not retired
```

Load for least-connections:
```text
active_connections + pending_connections
```

---

## Strategy Algorithms

### Round robin

Uses stable eligible order and a moving index.

### Smooth weighted round robin

For every selection:
1. add each backend's configured weight to its current weight
2. choose the greatest current weight
3. break equal current weights by lower load
4. subtract total pool weight from the winner

Retry selection uses scratch state when it must not advance fairness state.

### Least connections

1. compute minimum `load_connections`
2. collect all tied backends
3. choose among ties using a rotating index

### Consistent hash

1. build weighted virtual nodes from `SHA256(backend_id:virtual_index)`
2. hash sticky key/source IP
3. binary-search first clockwise ring point
4. wrap to ring start when needed
5. rebuild when `(backend_id, weight)` signature changes

---

## Connection Lifecycle

```text
ACCEPTED
  -> SELECTING_BACKEND
  -> CONNECTING_BACKEND
  -> RELAYING
  -> HALF_CLOSED_CLIENT / HALF_CLOSED_BACKEND
  -> CLOSING
  -> CLOSED

Any operational failure may transition to FAILED before cleanup.
```

Each connection has:
- UUID
- client address
- accepted and last-activity monotonic timestamps
- backend ID
- directional byte counters
- close reason and error summary
- accounting and cleanup guards

---

## Connect and Retry Flow

```text
build ConnectionContext
  │
  ▼
choose strategy through rule VM
  │
  ▼
select eligible backend
  │
  ├── increment pending
  ├── asyncio.open_connection with connect timeout
  └── decrement pending in finally
        │
        ├── success: open/account connection
        └── failure:
              add backend to excluded set
              record passive failure
              emit critical event
              rerun strategy without normal advancement
```

When every candidate fails, the proxy raises a summarized backend-connect error.

---

## Relay Flow

Two tasks pump data concurrently:
- client -> backend
- backend -> client

Each pump:
- reads up to 64 KiB
- writes to opposite stream
- awaits `drain()` for backpressure
- updates monotonic activity and directional counters
- propagates EOF with `write_eof()` where supported

An idle watcher cancels both pumps after configured inactivity. An exception in one pump cancels its sibling. Cleanup closes writers and waits with a bounded close timeout.

---

## Health System

The health checker:
- runs one periodic coroutine
- checks eligible non-disabled/non-draining backends concurrently
- uses TCP connect with timeout
- applies consecutive failure/success thresholds
- emits check events and critical transition events
- can be woken immediately by stop, reload, or explicit check request

Passive connect failures increment the same failure evidence and can trigger unhealthy transition between active checks.

---

## Drain and Retirement

`drain`:
- excludes backend from future selection
- preserves active/pending state
- schedules a deadline
- force-cancels remaining relay tasks after timeout

`remove`:
- marks backend draining and retired
- removes it from memory only when active and pending counts are both zero

Reload preserves unhealthy and draining state instead of resetting operational decisions.

---

## Rule DSL and VM

Grammar supports:
- `return "strategy"`
- `if expression then return "strategy" [else return "strategy"]`
- fields: `client.ip`, `client.port`, `backend.tag`, `backend.tags`, `command`, `args.backend_id`
- operators: `==`, `startswith`, `contains`, `and`, `or`, parentheses

Compiler emits:
- `LOAD_FIELD`
- `LOAD_CONST`
- `EQ`
- `STARTSWITH`
- `CONTAINS`
- `AND`
- `OR`
- `JUMP_IF_FALSE`
- `RETURN`

On the L4 connection path only `client.ip` and `client.port` are populated. Unknown/unavailable fields resolve to `None`. A non-strategy return value falls back to the configured strategy.

---

## Admin Protocol

Frame:
```text
length:u32 big-endian | UTF-8 JSON body
```

Signed request fields:
- version
- timestamp
- nonce
- command
- args

Signed response fields:
- version
- timestamp
- nonce
- ok
- data or error

Security checks:
- frame size and completeness
- JSON object shape
- command allowlist and argument types
- protocol version
- bounded clock skew
- canonical compact sorted JSON
- HMAC-SHA256 with constant-time comparison
- bounded nonce replay caches
- signed response verification on the client

---

## Metrics and Persistence

`Metrics` owns in-memory counters and two bounded queues:
- normal events
- critical lifecycle/reload/admin/health events

Queue insertion uses `put_nowait()`. Full queues increment drop counters instead of blocking.

SQLite uses WAL mode and stores:
- schema metadata
- config snapshots and backend configurations
- rule versions
- health events
- connection/routing events
- admin events
- reload events
- process events
- metrics snapshots

The writer:
- drains critical events first
- batches normal events
- calls SQLite through `asyncio.to_thread()`
- retries busy errors with exponential backoff
- bounds pending non-critical memory under sustained pressure
- never drops critical pending events during ordinary pressure
- records dropped snapshots after retry exhaustion

Retention:
- 50 config snapshots
- 100,000 rows per event/snapshot table

---

## Reload Transaction

```text
acquire reload lock
  -> parse candidate TOML off-loop
  -> validate/compile candidate
  -> reject restart-only differences
  -> apply backend candidate and schedule drains
  -> update proxy, health, and writer settings
  -> swap current config
  -> persist accepted snapshot off-loop
  -> emit reload_success
```

Any error emits `reload_failure` and leaves the previous accepted configuration running.

---

## Graceful Shutdown

1. reject new mutating admin work
2. wait for in-flight reload lock
3. close control listener while letting current command reply finish
4. stop health checks
5. cancel backend drain-deadline tasks
6. stop accepting client connections
7. wait for relays up to drain timeout
8. cancel remaining relays
9. flush critical process events
10. stop metrics writer after queues drain
11. release PID file

---

## Limits

- One process and one event loop.
- No HTTP or TLS.
- No multi-process shared backend state.
- No persistent runtime override layer.
- No DDoS hardening.
- No zero-copy forwarding.
- Control plane assumes local/private deployment despite HMAC protection.
- SQLite telemetry favors inspectability over high-throughput analytics.

---

## Verification Summary

Repository tooling verifies:
- Python 3.11, 3.12, and 3.13
- Ruff lint
- mypy over `src`
- unittest discovery with ResourceWarnings promoted to errors
- pytest including asyncio integration cases

The README reports 191 tests spanning strategies, health, config validation, HMAC framing/replay, rule compiler/VM, proxy integration, reload rollback, SQLite pressure, daemon control, graceful shutdown, and failure injection.

---

*Constitution reference: Article 4 (engineering quality), Article 6 (behavior verification), Article 7 (progressive complexity), and Article 8 (valid learner work).*

---


# Interface Design Specification
## App — Learning Load Balancer
**Traffic Distribution Systems Group | Document 3 of 5**

---

## Public CLI

```powershell
load-balancer [--config FILE] [--json] [--log-level LEVEL] <command>
```

Global options:
- `--config` — TOML path, default `load-balancer.toml`
- `--json` — structured output
- `--log-level` — DEBUG, INFO, WARNING, or ERROR
- `--version`

---

## Initialization Commands

```powershell
load-balancer --config load-balancer.toml config validate
load-balancer --config load-balancer.toml init-secret
load-balancer --config load-balancer.toml init-secret --force
load-balancer --config load-balancer.toml init-db
```

`init-secret`:
- creates a cryptographically random secret
- refuses overwrite unless `--force`
- requests owner-only `0600` permissions where supported

`init-db` initializes or migrates the SQLite schema.

---

## Process Commands

```powershell
load-balancer --config load-balancer.toml start --foreground
load-balancer --config load-balancer.toml start --daemon
load-balancer --config load-balancer.toml status
load-balancer --config load-balancer.toml reload
load-balancer --config load-balancer.toml stop
```

Foreground mode shows logs directly. Daemon mode spawns a detached process and confirms readiness with an authenticated status request.

---

## Backend Commands

```powershell
load-balancer backends list
load-balancer backends add NAME HOST PORT [--weight N] [--tag TAG ...]
load-balancer backends enable BACKEND_ID
load-balancer backends disable BACKEND_ID
load-balancer backends drain BACKEND_ID
load-balancer backends remove BACKEND_ID
```

Behavior:
- add validates name, address, weight, tags, and reserved-address collisions
- drain stops new assignments while preserving existing connections
- disable also enforces a drain deadline for existing connections
- remove retires the backend after connections close
- enable resets health streaks and restores eligibility

Runtime mutations are in memory. A later reload re-applies TOML configuration while preserving relevant operational health/drain state.

---

## Strategy Commands

```powershell
load-balancer strategy get
load-balancer strategy set round_robin
load-balancer strategy set weighted_round_robin
load-balancer strategy set least_connections
load-balancer strategy set consistent_hash
```

A rule may override the configured/current strategy for a connection.

---

## Metrics Commands

```powershell
load-balancer metrics summary
load-balancer metrics health-history --limit 50
load-balancer metrics routing-history --limit 50
```

Queries read persisted SQLite history rather than requiring a running daemon.

---

## Demo Commands

### Dummy backends

```powershell
load-balancer dummy-backend echo --port 9001 --name echo-1
load-balancer dummy-backend slow --port 9002 --delay-ms 500
load-balancer dummy-backend flaky --port 9003 --fail-rate 0.3
load-balancer dummy-backend close-immediately --port 9004
```

### Client

```powershell
load-balancer lb-client send --host 127.0.0.1 --port 8080 --message hello --count 12
load-balancer lb-client hold-open --host 127.0.0.1 --port 8080 --seconds 60
```

---

## Config Interface

```toml
[listener]
host = "127.0.0.1"
port = 8080

[balancer]
strategy = "round_robin"
drain_timeout_seconds = 10
pid_file = "load-balancer.pid"
virtual_nodes_per_weight = 64
max_connections = 0

[health]
interval_seconds = 2
timeout_seconds = 1
failures_to_unhealthy = 3
successes_to_healthy = 2

[timeouts]
connect_seconds = 2
idle_seconds = 60

[metrics]
database_path = "load-balancer.db"
flush_interval_seconds = 0.5
snapshot_interval_seconds = 5
queue_size = 10000
batch_size = 250

[control]
host = "127.0.0.1"
port = 9900
max_frame_bytes = 65536
max_clock_skew_seconds = 30

[crypto]
secret_file = "admin.secret"

[rules]
source = 'return "default"'
max_instructions = 256

[[backends]]
name = "echo-1"
host = "127.0.0.1"
port = 9001
weight = 2
tags = ["stable"]
```

`max_connections = 0` means unlimited.

---

## Rule Interface

```text
return "default"
```

```text
if client.ip startswith "10." and client.port == "443"
then return "consistent_hash"
else return "round_robin"
```

Supported fields:
- `client.ip`
- `client.port`
- `backend.tag`
- `backend.tags`
- `command`
- `args.backend_id`

Supported operators:
- `==`
- `startswith`
- `contains`
- `and`
- `or`
- parentheses

Only client fields are supplied on the current L4 routing path.

---

## Admin Command Contract

Supported wire commands:
- `status`
- `stop`
- `reload`
- `backends.list`
- `backends.add`
- `backends.remove`
- `backends.enable`
- `backends.disable`
- `backends.drain`
- `strategy.get`
- `strategy.set`

Request example shape:

```json
{
  "version": 1,
  "timestamp": 1783360000,
  "nonce": "secure-random-text",
  "command": "status",
  "args": {},
  "signature": "hex-hmac"
}
```

Response shape:

```json
{
  "version": 1,
  "timestamp": 1783360000,
  "nonce": "new-response-nonce",
  "ok": true,
  "data": {},
  "signature": "hex-hmac"
}
```

---

## Status Contract

Status includes:
- PID and listener address
- configured/current strategy
- whether the rule can override strategy
- shutdown flag
- max-connection limit and slots in use
- metrics counters and telemetry-drop counters
- backend states and counters
- connection details
- true connection count
- truncation flag when more than 100 connection records exist

---

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | Success |
| `1` | Known configuration, admin, I/O, or runtime error |
| `2` | Argparse usage error |

---

## Public Python Surface

Primary implementation classes/functions include:

```python
from load_balancer.config_parser import AppConfig, BackendConfig, load_config
from load_balancer.pool import Backend, BackendPool, BackendState
from load_balancer.strategies import ConnectionContext, create_strategy
from load_balancer.proxy import TCPProxy
from load_balancer.health import HealthChecker
from load_balancer.control import ControlClient, ControlServer
from load_balancer.rule_dsl import compile_rule
from load_balancer.vm import VirtualMachine
from load_balancer.metrics import Metrics
from load_balancer.store import SQLiteStore, MetricsWriter
from load_balancer.daemon import LoadBalancerDaemon
```

The CLI is the supported operator interface; module imports primarily support testing and educational inspection.

---

## Side Effects

| Operation | Side Effect |
|---|---|
| `start` | Binds listener/control sockets, creates PID file, initializes DB |
| proxy connection | Opens backend socket and relays bytes |
| health check | Opens short-lived TCP connection to backend |
| `init-secret` | Writes secret file |
| `init-db` | Creates/migrates SQLite schema |
| reload | Mutates in-memory pool/config and writes accepted snapshot |
| admin backend actions | Mutate runtime backend state |
| metrics writer | Writes/prunes SQLite rows |
| daemon mode | Spawns detached process |
| shutdown | Stops listeners, drains/cancels connections, removes PID file |

---

*Constitution reference: Article 4 (input/output boundaries), Article 6 (verification), and Article 8 (understandable and verifiable work).*

---


# Runbook
## App — Learning Load Balancer
**Traffic Distribution Systems Group | Document 4 of 5**

---

## Requirements

- Python 3.11+
- No third-party runtime dependencies
- Ruff, mypy, pytest, and pytest-asyncio for development

---

## Installation

```powershell
python -m pip install -e .
```

Development:

```powershell
python -m pip install -r requirements-dev.txt
```

---

## Initial Setup

```powershell
load-balancer --config load-balancer.toml config validate
load-balancer --config load-balancer.toml init-secret
load-balancer --config load-balancer.toml init-db
```

The daemon will initialize the database automatically, but the admin secret is mandatory.

---

## First End-to-End Test

Start three backends:

```powershell
load-balancer dummy-backend echo --port 9001 --name echo-1
load-balancer dummy-backend echo --port 9002 --name echo-2
load-balancer dummy-backend echo --port 9003 --name echo-3
```

Start the balancer:

```powershell
load-balancer --config load-balancer.toml start --foreground
```

Send traffic:

```powershell
load-balancer --config load-balancer.toml lb-client send --message "hello" --count 12
```

Inspect:

```powershell
load-balancer --config load-balancer.toml status
load-balancer --config load-balancer.toml backends list
load-balancer --config load-balancer.toml metrics summary
```

---

## Strategy Demonstration

```powershell
load-balancer strategy set round_robin
load-balancer lb-client send --message rr --count 12

load-balancer strategy set weighted_round_robin
load-balancer lb-client send --message weighted --count 20

load-balancer strategy set least_connections
load-balancer lb-client hold-open --seconds 30

load-balancer strategy set consistent_hash
load-balancer lb-client send --message sticky --count 10
```

Expected observations:
- round robin cycles through eligible order
- weighted distribution approaches configured ratios
- least connections avoids busier backends
- consistent hash keeps one source IP mapped until ring membership changes

---

## Health Failure and Recovery

1. Stop one dummy backend.
2. Watch status and history:

```powershell
load-balancer status
load-balancer metrics health-history --limit 20
load-balancer metrics routing-history --limit 20
```

Expected:
- connect retries avoid the failed backend
- passive failures appear immediately
- active checks eventually cross unhealthy threshold
- backend returns only after configured consecutive successes

---

## Drain Procedure

```powershell
load-balancer backends drain echo-3
load-balancer status
```

Expected:
- no new connection selects `echo-3`
- current relays remain active
- lingering relays are cancelled at drain deadline

Remove after drain:

```powershell
load-balancer backends remove echo-3
```

---

## Transactional Reload

Edit TOML reload-safe fields, then:

```powershell
load-balancer reload
```

Verify:

```powershell
load-balancer status
load-balancer metrics summary
```

Rejected reload examples:
- invalid TOML
- duplicate backend address
- rule syntax error
- oversized compiled rule
- listener/control address change
- metrics DB path or queue-size change
- address change for backend with live connections

An unsuccessful reload leaves the prior configuration active.

---

## Graceful Shutdown

```powershell
load-balancer stop
```

Expected:
- control stops accepting new commands after current reply
- listener stops accepting clients
- existing connections drain up to configured timeout
- remaining tasks are cancelled through shared cleanup
- metrics queues flush
- PID file is removed

POSIX signals:
- `SIGINT` / `SIGTERM` — shutdown
- `SIGHUP` — reload

Windows:
- Ctrl+C / SIGBREAK — shutdown
- use CLI for reload

---

## Daemon Mode

```powershell
load-balancer start --daemon
load-balancer status
load-balancer stop
```

The launcher validates config/secret first and performs a signed status probe after spawning.

For visible startup errors, use foreground mode.

---

## Database Inspection

Operational queries:

```powershell
load-balancer metrics summary
load-balancer metrics health-history --limit 50
load-balancer metrics routing-history --limit 50
```

Database characteristics:
- SQLite WAL
- two-second busy timeout
- schema version in `schema_meta`
- forward-only migration registry
- bounded history retention

---

## Quality Checks

```powershell
ruff check .
mypy src
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m pytest tests -v
```

CI runs the same checks on Python 3.11, 3.12, and 3.13.

---

## Troubleshooting

### Admin secret missing

```text
admin secret not found; run init-secret
```

Fix:

```powershell
load-balancer init-secret
```

---

### No healthy eligible backend

Check:

```powershell
load-balancer status
load-balancer metrics health-history
```

Confirm:
- at least one backend process is running
- backend is enabled and not draining
- health threshold has recovered
- address/port are correct

---

### All backend connection attempts failed

Meaning:
- eligible candidates existed
- every bounded connect attempt failed

Check routing history and backend processes. Active health checks may not yet have crossed the failure threshold.

---

### Reload requires restart

Restart-only changes:
- listener address
- control address/settings
- metrics database path
- metrics queue size

Perform graceful stop, change config, then start.

---

### Admin authentication or replay error

Check:
- CLI and daemon use the same secret file
- system clocks are within allowed skew
- old recorded requests/responses are not being replayed
- control frame has correct version and fields

---

### Metrics drop counters increase

Meaning:
- event queues or SQLite writer could not keep up

Actions:
- inspect disk/database contention
- increase queue size only with restart
- tune batch and flush interval through reload
- reduce telemetry pressure

Traffic continues by design.

---

### Idle connections close

The idle timeout is based on no bytes flowing in either direction. Increase `timeouts.idle_seconds` and reload when longer quiet sessions are expected.

---

### Existing connections remain after lowering max limit

Expected. A reload changes admission for new connections but does not disconnect existing clients.

---

## Maintenance Notes

- Preserve one-owner event-loop state.
- Keep SQLite out of the relay hot path.
- Add tests before changing accounting/cleanup.
- Add tests before changing strategy fairness semantics.
- Preserve HMAC canonicalization and replay checks.
- Keep rule execution bounded and free of `eval`/`exec`.
- Keep reload persistence after successful in-memory swap.
- Add an ADR before introducing TLS, HTTP routing, multi-process workers, or a remote control plane.

---

*Constitution reference: Article 6 (behavior verification), Article 5 (constraints and trade-offs), and Article 8 (verifiable learner work).*

---


# Lessons Learned
## App — Learning Load Balancer
**Traffic Distribution Systems Group | Document 5 of 5**

---

## Why This Design Was Chosen

A load balancer is not only a selection algorithm. The real engineering work is connection lifecycle management: partial connects, retries, two-way relay, half-closes, cancellation, idle timeouts, accurate counters, health transitions, draining, reload, and shutdown.

Asyncio is the right fit for this capstone because most time is spent waiting on sockets. It also makes the ownership model clear: the event loop owns runtime state, while blocking SQLite operations move to a worker thread.

Separating the strategy layer was equally important. Round robin, smooth weighting, least connections, and consistent hashing can be reasoned about as algorithms without needing a network test for every property.

---

## What Was Intentionally Omitted

**HTTP routing:** Out of scope.

**TLS termination:** Out of scope.

**Multi-process workers:** Deferred.

**Kernel/socket tuning:** Deferred.

**Zero-copy relay:** Deferred.

**Remote control plane:** Out of scope.

**Persistent runtime overrides:** Deferred.

**Distributed health checking:** Deferred.

**Production DDoS protection:** Out of scope.

**Service-manager integration:** Detached mode is only a convenience.

---

## Biggest Weakness

The biggest weakness is single-process scaling. One event loop provides a clean correctness model, but CPU-heavy work or very high connection counts would eventually require multiple workers and a design for shared or partitioned state.

The second weakness is the control-plane deployment model. HMAC, timestamps, and replay caches are meaningful protections, but the default design still assumes a local/private control socket and shared-secret lifecycle.

The third weakness is SQLite telemetry throughput. It is excellent for a capstone and operational inspection, but not a substitute for a production metrics pipeline.

---

## Scaling Considerations

**If connection volume grows:**
- add `SO_REUSEPORT` multi-worker architecture
- partition connections by process
- define how health and admin state propagate
- benchmark memory per connection
- investigate uvloop or lower-level transports only after profiling

**If telemetry grows:**
- export counters to Prometheus/OpenTelemetry
- move event history to an external stream/store
- retain bounded local audit data only

**If security grows:**
- use mutually authenticated TLS for admin transport
- rotate secrets/credentials
- introduce authorization roles
- bind control to a Unix domain socket where appropriate

**If routing grows:**
- add a separate Layer 7 project boundary
- preserve the L4 relay as a reusable transport core
- avoid injecting HTTP semantics into pure TCP strategy code

---

## What the Next Refactor Would Be

1. **Persistent override model** — explicitly layer TOML defaults and runtime changes.

2. **Structured reload plan** — compute and validate an immutable change plan before applying any pool mutation.

3. **Multi-worker architecture ADR** — define shared health, consistent-hash membership, and admin coordination.

4. **Metrics exporter** — expose live counters without querying SQLite.

5. **Connection backpressure limits** — add per-client/global bandwidth or buffer policies.

---

## What This Project Taught

- **Load balancing begins after eligibility.** A perfect algorithm cannot route to unhealthy or draining backends.

- **Pending work is load.** Least-connections must include in-progress connects.

- **Cleanup is a state machine.** Every timeout, cancellation, EOF, and exception must converge on one idempotent path.

- **Half-close semantics matter.** TCP is bidirectional, and one side finishing does not always end the session.

- **Health checks need memory.** Hysteresis avoids flapping, while passive evidence shortens detection time.

- **Reload is a transaction.** Parse, validate, apply, then persist; never record a rejected candidate as active configuration.

- **Control responses need authentication too.** Request authentication alone does not prevent replayed or forged operator output.

- **A tiny VM can be safer than configuration code execution.** Restricting fields and opcodes makes policy behavior inspectable.

- **Observability must not become the outage.** Dropped telemetry is preferable to blocking client traffic, provided the loss is counted.

- **Scope discipline strengthens the portfolio.** A correct educational L4 proxy is more defensible than an incomplete claim to be production HAProxy.

---

*Constitution v2.0 checklist: This document satisfies Article 5 (trade-off documentation), Article 6 (verification), and Article 7 (progressive complexity) for Learning Load Balancer.*
