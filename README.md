# Learning Load Balancer

An educational, fully working Layer 4 TCP load balancer built with Python's
standard library. It accepts TCP connections, chooses a healthy backend, relays
bytes in both directions, performs active and passive health checks, and exposes
an HMAC-authenticated admin CLI.

This is a systems-programming capstone, not internet-ready infrastructure.

## What is implemented

- Asyncio TCP proxy with bidirectional streaming, half-close handling, idle and
  connect timeouts, retry around failed backends, and idempotent accounting
- Round robin, smooth weighted round robin, least connections, and consistent
  hashing with weighted virtual nodes
- Active TCP health checks with failure/success hysteresis and passive failure
  suspicion
- Backend drain, disable, enable, runtime add/remove, graceful shutdown, and
  transactional config reload
- Length-prefixed JSON control protocol authenticated with SHA-256 HMAC,
  canonical JSON, timestamps, secure nonces, and replay prevention
- Typed TOML configuration and a safe rule DSL compiled to bytecode for a
  bounded stack VM—no `eval` or `exec`
- SQLite config snapshots, health history, connection events, process/admin
  events, and periodic metrics snapshots
- SQLite writes through a bounded asyncio queue and worker thread, never from
  the relay hot path
- Dummy echo/slow/flaky/close-immediately backends and load-balancer clients
- Unit, failure-path, network integration, daemon/control, reload, and graceful
  shutdown tests

## Architecture

```text
                       background tasks
                 +---------------------------+
                 | health checker            |
                 | metrics queue -> SQLite   |
                 | HMAC control server       |
                 +-------------+-------------+
                               |
client -> listener -> strategy -> backend connect -> backend
   |                           <->                          |
   +-------------- bidirectional byte relay --------------+

Config TOML -> validate -> compile rule DSL -> candidate pool -> atomic reload
```

Mutable runtime state is owned by one asyncio event loop. Config parsing,
SQLite work, and admin parsing are outside the connection hot path.

## Requirements and installation

Python 3.11 or newer (CI exercises 3.11, 3.12, and 3.13). Runtime dependencies are
stdlib-only; optional dev tooling is listed in `pyproject.toml`,
`requirements-dev.txt` (unpinned), and `requirements-dev.lock` (pinned for CI).

```powershell
python -m pip install -e .
load-balancer --config load-balancer.toml config validate
load-balancer --config load-balancer.toml init-secret
load-balancer --config load-balancer.toml init-db
```

For linting, type checking, and tests:

```powershell
python -m pip install -r requirements-dev.txt
```

`init-secret` is required before starting (the daemon refuses to run without a
secret). `init-db` is optional: `start` initializes the schema on first run if
the database does not yet exist; run it explicitly only if you want the file
created up front.

The repository's default config binds only to localhost and expects backends on
ports 9001–9003.

## Quick start

Open three terminals:

```powershell
load-balancer dummy-backend echo --port 9001 --name echo-1
load-balancer dummy-backend echo --port 9002 --name echo-2
load-balancer dummy-backend echo --port 9003 --name echo-3
```

Then start the balancer:

```powershell
load-balancer --config load-balancer.toml start --foreground
```

From another terminal:

```powershell
load-balancer --config load-balancer.toml lb-client send --message "hello" --count 12
load-balancer --config load-balancer.toml status
load-balancer --config load-balancer.toml strategy set weighted_round_robin
load-balancer --config load-balancer.toml backends drain echo-3
load-balancer --config load-balancer.toml reload
load-balancer --config load-balancer.toml metrics health-history
load-balancer --config load-balancer.toml stop
```

`start --daemon` launches a detached process. Foreground mode is preferable
while learning because logs and failures remain visible.

On POSIX, `SIGINT`/`SIGTERM` trigger graceful shutdown and `SIGHUP` triggers a
reload; on Windows, `Ctrl+C` and `SIGBREAK` (`Ctrl+Break`) trigger graceful
shutdown (reload is CLI-only there). The balancer logs a warning at startup if
the listener or control socket is bound to a non-loopback address, since the
security model assumes localhost-only access.

