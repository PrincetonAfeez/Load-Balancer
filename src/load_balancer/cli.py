"""Command-line interface for daemon, admin, persistence, and demo tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from . import __version__
from .config_parser import STRATEGIES, load_config
from .control import ControlClient
from .crypto import generate_secret, load_admin_secret
from .daemon import run_daemon
from .demo_tools import hold_open, run_dummy_backend, send_messages
from .errors import LoadBalancerError
from .store import SQLiteStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="load-balancer",
        description="Educational asyncio Layer 4 TCP load balancer",
        epilog=(
            "exit codes:\n"
            "  0  success\n"
            "  1  known error (bad config, admin/IO failure, invalid argument)\n"
            "  2  command-line usage error (argparse)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="load-balancer.toml", help="TOML config file"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging level for foreground daemon and detached child (default: INFO)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "init-db",
        help="initialize the SQLite schema",
        description="Create or migrate the metrics database schema on disk.",
    )
    secret = subparsers.add_parser(
        "init-secret",
        help="create an admin secret",
        description="Write a new HMAC admin secret to the configured secret file.",
    )
    secret.add_argument("--force", action="store_true", help="replace existing secret")

    start = subparsers.add_parser(
        "start",
        help="start the load balancer",
        description="Run the daemon in the foreground or spawn a detached process.",
    )
    mode = start.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--foreground", action="store_true", help="run attached to this terminal"
    )
    mode.add_argument(
        "--daemon", action="store_true", help="spawn a detached background process"
    )
    subparsers.add_parser(
        "stop",
        help="gracefully stop the daemon",
        description="Request graceful shutdown through the admin control socket.",
    )
    subparsers.add_parser(
        "reload",
        help="transactionally reload the config",
        description="Parse load-balancer.toml and apply a transactional in-memory reload.",
    )
    subparsers.add_parser(
        "status",
        help="show live daemon status",
        description="Query runtime status, backends, and metrics from the daemon.",
    )

    backends = subparsers.add_parser(
        "backends",
        help="manage runtime backends",
        description="Inspect or mutate backend pool state through the admin socket.",
    )
    backend_sub = backends.add_subparsers(dest="backend_command", required=True)
    backend_sub.add_parser(
        "list",
        help="list configured and runtime backends",
        description="List all current backends and their runtime state.",
    )
    add = backend_sub.add_parser(
        "add",
        help="add a runtime backend",
        description="Register a new backend in the running daemon until the next reload.",
    )
    add.add_argument("name", help="backend display name")
    add.add_argument("host", help="backend host address")
    add.add_argument("port", type=int, help="backend TCP port")
    add.add_argument("--weight", type=int, default=1, help="selection weight (default: 1)")
    add.add_argument(
        "--tag",
        action="append",
        default=[],
        help="optional tag (repeatable)",
    )
    backend_help = {
        "remove": (
            "remove a runtime backend",
            "Remove a backend from the pool; lingering connections drain first.",
        ),
        "enable": (
            "enable a backend for new traffic",
            "Mark a backend eligible for new selections again.",
        ),
        "disable": (
            "disable a backend from new traffic",
            "Exclude a backend from new selections without dropping active connections.",
        ),
        "drain": (
            "drain a backend gracefully",
            "Stop selecting a backend for new connections while existing ones finish.",
        ),
    }
    for action, (help_text, description) in backend_help.items():
        action_parser = backend_sub.add_parser(
            action, help=help_text, description=description
        )
        action_parser.add_argument(
            "backend_id", help="backend identifier from status or backends list"
        )

    strategy = subparsers.add_parser(
        "strategy",
        help="inspect or change strategy",
        description="Read or change the active balancing strategy at runtime.",
    )
    strategy_sub = strategy.add_subparsers(dest="strategy_command", required=True)
    strategy_sub.add_parser(
        "get",
        help="show the active and configured strategy",
        description="Show active strategy, configured strategy, and rule source.",
    )
    strategy_set = strategy_sub.add_parser(
        "set",
        help="change the active strategy",
        description="Set the runtime strategy until the next reload.",
    )
    strategy_set.add_argument(
        "name",
        choices=sorted(STRATEGIES),
        help="balancing strategy name",
    )

    metrics = subparsers.add_parser(
        "metrics",
        help="query persisted metrics",
        description="Read historical metrics and events from SQLite.",
    )
    metrics_sub = metrics.add_subparsers(dest="metrics_command", required=True)
    metrics_sub.add_parser(
        "summary",
        help="show the most recent persisted metrics snapshot",
        description="Print the latest metrics snapshot stored in SQLite.",
    )
    history = metrics_sub.add_parser(
        "health-history",
        help="show recent backend health transitions",
        description="List recent health check transitions from SQLite.",
    )
    history.add_argument(
        "--limit", type=int, default=50, help="maximum rows to return (default: 50)"
    )
    routing = metrics_sub.add_parser(
        "routing-history",
        help="show recent routing/connection events",
        description="List recent connection routing events from SQLite.",
    )
    routing.add_argument(
        "--limit", type=int, default=50, help="maximum rows to return (default: 50)"
    )

    config = subparsers.add_parser(
        "config",
        help="configuration utilities",
        description="Validate configuration without starting the daemon.",
    )
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser(
        "validate",
        help="parse and validate the TOML config",
        description="Load, type-check, and compile rules from the config file.",
    )

    dummy = subparsers.add_parser(
        "dummy-backend",
        help="run a local test backend",
        description="Start a standalone echo/slow/flaky test backend for local demos.",
    )
    dummy.add_argument(
        "mode",
        choices=["echo", "slow", "flaky", "close-immediately"],
        help="backend behavior mode",
    )
    dummy.add_argument(
        "--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)"
    )
    dummy.add_argument("--port", type=int, required=True, help="listen port")
    dummy.add_argument("--name", help="optional label printed in logs")
    dummy.add_argument(
        "--delay-ms",
        type=int,
        default=500,
        help="slow-mode response delay in milliseconds (default: 500)",
    )
    dummy.add_argument(
        "--fail-rate",
        type=float,
        default=0.3,
        help="flaky-mode close probability from 0.0 to 1.0 (default: 0.3)",
    )

    client = subparsers.add_parser(
        "lb-client",
        help="send traffic through a balancer",
        description="Generate test traffic against a running load balancer.",
    )
    client_sub = client.add_subparsers(dest="client_command", required=True)
    send = client_sub.add_parser(
        "send",
        help="send one or more messages through the balancer",
        description="Open TCP connections, send payloads, and print echoed replies.",
    )
    send.add_argument(
        "--host", default="127.0.0.1", help="balancer host (default: 127.0.0.1)"
    )
    send.add_argument(
        "--port", type=int, default=8080, help="balancer port (default: 8080)"
    )
    send.add_argument(
        "--message", default="hello", help="payload to send (default: hello)"
    )
    send.add_argument(
        "--count", type=int, default=1, help="number of connections (default: 1)"
    )
    held = client_sub.add_parser(
        "hold-open",
        help="keep a connection open for a duration",
        description="Hold one client connection open to test concurrency or drain.",
    )
    held.add_argument(
        "--host", default="127.0.0.1", help="balancer host (default: 127.0.0.1)"
    )
    held.add_argument(
        "--port", type=int, default=8080, help="balancer port (default: 8080)"
    )
    held.add_argument(
        "--seconds",
        type=float,
        default=60,
        help="seconds to keep the connection open (default: 60)",
    )
    return parser


def _load_secret(path: str) -> str:
    return load_admin_secret(path)


async def _admin(args: argparse.Namespace, command: str, data: dict[str, Any] | None = None):
    config = load_config(args.config)
    secret = _load_secret(config.crypto.secret_file)
    client = ControlClient(
        config.control.host,
        config.control.port,
        secret,
        config.control.max_frame_bytes,
        max_clock_skew_seconds=config.control.max_clock_skew_seconds,
    )
    return await client.command(command, data)


def _print_status(data: dict[str, Any]) -> None:
    metrics = data["metrics"]
    listener = data["listener"]
    print(f"PID:       {data['pid']}")
    print(f"Listener:  {listener['host']}:{listener['port']}")
    if data.get("shutdown_in_progress"):
        print("Shutdown:  in progress")
    strategy = data.get("strategy", "?")
    configured = data.get("configured_strategy")
    if configured and configured != strategy:
        print(f"Strategy:  {strategy} (configured: {configured})")
    else:
        print(f"Strategy:  {strategy}")
    if data.get("rule_can_override_strategy"):
        print("Rule:      may override configured strategy")
    max_connections = data.get("max_connections")
    slots_in_use = data.get("connection_slots_in_use")
    if isinstance(max_connections, int) and max_connections > 0:
        print(
            f"Slots:     {slots_in_use} in use / {max_connections} max "
            "(existing connections may exceed a lowered limit until they close)"
        )
    print(f"Uptime:    {metrics['uptime_seconds']:.1f}s")
    print(
        f"Connections: {metrics['active_connections']} active / "
        f"{data.get('connections_total', '?')} open / "
        f"{metrics['total_connections']} lifetime"
    )
    print(
        f"Bytes:     {metrics['bytes_client_to_backend']} in / "
        f"{metrics['bytes_backend_to_client']} out"
    )
    print(
        f"Dropped:   {metrics.get('dropped_events', 0)} events / "
        f"{metrics.get('dropped_critical_events', 0)} critical"
    )
    if data.get("connections_truncated"):
        print(f"(showing {len(data['connections'])} of {data['connections_total']} connections)")
    print()
    print(
        f"{'BACKEND':16} {'ADDRESS':22} {'STATE':10} "
        f"{'ACTIVE':>6} {'TOTAL':>7} {'WEIGHT':>6} "
        f"{'BYTES_IN':>10} {'BYTES_OUT':>10} {'OK':>4} {'FAIL':>4} {'LAST_CHK':>9}"
    )
    now = time.time()
    for backend in data["backends"]:
        address = f"{backend['host']}:{backend['port']}"
        last = backend.get("last_health_check")
        last_str = f"{now - last:.1f}s" if isinstance(last, (int, float)) else "never"
        print(
            f"{backend['id'][:16]:16} {address[:22]:22} "
            f"{backend['state']:10} {backend['active']:6} "
            f"{backend['total']:7} {backend['weight']:6} "
            f"{backend['bytes_in']:10} {backend['bytes_out']:10} "
            f"{backend['success_streak']:4} {backend['failure_streak']:4} "
            f"{last_str:>9}"
        )


def _print_strategy_get(data: dict[str, Any]) -> None:
    print(f"Strategy:  {data.get('strategy', '?')}")
    configured = data.get("configured_strategy")
    strategy = data.get("strategy")
    if configured and configured != strategy:
        print(f"Configured: {configured}")
    if data.get("rule_can_override_strategy"):
        print("Rule:      may override configured strategy")
    source = data.get("rule_source")
    if isinstance(source, str):
        print(f"Source:    {source!r}")


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    elif isinstance(data, dict) and "backends" in data and len(data) == 1:
        for backend in data["backends"]:
            print(
                f"{backend['id']:20} {backend['host']}:{backend['port']} "
                f"{backend['state']:12} active={backend['active']} "
                f"total={backend['total']} weight={backend['weight']}"
            )
    elif isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    else:
        print(data)


async def _spawn_daemon(args: argparse.Namespace) -> dict[str, Any]:
    # Validate the config and load the secret in this process so obvious failures
    # surface here rather than vanishing into the detached child's discarded output.
    config = load_config(args.config)
    secret = _load_secret(config.crypto.secret_file)
    command = [
        sys.executable,
        "-m",
        "load_balancer",
        "--config",
        str(Path(args.config).resolve()),
        "--log-level",
        args.log_level,
        "start",
        "--foreground",
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(Path(args.config).resolve().parent),
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)

    # Confirm the child actually came up instead of reporting optimistic success.
    # A real signed status both proves readiness and avoids logging a spurious
    # rejected-admin event on the new daemon.
    probe = ControlClient(
        config.control.host, config.control.port, secret,
        config.control.max_frame_bytes, timeout=1.0,
        max_clock_skew_seconds=config.control.max_clock_skew_seconds,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline:
        if process.poll() is not None:
            raise LoadBalancerError(
                f"daemon exited immediately (exit code {process.returncode}); "
                "run 'start --foreground' to see the error"
            )
        try:
            await probe.command("status")
            return {"started": True, "pid": process.pid, "mode": "daemon"}
        except OSError:
            await asyncio.sleep(0.1)
        except (LoadBalancerError, TimeoutError):
            # Control answered (so the daemon is up) but the round trip hiccuped.
            return {"started": True, "pid": process.pid, "mode": "daemon"}
    return {
        "started": True,
        "pid": process.pid,
        "mode": "daemon",
        "warning": "control socket not confirmed within 5s",
    }


async def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "init-db":
        config = load_config(args.config)
        store = SQLiteStore(config.metrics.database_path)
        await asyncio.to_thread(store.initialize)
        return {"initialized": str(Path(store.path).resolve())}
    if args.command == "init-secret":
        config = load_config(args.config)
        path = Path(config.crypto.secret_file)
        if path.exists() and not args.force:
            raise LoadBalancerError(
                f"secret already exists at {path}; pass --force to replace it"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generate_secret() + "\n", encoding="utf-8")
        # Restrict to owner-only where the OS honors it (no-op effect on Windows).
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {"created": str(path.resolve())}
    if args.command == "start":
        if args.daemon:
            return await _spawn_daemon(args)
        await run_daemon(args.config)
        return {"stopped": True}
    if args.command in {"stop", "reload", "status"}:
        data = await _admin(args, args.command)
        if args.command == "status" and not args.json:
            _print_status(data)
            return None
        return data
    if args.command == "backends":
        command = f"backends.{args.backend_command}"
        payload: dict[str, Any] = {}
        if args.backend_command == "add":
            payload = {
                "name": args.name,
                "host": args.host,
                "port": args.port,
                "weight": args.weight,
                "tags": args.tag,
            }
        elif args.backend_command != "list":
            payload = {"backend_id": args.backend_id}
        return await _admin(args, command, payload)
    if args.command == "strategy":
        payload = {"name": args.name} if args.strategy_command == "set" else {}
        data = await _admin(args, f"strategy.{args.strategy_command}", payload)
        if args.strategy_command == "get" and not args.json:
            _print_strategy_get(data)
            return None
        return data
    if args.command == "metrics":
        config = load_config(args.config)
        store = SQLiteStore(config.metrics.database_path)
        if args.metrics_command == "summary":
            return await asyncio.to_thread(store.metrics_summary)
        if args.limit <= 0:
            raise LoadBalancerError("--limit must be a positive integer")
        if args.metrics_command == "routing-history":
            return await asyncio.to_thread(store.routing_history, args.limit)
        return await asyncio.to_thread(store.health_history, args.limit)
    if args.command == "config":
        config = load_config(args.config)
        return {
            "valid": True,
            "backends": len(config.backends),
            "strategy": config.balancer.strategy,
        }
    if args.command == "dummy-backend":
        if not 0 <= args.fail_rate <= 1:
            raise LoadBalancerError("--fail-rate must be between 0 and 1")
        await run_dummy_backend(
            args.mode,
            args.host,
            args.port,
            delay_ms=args.delay_ms,
            fail_rate=args.fail_rate,
            name=args.name,
        )
        return None
    if args.command == "lb-client":
        if args.client_command == "send":
            results = await send_messages(
                args.host, args.port, args.message, args.count
            )
            if args.json:
                return results
            for result in results:
                marker = "" if result.get("complete", True) else " [partial]"
                print(
                    f"#{result['index']}: {result['response']!r}{marker} "
                    f"({result['latency_ms']:.2f} ms)"
                )
            return None
        await hold_open(args.host, args.port, args.seconds)
        return None
    raise LoadBalancerError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        result = asyncio.run(dispatch(args))
        if result is not None:
            _emit(result, args.json)
    except KeyboardInterrupt:
        pass
    except (LoadBalancerError, OSError, ValueError, RuntimeError, EOFError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()

