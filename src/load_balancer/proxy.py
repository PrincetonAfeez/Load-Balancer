"""Asyncio TCP listener, backend connection, and bidirectional relay hot path."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
import logging
import time
import uuid

from .config_parser import STRATEGIES, AppConfig
from .errors import BackendConnectError, NoHealthyBackendError
from .metrics import Metrics
from .pool import Backend, BackendPool
from .rule_dsl import compile_rule
from .strategies import BaseStrategy, ConnectionContext, create_strategy
from .vm import VirtualMachine

LOG = logging.getLogger(__name__)
BUFFER_SIZE = 64 * 1024


class ConnectionState(StrEnum):
    ACCEPTED = "accepted"
    SELECTING_BACKEND = "selecting_backend"
    CONNECTING_BACKEND = "connecting_backend"
    RELAYING = "relaying"
    HALF_CLOSED_CLIENT = "half_closed_client"
    HALF_CLOSED_BACKEND = "half_closed_backend"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(slots=True)
class ConnectionRecord:
    connection_id: str
    client_address: str
    accepted_monotonic: float
    last_activity_monotonic: float
    state: ConnectionState = ConnectionState.ACCEPTED
    backend_id: str | None = None
    bytes_client_to_backend: int = 0
    bytes_backend_to_client: int = 0
    close_reason: str | None = None
    error_summary: str | None = None
    accounted: bool = False
    cleaned: bool = False


class TCPProxy:
    def __init__(
        self, config: AppConfig, pool: BackendPool, metrics: Metrics
    ) -> None:
        self.config = config
        self.pool = pool
        self.metrics = metrics
        self.server: asyncio.AbstractServer | None = None
        self.connections: dict[str, ConnectionRecord] = {}
        self._conn_tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._strategies: dict[str, BaseStrategy] = {}
        self.strategy_name = config.balancer.strategy
        self._rule_program = compile_rule(config.rules.source)
        self._rule_vm = VirtualMachine(config.rules.max_instructions)
        self._closing = False
        self._connection_slots_in_use = 0

    def _try_acquire_connection_slot(self) -> bool:
        limit = self.config.balancer.max_connections
        if limit <= 0:
            return True
        if self._connection_slots_in_use >= limit:
            return False
        self._connection_slots_in_use += 1
        return True

    def _release_connection_slot(self) -> None:
        if self.config.balancer.max_connections > 0:
            self._connection_slots_in_use = max(0, self._connection_slots_in_use - 1)

    @property
    def connection_slots_in_use(self) -> int:
        return self._connection_slots_in_use

    @property
    def address(self) -> tuple[str, int]:
        sockets = getattr(self.server, "sockets", None)
        if sockets:
            address = sockets[0].getsockname()
            return str(address[0]), int(address[1])
        return self.config.listener.host, self.config.listener.port

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._accept, self.config.listener.host, self.config.listener.port
        )
        LOG.info("proxy listening on %s:%s", *self.address)

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._closing:
            task = asyncio.create_task(self._reject_connection(writer, "shutdown"))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        task = asyncio.create_task(self._handle_connection(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _reject_connection(
        self, writer: asyncio.StreamWriter, reason: str
    ) -> None:
        connection_id = uuid.uuid4().hex
        peer = writer.get_extra_info("peername") or ("unknown", 0)
        client_address = f"{peer[0]}:{peer[1] if len(peer) > 1 else 0}"
        self.metrics.emit(
            "connection_rejected",
            {
                "connection_id": connection_id,
                "client": client_address,
                "reason": reason,
            },
        )
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    def set_strategy(self, name: str) -> None:
        if name not in STRATEGIES:
            raise ValueError(f"unknown strategy: {name}")
        self.strategy_name = name

    def update_config(self, config: AppConfig) -> None:
        old_vnodes = self.config.balancer.virtual_nodes_per_weight
        self.config = config
        self.strategy_name = config.balancer.strategy
        self._rule_program = compile_rule(config.rules.source)
        self._rule_vm = VirtualMachine(config.rules.max_instructions)
        # Strategy state is preserved across reloads, and hash rings rebuild when
        # the (id, weight) signature changes. But a cached consistent_hash
        # strategy froze virtual_nodes_per_weight at construction, so drop it if
        # that setting changed so the new value actually takes effect.
        if config.balancer.virtual_nodes_per_weight != old_vnodes:
            self._strategies.pop("consistent_hash", None)

    async def stop_accepting(self) -> None:
        self._closing = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def drain(self, timeout: float) -> None:
        await self.stop_accepting()
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _strategy(self, name: str) -> BaseStrategy:
        strategy = self._strategies.get(name)
        if strategy is None:
            strategy = create_strategy(
                name, self.config.balancer.virtual_nodes_per_weight
            )
            self._strategies[name] = strategy
        return strategy

    def _strategy_for(self, context: ConnectionContext) -> BaseStrategy:
        # L4 routing can only supply client.* facts. The DSL also defines
        # command, backend.tag, and args.backend_id for future admin/L7
        # routing; at L4 those resolve to None and fall through to the default
        # strategy. client.port is stringified so the DSL's string-only
        # operators (==, startswith) can match it.
        result = self._rule_vm.execute(
            self._rule_program,
            {
                "client": {
                    "ip": context.client_host,
                    "port": (
                        str(context.client_port)
                        if context.client_port is not None
                        else None
                    ),
                }
            },
        )
        selected_name = result if result in STRATEGIES else self.strategy_name
        return self._strategy(selected_name)

    def force_close_backend(self, backend_id: str) -> int:
        """Cancel relay tasks for connections pinned to a backend.

        Reuses the same idempotent cleanup path as graceful shutdown, so byte
        and active-connection accounting stay correct.
        """
        closed = 0
        for connection_id, record in list(self.connections.items()):
            if record.backend_id != backend_id:
                continue
            task = self._conn_tasks.get(connection_id)
            if task is not None and not task.done():
                task.cancel()
                closed += 1
        return closed

    async def _handle_connection(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        if not self._try_acquire_connection_slot():
            connection_id = uuid.uuid4().hex
            peer = client_writer.get_extra_info("peername") or ("unknown", 0)
            client_address = f"{peer[0]}:{peer[1] if len(peer) > 1 else 0}"
            self.metrics.emit(
                "connection_rejected",
                {
                    "connection_id": connection_id,
                    "client": client_address,
                    "reason": "max_connections",
                    "limit": self.config.balancer.max_connections,
                },
            )
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        peer = client_writer.get_extra_info("peername") or ("unknown", 0)
        client_host = str(peer[0])
        client_port = int(peer[1]) if len(peer) > 1 and isinstance(peer[1], int) else None
        connection_id = uuid.uuid4().hex
        now = time.monotonic()
        record = ConnectionRecord(
            connection_id=connection_id,
            client_address=f"{client_host}:{client_port or 0}",
            accepted_monotonic=now,
            last_activity_monotonic=now,
        )
        self.connections[connection_id] = record
        current_task = asyncio.current_task()
        if current_task is not None:
            self._conn_tasks[connection_id] = current_task
        self.metrics.total_connections += 1
        self.metrics.emit(
            "connection_accepted",
            {
                "connection_id": connection_id,
                "client": record.client_address,
            },
        )
        LOG.debug("accepted %s as %s", record.client_address, connection_id)
        backend: Backend | None = None
        backend_writer: asyncio.StreamWriter | None = None
        try:
            context = ConnectionContext(
                client_host=client_host,
                client_port=client_port,
                sticky_key=client_host,
            )
            record.state = ConnectionState.SELECTING_BACKEND
            backend, backend_reader, backend_writer = await self._connect_backend(
                context, record
            )
            record.backend_id = backend.id
            backend.connection_opened(connection_id)
            record.accounted = True
            self.metrics.active_connections += 1
            record.state = ConnectionState.RELAYING
            self.metrics.emit(
                "connection_opened",
                {
                    "connection_id": connection_id,
                    "backend_id": backend.id,
                    "client": record.client_address,
                },
            )
            record.close_reason = await self._relay(
                client_reader,
                client_writer,
                backend_reader,
                backend_writer,
                record,
                backend,
            )
        except NoHealthyBackendError as exc:
            record.state = ConnectionState.FAILED
            record.close_reason = "no_eligible_backend"
            record.error_summary = str(exc)
            self.metrics.no_eligible_backend += 1
            self.metrics.emit(
                "no_eligible_backend",
                {
                    "connection_id": connection_id,
                    "client": record.client_address,
                    "error": str(exc),
                },
                critical=True,
            )
            LOG.warning("no eligible backend for %s", record.client_address)
        except BackendConnectError as exc:
            record.state = ConnectionState.FAILED
            record.close_reason = "connect_failed"
            record.error_summary = str(exc)
            self.metrics.all_connects_failed += 1
            self.metrics.emit(
                "all_connects_failed",
                {
                    "connection_id": connection_id,
                    "client": record.client_address,
                    "error": str(exc),
                },
                critical=True,
            )
        except asyncio.CancelledError:
            record.close_reason = "shutdown"
            record.error_summary = "connection cancelled during shutdown"
            raise
        except Exception as exc:
            record.state = ConnectionState.FAILED
            record.close_reason = "error"
            record.error_summary = str(exc)
            LOG.exception("connection %s failed", connection_id)
        finally:
            self._release_connection_slot()
            await self._cleanup(
                record, backend, client_writer, backend_writer
            )

    async def _connect_backend(
        self, context: ConnectionContext, record: ConnectionRecord
    ) -> tuple[Backend, asyncio.StreamReader, asyncio.StreamWriter]:
        failures: list[str] = []
        while True:
            # Each retry re-runs the strategy with the failed backend excluded.
            # Round-robin and least-connections skip index advancement on retries;
            # smooth weighted round robin uses scratch state when advance=False.
            strategy = self._strategy_for(context)
            try:
                backend = strategy.select(
                    self.pool, context, advance=not failures
                )
            except NoHealthyBackendError as exc:
                if failures:
                    raise BackendConnectError(
                        "all backend connection attempts failed: "
                        + "; ".join(failures)
                    ) from exc
                raise
            LOG.debug(
                "selected backend %s (%s) for %s",
                backend.id,
                strategy.name,
                record.connection_id,
            )
            record.state = ConnectionState.CONNECTING_BACKEND
            backend.begin_connection_attempt()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(backend.host, backend.port),
                    timeout=self.config.timeouts.connect_seconds,
                )
                strategy.on_success(backend)
                return backend, reader, writer
            except (OSError, TimeoutError) as exc:
                failures.append(f"{backend.id}: {exc}")
                context.excluded_backend_ids.add(backend.id)
                strategy.on_failure(backend)
                self.metrics.backend_connect_failures += 1
                transition = backend.record_passive_failure(
                    self.config.health.failures_to_unhealthy
                )
                self.metrics.emit(
                    "backend_connect_failure",
                    {
                        "connection_id": record.connection_id,
                        "backend_id": backend.id,
                        "error": str(exc),
                    },
                    critical=True,
                )
                if transition:
                    old, new = transition
                    self.metrics.emit(
                        "health_transition",
                        {
                            "backend_id": backend.id,
                            "from": old.value,
                            "to": new.value,
                            "source": "passive",
                        },
                        critical=True,
                    )
                LOG.warning("backend connect failed for %s: %s", backend.id, exc)
            finally:
                backend.end_connection_attempt()

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        backend_reader: asyncio.StreamReader,
        backend_writer: asyncio.StreamWriter,
        record: ConnectionRecord,
        backend: Backend,
    ) -> str:
        client_to_backend = asyncio.create_task(
            self._pump(
                client_reader,
                backend_writer,
                record,
                backend,
                client_to_backend=True,
            )
        )
        backend_to_client = asyncio.create_task(
            self._pump(
                backend_reader,
                client_writer,
                record,
                backend,
                client_to_backend=False,
            )
        )
        idle_watcher = asyncio.create_task(self._watch_idle(record))
        pumps: set[asyncio.Task[str]] = {client_to_backend, backend_to_client}
        first_close_reason: str | None = None
        try:
            while pumps:
                done, _ = await asyncio.wait(
                    pumps | {idle_watcher}, return_when=asyncio.FIRST_COMPLETED
                )
                if idle_watcher in done:
                    for task in pumps:
                        task.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
                    return "timeout"
                for task in done & pumps:
                    pumps.remove(task)
                    try:
                        reason = task.result()
                    except Exception:
                        for sibling in pumps:
                            sibling.cancel()
                        await asyncio.gather(*pumps, return_exceptions=True)
                        return "error"
                    if first_close_reason is None:
                        first_close_reason = reason
            return first_close_reason or "client_closed"
        finally:
            for task in pumps:
                if not task.done():
                    task.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            idle_watcher.cancel()
            await asyncio.gather(idle_watcher, return_exceptions=True)

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        record: ConnectionRecord,
        backend: Backend,
        *,
        client_to_backend: bool,
    ) -> str:
        while True:
            data = await reader.read(BUFFER_SIZE)
            if not data:
                if client_to_backend:
                    record.state = ConnectionState.HALF_CLOSED_CLIENT
                    reason = "client_closed"
                else:
                    record.state = ConnectionState.HALF_CLOSED_BACKEND
                    reason = "backend_closed"
                try:
                    if writer.can_write_eof():
                        writer.write_eof()
                        await writer.drain()
                except (AttributeError, ConnectionError, OSError):
                    pass
                return reason
            writer.write(data)
            await writer.drain()
            record.last_activity_monotonic = time.monotonic()
            if client_to_backend:
                size = len(data)
                record.bytes_client_to_backend += size
                backend.bytes_in += size
                self.metrics.bytes_client_to_backend += size
            else:
                size = len(data)
                record.bytes_backend_to_client += size
                backend.bytes_out += size
                self.metrics.bytes_backend_to_client += size

    async def _watch_idle(self, record: ConnectionRecord) -> str:
        # Returns "timeout" (like the pumps return a reason) so the relay's task
        # set is uniformly typed; the caller distinguishes it by identity.
        interval = min(1.0, self.config.timeouts.idle_seconds / 4)
        while True:
            await asyncio.sleep(interval)
            if (
                time.monotonic() - record.last_activity_monotonic
                >= self.config.timeouts.idle_seconds
            ):
                return "timeout"

    async def _cleanup(
        self,
        record: ConnectionRecord,
        backend: Backend | None,
        client_writer: asyncio.StreamWriter,
        backend_writer: asyncio.StreamWriter | None,
    ) -> None:
        if record.cleaned:
            return
        record.cleaned = True
        record.state = ConnectionState.CLOSING
        if backend is not None and record.accounted:
            backend.connection_closed(record.connection_id)
            self.metrics.active_connections = max(
                0, self.metrics.active_connections - 1
            )
            record.accounted = False
        # Do all bookkeeping (deregister, prune, emit) and initiate close
        # synchronously, before any await, so a second cancellation during
        # teardown cannot leave a phantom connection record behind.
        self.connections.pop(record.connection_id, None)
        self._conn_tasks.pop(record.connection_id, None)
        for writer in (backend_writer, client_writer):
            if writer is not None:
                writer.close()
        self.pool.prune_retired()
        record.state = ConnectionState.CLOSED
        self.metrics.emit(
            "connection_closed",
            {
                "connection_id": record.connection_id,
                "backend_id": record.backend_id,
                "reason": record.close_reason or "error",
                "error": record.error_summary,
                "bytes_client_to_backend": record.bytes_client_to_backend,
                "bytes_backend_to_client": record.bytes_backend_to_client,
                "duration_seconds": time.monotonic() - record.accepted_monotonic,
            },
        )
        close_timeout = self.config.timeouts.connect_seconds
        for writer in (backend_writer, client_writer):
            if writer is not None:
                try:
                    await asyncio.wait_for(
                        writer.wait_closed(), timeout=close_timeout
                    )
                except (TimeoutError, ConnectionError, OSError):
                    pass
