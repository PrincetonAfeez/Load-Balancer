"""Long-lived process lifecycle, PID handling, signals, reload, and admin actions."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import logging
import os
from pathlib import Path
import signal
from types import FrameType
from typing import Any

from .config_parser import (
    MAX_BACKEND_WEIGHT,
    STRATEGIES,
    AppConfig,
    BackendConfig,
    load_config,
)
from .control import ControlServer
from .crypto import load_admin_secret
from .errors import ConfigError
from .health import HealthChecker
from .metrics import Metrics
from .pool import BackendPool
from .proxy import TCPProxy
from .rule_dsl import compile_rule, non_l4_fields_in_rule, rule_can_override_strategy
from .store import MetricsWriter, SQLiteStore

LOG = logging.getLogger(__name__)

# Status serializes one entry per active connection; cap the list so the reply
# stays within a single control frame even under heavy load. The true count is
# always reported separately as "connections_total".
MAX_STATUS_CONNECTIONS = 100


class PIDFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.acquired = False

    def acquire(self) -> None:
        if self.path.exists():
            try:
                existing_pid = int(self.path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                existing_pid = 0
            if existing_pid and self._is_alive(existing_pid):
                raise RuntimeError(
                    f"load balancer already running with PID {existing_pid}"
                )
            self.path.unlink(missing_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.path.exists():
                content = self.path.read_text(encoding="utf-8").strip()
                if content == str(os.getpid()):
                    self.path.unlink()
        finally:
            self.acquired = False

    @staticmethod
    def _is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Cannot signal-check another user's process; treat as stale.
            return False
        except OSError:
            return False


class LoadBalancerDaemon:
    def __init__(self, config_path: str | Path) -> None:
        self.config_path = str(Path(config_path).resolve())
        self.config: AppConfig | None = None
        self.pool: BackendPool | None = None
        self.metrics: Metrics | None = None
        self.store: SQLiteStore | None = None
        self.proxy: TCPProxy | None = None
        self.health: HealthChecker | None = None
        self.writer: MetricsWriter | None = None
        self.control: ControlServer | None = None
        self.pid_file: PIDFile | None = None
        self._shutdown = asyncio.Event()
        self._shutdown_reason = "unknown"
        self._reload_lock = asyncio.Lock()
        self._background_tasks: list[asyncio.Task[None]] = []
        self._drain_tasks: set[asyncio.Task[None]] = set()
        self._drain_deadline_tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        config = load_config(self.config_path)
        secret = load_admin_secret(config.crypto.secret_file)
        self.config = config
        self._warn_non_loopback(config)
        non_l4_fields = non_l4_fields_in_rule(config.rules.source)
        if non_l4_fields:
            LOG.info(
                "rule references non-L4 fields %s; on the connection hot path "
                "they resolve to nothing unless a future admin/L7 router supplies them",
                ", ".join(non_l4_fields),
            )
        self.pid_file = PIDFile(config.balancer.pid_file)
        self.pid_file.acquire()

        try:
            self.pool = BackendPool.from_configs(config.backends)
            self.metrics = Metrics(config.metrics.queue_size)
            self.store = SQLiteStore(config.metrics.database_path)
            await asyncio.to_thread(self.store.initialize)
            compiled = compile_rule(config.rules.source)
            await asyncio.to_thread(
                self.store.save_config_snapshot,
                config,
                self.config_path,
                [
                    {"op": instruction.op.value, "arg": instruction.arg}
                    for instruction in compiled
                ],
            )

            self.proxy = TCPProxy(config, self.pool, self.metrics)
            self.health = HealthChecker(self.pool, config.health, self.metrics)
            self.writer = MetricsWriter(
                self.store,
                self.metrics,
                self.pool,
                config.metrics.flush_interval_seconds,
                config.metrics.snapshot_interval_seconds,
                config.metrics.batch_size,
            )
            self.control = ControlServer(
                config.control.host,
                config.control.port,
                secret,
                config.control.max_frame_bytes,
                config.control.max_clock_skew_seconds,
                self.handle_command,
                self.metrics,
            )

            await self.proxy.start()
            await self.control.start()
            self._install_signal_handlers()
            self.metrics.emit(
                "process_started",
                {
                    "pid": os.getpid(),
                    "listener": f"{config.listener.host}:{config.listener.port}",
                },
                critical=True,
            )
            self._background_tasks = [
                asyncio.create_task(self.health.run(), name="health-checker"),
                asyncio.create_task(self.writer.run(), name="metrics-writer"),
            ]
            LOG.info("load balancer started with PID %s", os.getpid())
            await self._shutdown.wait()
            await self._graceful_shutdown()
        except Exception:
            # Startup failed partway (e.g. control port already in use after the
            # proxy bound). Tear down whatever started before re-raising.
            await self._abort_startup()
            raise
        finally:
            if self.pid_file is not None:
                self.pid_file.release()

    async def _abort_startup(self) -> None:
        # Best-effort: never let teardown raise and mask the original startup error.
        LOG.error("tearing down partially started load balancer")
        try:
            for task in self._background_tasks:
                task.cancel()
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
                self._background_tasks = []
            for task in list(self._drain_tasks):
                task.cancel()
            if self._drain_tasks:
                await asyncio.gather(*self._drain_tasks, return_exceptions=True)
            if self.control is not None:
                await self.control.close()
            if self.proxy is not None:
                await self.proxy.stop_accepting()
        except Exception:
            LOG.exception("error while aborting startup (ignored)")

    @staticmethod
    def _warn_non_loopback(config: AppConfig) -> None:
        def is_loopback(host: str) -> bool:
            return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")

        for label, host in (
            ("listener", config.listener.host),
            ("control", config.control.host),
        ):
            if not is_loopback(host):
                LOG.warning(
                    "%s bound to non-loopback address %s; this project's security "
                    "model assumes localhost-only access",
                    label,
                    host,
                )

    def request_shutdown(self, reason: str) -> None:
        if not self._shutdown.is_set():
            self._shutdown_reason = reason
            self._shutdown.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def make_fallback(name: str):
            # Hop back onto the loop thread rather than touching the
            # asyncio.Event from the C-level signal handler.
            def _handler(_signum: int, _frame: FrameType | None) -> None:
                loop.call_soon_threadsafe(self.request_shutdown, name.lower())

            return _handler

        # SIGBREAK is Windows-only (Ctrl+Break / detached process groups).
        for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            signum = getattr(signal, signal_name, None)
            if signum is None:
                continue
            try:
                loop.add_signal_handler(
                    signum, self.request_shutdown, signal_name.lower()
                )
            except (NotImplementedError, RuntimeError):
                signal.signal(signum, make_fallback(signal_name))
        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            try:
                loop.add_signal_handler(sighup, self._trigger_reload)
            except (NotImplementedError, RuntimeError):
                LOG.info(
                    "SIGHUP reload is not available on this platform; use the CLI reload command"
                )

    def _trigger_reload(self) -> None:
        if self._shutdown.is_set():
            LOG.warning("ignoring reload request because shutdown is in progress")
            return
        task = asyncio.create_task(self.reload())
        task.add_done_callback(self._reload_task_done)

    @staticmethod
    def _reload_task_done(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            LOG.error("SIGHUP reload failed: %s", exc)

    async def _graceful_shutdown(self) -> None:
        assert self.config and self.proxy and self.health and self.writer
        assert self.control and self.metrics
        # Wait for any in-flight reload before tearing down shared pool/proxy state.
        async with self._reload_lock:
            LOG.info("graceful shutdown started: %s", self._shutdown_reason)
        self.metrics.emit(
            "process_shutdown_started",
            {"reason": self._shutdown_reason},
            critical=True,
        )
        await self.control.close()
        self.health.stop()
        for task in list(self._drain_tasks):
            task.cancel()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        await self.proxy.drain(self.config.balancer.drain_timeout_seconds)
        self.metrics.emit(
            "process_shutdown_finished",
            {"reason": self._shutdown_reason},
            critical=True,
        )
        for task in self._background_tasks:
            if task.get_name() == "health-checker":
                await asyncio.gather(task, return_exceptions=True)
        self.writer.stop()
        for task in self._background_tasks:
            await asyncio.gather(task, return_exceptions=True)
        LOG.info("graceful shutdown complete")

    async def reload(self) -> dict[str, Any]:
        async with self._reload_lock:
            assert self.config and self.pool and self.proxy and self.health
            assert self.metrics and self.store
            if self._shutdown.is_set():
                raise ValueError("shutdown in progress; reload rejected")
            try:
                candidate = await asyncio.to_thread(load_config, self.config_path)
                self._reject_reload_during_shutdown()
                # Listener/control rebinding is intentionally a restart-only operation.
                if candidate.listener != self.config.listener:
                    raise ConfigError(
                        "listener address changes require a process restart"
                    )
                if candidate.control != self.config.control:
                    raise ConfigError(
                        "control socket settings changes require a process restart"
                    )
                if (
                    candidate.metrics.database_path
                    != self.config.metrics.database_path
                ):
                    raise ConfigError(
                        "metrics database_path changes require a process restart"
                    )
                if candidate.metrics.queue_size != self.config.metrics.queue_size:
                    raise ConfigError(
                        "metrics queue_size changes require a process restart"
                    )
                # Recompile for the post-reload snapshot; persist only after the
                # in-memory swap succeeds so the DB never records a rejected candidate.
                compiled = compile_rule(candidate.rules.source)
                compiled_rule = [
                    {"op": instruction.op.value, "arg": instruction.arg}
                    for instruction in compiled
                ]
                self._reject_reload_during_shutdown()
                result = self.pool.apply_configs(
                    candidate.backends,
                    reserved_addresses=frozenset(
                        {
                            (candidate.listener.host, candidate.listener.port),
                            (candidate.control.host, candidate.control.port),
                        }
                    ),
                )
                for backend_id in result["draining"]:
                    self._schedule_connection_deadline(backend_id)
                self.proxy.update_config(candidate)
                self.health.update_config(candidate.health)
                if self.writer is not None:
                    self.writer.flush_interval = (
                        candidate.metrics.flush_interval_seconds
                    )
                    self.writer.snapshot_interval = (
                        candidate.metrics.snapshot_interval_seconds
                    )
                    self.writer.batch_size = candidate.metrics.batch_size
                self.config = candidate
                self._reject_reload_during_shutdown()
                await asyncio.to_thread(
                    self.store.save_config_snapshot,
                    candidate,
                    self.config_path,
                    compiled_rule,
                )
                self.metrics.emit(
                    "reload_success",
                    {"changes": result},
                    critical=True,
                )
                LOG.info("configuration reloaded: %s", result)
                return {"reloaded": True, "changes": result}
            except Exception as exc:
                self.metrics.emit(
                    "reload_failure",
                    {"error": str(exc)},
                    critical=True,
                )
                LOG.error("configuration reload rejected: %s", exc)
                raise

    async def handle_command(
        self, command: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        assert self.config and self.pool and self.proxy and self.metrics
        if self._shutdown.is_set() and command not in {"status", "stop"}:
            raise ValueError("shutdown in progress; mutating commands rejected")
        if command == "status":
            return self.status()
        if command == "stop":
            self.request_shutdown("admin")
            return {"stopping": True}
        if command == "reload":
            return await self.reload()
        if command == "backends.list":
            return {"backends": [backend.status() for backend in self.pool.all()]}
        if command == "backends.add":
            backend_config = self._backend_from_args(args)
            self._reject_reserved_address(backend_config.host, backend_config.port)
            for existing in self.pool.all():
                if (existing.host, existing.port) == (
                    backend_config.host,
                    backend_config.port,
                ):
                    raise ValueError(
                        "backend address already in use: "
                        f"{backend_config.host}:{backend_config.port}"
                    )
            added = self.pool.add(backend_config)
            self.metrics.emit(
                "admin_backend_added", {"backend_id": added.id}, critical=True
            )
            return {"backend": added.status(), "persistence": "until next reload"}
        if command in {
            "backends.remove",
            "backends.enable",
            "backends.disable",
            "backends.drain",
        }:
            backend_id = self._require_backend_id(args)
            action = command.split(".")[1]
            try:
                if action == "remove":
                    backend = self.pool.remove(backend_id)
                elif action == "enable":
                    backend = self.pool.enable(backend_id)
                    self._cancel_connection_deadline(backend_id)
                elif action == "disable":
                    backend = self.pool.disable(backend_id)
                    self._cancel_connection_deadline(backend_id)
                    self._schedule_connection_deadline(backend_id)
                else:
                    backend = self.pool.drain(backend_id)
            except KeyError as exc:
                raise ValueError(f"unknown backend: {backend_id}") from exc
            if action in {"drain", "remove"}:
                self._schedule_connection_deadline(backend_id)
            self.metrics.emit(
                f"admin_backend_{action}",
                {"backend_id": backend_id},
                critical=True,
            )
            return {"backend": backend.status()}
        if command == "strategy.get":
            assert self.config is not None
            return {
                "strategy": self.proxy.strategy_name,
                "configured_strategy": self.config.balancer.strategy,
                "rule_source": self.config.rules.source,
                "rule_can_override_strategy": rule_can_override_strategy(
                    self.config.rules.source
                ),
            }
        if command == "strategy.set":
            name = args.get("name")
            if not isinstance(name, str) or name not in STRATEGIES:
                raise ValueError(
                    f"strategy must be one of: {', '.join(sorted(STRATEGIES))}"
                )
            self.proxy.set_strategy(name)
            self.metrics.emit(
                "admin_strategy_set", {"strategy": name}, critical=True
            )
            return {"strategy": name, "persistence": "until next reload"}
        raise ValueError(f"unsupported command: {command}")

    def _reject_reload_during_shutdown(self) -> None:
        if self._shutdown.is_set():
            raise ValueError("shutdown in progress; reload rejected")

    def _cancel_connection_deadline(self, backend_id: str) -> None:
        task = self._drain_deadline_tasks.pop(backend_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_connection_deadline(self, backend_id: str) -> None:
        """Force-close lingering connections after the drain timeout."""
        assert self.config is not None
        self._cancel_connection_deadline(backend_id)
        timeout = self.config.balancer.drain_timeout_seconds
        task = asyncio.create_task(self._connection_deadline(backend_id, timeout))
        self._drain_deadline_tasks[backend_id] = task
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_task_done)

    def _drain_task_done(self, task: asyncio.Task[None]) -> None:
        self._drain_tasks.discard(task)
        for backend_id, tracked in list(self._drain_deadline_tasks.items()):
            if tracked is task:
                self._drain_deadline_tasks.pop(backend_id, None)
                break

    async def _connection_deadline(self, backend_id: str, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        if self.pool is None or self.proxy is None:
            return
        backend = self.pool.get(backend_id)
        if backend is None or backend.active_connections == 0:
            return
        closed = self.proxy.force_close_backend(backend_id)
        self.pool.prune_retired()
        if closed and self.metrics is not None:
            self.metrics.emit(
                "admin_connection_timeout",
                {"backend_id": backend_id, "closed": closed},
                critical=True,
            )
            LOG.info(
                "connection timeout reached for %s; force-closed %s connection(s)",
                backend_id,
                closed,
            )

    def status(self) -> dict[str, Any]:
        assert self.config and self.pool and self.proxy and self.metrics
        records = list(self.proxy.connections.values())
        connections = [
            {
                "connection_id": record.connection_id,
                "client": record.client_address,
                "backend_id": record.backend_id,
                "state": record.state.value,
                "bytes_client_to_backend": record.bytes_client_to_backend,
                "bytes_backend_to_client": record.bytes_backend_to_client,
            }
            for record in records[:MAX_STATUS_CONNECTIONS]
        ]
        return {
            "pid": os.getpid(),
            "listener": {
                "host": self.proxy.address[0],
                "port": self.proxy.address[1],
            },
            "control": asdict(self.config.control),
            "strategy": self.proxy.strategy_name,
            "configured_strategy": self.config.balancer.strategy,
            "rule_source": self.config.rules.source,
            "rule_can_override_strategy": rule_can_override_strategy(
                self.config.rules.source
            ),
            "shutdown_in_progress": self._shutdown.is_set(),
            "max_connections": self.config.balancer.max_connections,
            "connection_slots_in_use": self.proxy.connection_slots_in_use,
            "metrics": self.metrics.snapshot(),
            "connections": connections,
            "connections_total": len(records),
            "connections_truncated": len(records) > len(connections),
            "backends": [backend.status() for backend in self.pool.all()],
        }

    def _reject_reserved_address(self, host: str, port: int) -> None:
        assert self.config is not None
        if (host, port) == (
            self.config.listener.host,
            self.config.listener.port,
        ):
            raise ValueError("backend address collides with the listener address")
        if (host, port) == (self.config.control.host, self.config.control.port):
            raise ValueError("backend address collides with the control address")

    @staticmethod
    def _require_backend_id(args: dict[str, Any]) -> str:
        backend_id = args.get("backend_id")
        if not isinstance(backend_id, str) or not backend_id:
            raise ValueError("backend_id must be a non-empty string")
        return backend_id

    @staticmethod
    def _backend_from_args(args: dict[str, Any]) -> BackendConfig:
        name = args.get("name")
        host = args.get("host")
        port = args.get("port")
        weight = args.get("weight", 1)
        tags = args.get("tags", [])
        if not isinstance(name, str) or not name:
            raise ValueError("backend name must be a non-empty string")
        if not isinstance(host, str) or not host:
            raise ValueError("backend host must be a non-empty string")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("backend port must be between 1 and 65535")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError("backend weight must be a positive integer")
        if weight > MAX_BACKEND_WEIGHT:
            raise ValueError(
                f"backend weight must not exceed {MAX_BACKEND_WEIGHT}"
            )
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("backend tags must be a list of strings")
        return BackendConfig(name, host, port, weight, True, tuple(tags))


async def run_daemon(config_path: str | Path) -> None:
    daemon = LoadBalancerDaemon(config_path)
    await daemon.run()