The CLI exit codes are: `0` success, `1` a known error (bad config, admin/IO
failure, invalid argument), and `2` a command-line usage error (from argparse).
They are also shown in `load-balancer --help`.

For an automated tour of round robin, weighted selection, least connections,
consistent hashing, health failure/recovery, drain, live reload, HMAC, the rule
VM, SQLite, and shutdown:

```powershell
python scripts/demo.py
```

## CLI Reference

All commands accept global options before the subcommand:

| Option | Default | Description |
|--------|---------|-------------|
| `--config PATH` | `load-balancer.toml` | TOML config file |
| `--json` | off | Emit machine-readable JSON instead of formatted text |
| `--log-level LEVEL` | `INFO` | Logging level for foreground/detached daemon (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `--version` | — | Print package version and exit |

Exit codes for every command: `0` success, `1` known error (bad config, admin/IO
failure, invalid argument), `2` argparse usage error. Run `load-balancer --help`
for the full tree.

Persistence key:

- **disk** — writes or reads files on disk (secret, SQLite, TOML reload)
- **runtime** — changes the running daemon only until the next `reload` or restart
- **read-only** — no daemon or disk mutation

### Database and secret setup

| Command | Persistence | Description |
|---------|-------------|-------------|
| `init-db` | disk | Create or migrate the SQLite metrics schema |
| `init-secret [--force]` | disk | Write a new admin HMAC secret (`--force` replaces an existing file) |

### Daemon lifecycle

| Command | Persistence | Description |
|---------|-------------|-------------|
| `start --foreground` | process | Run the daemon attached to the terminal |
| `start --daemon` | process | Spawn a detached background daemon |
| `status` | read-only | Query live PID, listener, strategy, backends, and metrics |
| `reload` | disk + runtime | Transactionally reload `load-balancer.toml` into the running daemon |
| `stop` | runtime | Request graceful shutdown via the admin socket |

### Backend management

All `backends` subcommands talk to the running daemon over the signed admin
protocol. Runtime mutations are **not** written back to TOML.

| Command | Persistence | Description |
|---------|-------------|-------------|
| `backends list` | read-only | List backends and runtime state |
| `backends add NAME HOST PORT [--weight N] [--tag TAG]` | runtime | Add a backend (`--weight` default `1`; `--tag` repeatable) |
| `backends remove BACKEND_ID` | runtime | Remove a backend after draining active connections |
| `backends enable BACKEND_ID` | runtime | Mark a backend eligible for new traffic |
| `backends disable BACKEND_ID` | runtime | Exclude a backend from new selections |
| `backends drain BACKEND_ID` | runtime | Stop selecting a backend while existing connections finish |

### Strategy

| Command | Persistence | Description |
|---------|-------------|-------------|
| `strategy get` | read-only | Show active strategy, configured strategy, and rule source |
| `strategy set NAME` | runtime | Set runtime strategy (`round_robin`, `weighted_round_robin`, `least_connections`, `consistent_hash`) |

### Metrics

Read persisted history from SQLite (no daemon mutation):

| Command | Description |
|---------|-------------|
| `metrics summary` | Most recent metrics snapshot |
| `metrics health-history [--limit N]` | Recent health transitions (`--limit` default `50`) |
| `metrics routing-history [--limit N]` | Recent connection/routing events (`--limit` default `50`) |

### Configuration utilities

| Command | Persistence | Description |
|---------|-------------|-------------|
| `config validate` | read-only | Parse, type-check, and compile rules without starting the daemon |

### Local testing tools

Standalone helpers that do not require the admin secret:

| Command | Description |
|---------|-------------|
| `dummy-backend MODE --port PORT [--host HOST] [--name NAME] [--delay-ms MS] [--fail-rate RATE]` | Run a test backend. Modes: `echo`, `slow`, `flaky`, `close-immediately`. Defaults: `--host 127.0.0.1`, `--delay-ms 500`, `--fail-rate 0.3` |
| `lb-client send [--host HOST] [--port PORT] [--message TEXT] [--count N]` | Send traffic through the balancer (defaults: host `127.0.0.1`, port `8080`, message `hello`, count `1`) |
| `lb-client hold-open [--host HOST] [--port PORT] [--seconds SECONDS]` | Keep one connection open (default `60` seconds) |

## Configuration

The complete starter file is [load-balancer.toml](load-balancer.toml).

```toml
[listener]
host = "127.0.0.1"
port = 8080

[balancer]
strategy = "round_robin"
drain_timeout_seconds = 30

[health]
interval_seconds = 2
timeout_seconds = 1
failures_to_unhealthy = 3
successes_to_healthy = 2

[[backends]]
name = "api-1"
host = "127.0.0.1"
port = 9001
weight = 2
tags = ["stable"]
```

Relative paths for the PID, secret, and database are resolved from the
process's working directory. Listener and control addresses, the metrics
`database_path`, and the metrics `queue_size` are restart-only (reload rejects
changes to them); backend, strategy, timeout, health, metrics flush/snapshot
interval, batch size, and rule changes can be reloaded.

Reload is transactional:

1. Parse and type-check candidate TOML.
2. Validate addresses, weights, thresholds, and strategy.
3. Compile the policy rule and apply the candidate pool in memory.
4. Update proxy, health, and metrics writer settings from the candidate.
5. Persist the accepted config snapshot only after the in-memory swap succeeds.

An invalid candidate leaves the prior config running and does not write a new
SQLite snapshot. Removed backends enter
`draining`; they remain available to existing connections until their active
count reaches zero.

## Strategies

- `round_robin`: stable eligible order with a moving index
- `weighted_round_robin`: smooth weighted scheduling; a 5:3:2 pool converges
  precisely on that ratio
- `least_connections`: minimum active count, round robin among ties
- `consistent_hash`: SHA-256 ring lookup by source IP, with
  `weight * virtual_nodes_per_weight` points per backend

Unhealthy, disabled, draining, and retired backends receive no new traffic.
If a selected backend cannot be connected, the proxy records passive
suspicion and retries the remaining eligible backends, excluding each failed
backend. The first selection advances normal strategy state. Retry selections
do not advance round-robin or least-connections indexes, and smooth weighted
round-robin uses scratch state so failed connection attempts do not distort
future scheduling.

The hash ring is a circular ordered graph-like structure: keys and virtual
backend nodes occupy the same hash space, and lookup walks clockwise to the
first node. Adding or removing a backend moves only the neighboring key ranges
rather than reshuffling every client. The ring is built on the event loop and
sized `weight * virtual_nodes_per_weight` per backend, so `weight` (≤ 1000) and
`virtual_nodes_per_weight` (≤ 1024) are bounded at validation to keep
construction cheap and the hot path responsive.

## Connection lifecycle

Each connection passes through explicit states:

```text
ACCEPTED -> SELECTING_BACKEND -> CONNECTING_BACKEND -> RELAYING
          -> HALF_CLOSED_* -> CLOSING -> CLOSED
                                   \-> FAILED
```

The backend active count increments only after a successful connect and
decrements exactly once during cleanup. Cancellation, errors, idle timeout,
failed connects, and forced shutdown all use the same idempotent cleanup path.

## Health and draining

The health checker performs periodic TCP connects. A backend becomes unhealthy
only after the configured consecutive failure threshold and returns only after
the success threshold. Live connect failures add passive suspicion and can
accelerate the same threshold without making a single blip flap the pool.

`backends drain NAME` excludes a backend from new selections while existing
connections continue. Any connections still attached after
`drain_timeout_seconds` are force-closed (reusing the same idempotent cleanup
path as shutdown), so a drain cannot hang forever. A removed backend is
physically dropped from memory after its final connection closes.

## Admin protocol and security

Admin frames are:

```text
4-byte unsigned big-endian length | UTF-8 JSON payload
```

The signed JSON contains `version`, `timestamp`, `nonce`, `command`, and
`args`. Keys are sorted with compact separators and the signature field is
excluded. The server rejects oversized/malformed frames, unsupported versions,
expired timestamps, replayed nonces, unknown commands, invalid argument types,
and bad signatures. Signatures use SHA-256 HMAC and `hmac.compare_digest`.
Responses are also signed with the same HMAC scheme over `version`, `timestamp`,
`nonce`, `ok`, and either `data` or `error`; the CLI verifies every reply and
tracks response nonces so a signed reply cannot be replayed to the client.

Keep `admin.secret` out of source control. `init-secret` writes it with
owner-only permissions (`0600`) where the OS honors them (POSIX; on Windows
rely on NTFS ACLs). The control port is localhost-only by default, but
localhost is not itself an authentication boundary on a shared machine.

## Rule DSL and VM

Example:

```text
if client.ip startswith "10." and command == "status"
then return "consistent_hash"
else return "round_robin"
```

Tag-based example (compiles today; `backend.tags` is populated only when a
future router supplies tag context):

```text
if backend.tags contains "stable"
then return "round_robin"
else return "least_connections"
```

The DSL defines these fields:

- `client.ip`
- `client.port`
- `backend.tag`
- `backend.tags`
- `command`
- `args.backend_id`

Operators are `==`, `startswith`, `contains`, `and`, `or`, and parentheses.
Programs compile to instructions such as `LOAD_FIELD`, `LOAD_CONST`, `EQ`,
`STARTSWITH`, `CONTAINS`, `AND`, `OR`, `JUMP_IF_FALSE`, and `RETURN`. The
instruction limit is configurable and is enforced at config-load time: a
rule that would compile beyond the limit is rejected during validation/reload,
not left to fail at runtime.

On the current Layer 4 connection path, only `client.ip` and `client.port`
are populated (`client.port` as a string, so the string operators apply). The
remaining fields are reserved for future routing contexts and normally resolve
to no value in the current proxy path. A returned strategy name overrides the
configured strategy; any other result, including `"default"` or an unmatched
else-less rule, uses the configured strategy.

## SQLite and metrics

The database contains:

- `config_snapshots` and `backend_config`
- `health_events` and `connection_events`
- `metrics_snapshots`
- `admin_events`, `reload_events`, and `process_events`
- `rule_versions`

Events enter a bounded in-memory queue with non-blocking `put_nowait`, and
critical lifecycle events use a separate intake queue so a flood of connection
events cannot crowd them out. A background writer batches work through
`asyncio.to_thread`, retries temporary SQLite busy errors with backoff, and
drops telemetry under sustained pressure rather than delaying client traffic.
Dropped events, dropped critical events, and dropped snapshots are all counted
and shown in `status`.

`status` serializes at most 100 active connections inline; the true count is
always reported as `connections_total` (with `connections_truncated`) so a
heavily loaded balancer's reply still fits in one control frame. The
`shutdown_in_progress` field indicates graceful shutdown is underway (not
backend drain state; see per-backend `state` in the backends list).

## Tests

The application code has no third-party runtime dependencies. The test suite
(191 tests) uses `unittest` and `pytest` with real asyncio TCP integration
cases. Configuration lives in `pyproject.toml`; dev dependencies are in
`requirements-dev.txt`.

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m pytest tests -v
```

`PYTHONPATH=src` is set automatically via `pyproject.toml` for pytest; unittest
discovery picks up the `tests` package from the repo root after an editable
install.

On CPython 3.13+ on Windows, the default Proactor event loop can surface benign
`ResourceWarning`/teardown noise from sockets accepted during server close;
`tests/__init__.py` switches to the Selector policy on Windows. Pin 3.11–3.12
if you need a warning-free run with `-W error::ResourceWarning`.

Coverage spans algorithms and remapping, config validation, crypto and control
framing, health hysteresis, passive failures, drain/reload retention, HMAC
tamper/expiry/replay cases, rule DSL tokenizer/parser/compiler/VM opcodes,
daemon `handle_command` paths, CLI helpers, store/metrics writer, real proxy
echo traffic, no-backend behavior, signed daemon control, reload rollback,
SQLite persistence, graceful shutdown, and dummy-backend modes. A dedicated
failure-injection suite exercises idle timeout, backends that close immediately
or die mid-relay, transactional reload rollback, and database-pressure drop
behavior.

## Development

Broad compatible ranges live in `pyproject.toml` (`[project.optional-dependencies].dev`).
For reproducible installs, use the locked dev requirements:

```powershell
python -m pip install -e .
python -m pip install -r requirements-dev.lock
```

For day-to-day work with the latest compatible tooling:

```powershell
python -m pip install -r requirements-dev.txt
# or: python -m pip install -e ".[dev]"
```

Regenerate the lock file after changing dev dependency ranges:

```powershell
python -m pip install pip-tools
pip-compile --extra dev pyproject.toml -o requirements-dev.lock
```

Then verify in a fresh environment:

```powershell
ruff check .
mypy src
python -m unittest discover -s tests -v
python -m pytest tests -v
```

Ruff and mypy run clean on `src/`. GitHub Actions
([.github/workflows/ci.yml](.github/workflows/ci.yml)) installs from
`requirements-dev.lock`, then runs ruff, mypy, unittest, and pytest on CPython
3.11, 3.12, and 3.13.

The non-obvious design decisions are recorded as ADRs in
[docs/adr/](docs/adr/) (concurrency model, L4 core, off-hot-path persistence,
the rule DSL/VM, the control protocol, and transactional reload). Original and
revised capstone scope documents live at [load_balancer_scope.txt](load_balancer_scope.txt)
and [revised_load_balancer_scope.txt](revised_load_balancer_scope.txt).

The SQLite schema is versioned in `schema_meta`. `store.py` carries a
`SCHEMA_VERSION` and a forward-only `MIGRATIONS` registry applied in order on
`initialize()`, so the schema can evolve without losing existing databases.

## Why asyncio

Asyncio fits a proxy whose dominant work is waiting on sockets. It provides
stream backpressure via `drain()`, explicit task cancellation, monotonic
timeouts, and one-owner runtime state without a lock around every counter.

- Raw `selectors` could reduce abstraction overhead and provide tighter
  transport control, but would require a custom task/state-machine framework.
- A thread per connection is familiar and makes blocking APIs easy, but uses
  more memory, adds scheduler/locking complexity, and makes cancellation and
  exact counter cleanup harder under load.

This project deliberately uses one coherent asyncio model rather than mixing
all three.

## Honest limitations

- Not hardened against DDoS or hostile internet traffic
- No production TLS termination or PKI workflow
- No kernel/socket tuning, zero-copy forwarding, or multi-process workers
- No HTTP parsing or per-request Layer 7 balancing
- Runtime admin changes are in-memory until the TOML file is changed; reload
  reapplies TOML (strategy, enabled backends) but preserves admin drain and
  unhealthy state unless the file explicitly disables a backend
- Rule DSL fields other than `client.ip` and `client.port` compile but are
  not populated on the current L4 connection path unless a future router
  supplies that context
- `balancer.max_connections` (0 = unlimited) caps simultaneous client connections;
  lowering the limit on reload does not disconnect existing clients
- Reload rejects duplicate or reserved backend addresses on updates, not only on add
- Mutating admin commands are rejected once graceful shutdown has started
- `backends disable` force-closes lingering connections after `drain_timeout_seconds`
- Reload rejects backend address changes while connections are still active
- Telemetry tables are retained to 100,000 rows per table (config snapshots: 50)
- Listener/control rebinding requires restart
- SQLite is appropriate for capstone history, not high-volume telemetry
- Detached mode is a convenience, not a full Windows Service or systemd unit

