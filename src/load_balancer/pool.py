"""Backend runtime model and in-memory pool."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
import time

from .config_parser import BackendConfig


class BackendState(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DRAINING = "draining"
    DISABLED = "disabled"


@dataclass(slots=True)
class Backend:
    id: str
    host: str
    port: int
    weight: int = 1
    state: BackendState = BackendState.HEALTHY
    tags: tuple[str, ...] = ()
    active_connections: int = 0
    pending_connections: int = 0
    total_connections: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    passive_failures: int = 0
    last_health_check: float | None = None
    last_health_ok: bool | None = None
    retired: bool = False
    _active_tokens: set[str] = field(default_factory=set, repr=False)

    @classmethod
    def from_config(cls, config: BackendConfig) -> Backend:
        return cls(
            id=config.name,
            host=config.host,
            port=config.port,
            weight=config.weight,
            state=BackendState.HEALTHY if config.enabled else BackendState.DISABLED,
            tags=config.tags,
        )

    @property
    def eligible(self) -> bool:
        return self.state is BackendState.HEALTHY and not self.retired

    @property
    def load_connections(self) -> int:
        return self.active_connections + self.pending_connections

    def begin_connection_attempt(self) -> None:
        self.pending_connections += 1

    def end_connection_attempt(self) -> None:
        self.pending_connections = max(0, self.pending_connections - 1)

    def connection_opened(self, connection_id: str) -> None:
        if connection_id in self._active_tokens:
            return
        self._active_tokens.add(connection_id)
        self.active_connections += 1
        self.total_connections += 1

    def connection_closed(self, connection_id: str) -> None:
        if connection_id not in self._active_tokens:
            return
        self._active_tokens.remove(connection_id)
        self.active_connections = max(0, self.active_connections - 1)

    def reset_health_streaks(self) -> None:
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.passive_failures = 0

    def record_active_check(
        self,
        success: bool,
        failures_to_unhealthy: int,
        successes_to_healthy: int,
    ) -> tuple[BackendState, BackendState] | None:
        self.last_health_check = time.time()
        self.last_health_ok = success
        old_state = self.state
        if success:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            self.passive_failures = max(0, self.passive_failures - 1)
            if (
                self.state is BackendState.UNHEALTHY
                and self.consecutive_successes >= successes_to_healthy
            ):
                self.state = BackendState.HEALTHY
        else:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            if (
                self.state is BackendState.HEALTHY
                and self.consecutive_failures >= failures_to_unhealthy
            ):
                self.state = BackendState.UNHEALTHY
        if self.state is not old_state:
            return old_state, self.state
        return None

    def record_passive_failure(
        self, failures_to_unhealthy: int
    ) -> tuple[BackendState, BackendState] | None:
        self.passive_failures += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        old_state = self.state
        if (
            self.state is BackendState.HEALTHY
            and self.consecutive_failures >= failures_to_unhealthy
        ):
            self.state = BackendState.UNHEALTHY
        if self.state is not old_state:
            return old_state, self.state
        return None

    def update_config(self, config: BackendConfig) -> None:
        self.host = config.host
        self.port = config.port
        self.weight = config.weight
        self.tags = config.tags
        self.retired = False
        if not config.enabled:
            self.state = BackendState.DISABLED
        elif self.state is BackendState.DISABLED:
            self.state = BackendState.HEALTHY
            self.reset_health_streaks()
        # DRAINING and UNHEALTHY are preserved across reload so admin drain and
        # health hysteresis are not undone by a config swap.

    def status(self) -> dict[str, object]:
        return {
            "id": self.id,
            "host": self.host,
            "port": self.port,
            "weight": self.weight,
            "state": self.state.value,
            "active": self.active_connections,
            "pending": self.pending_connections,
            "total": self.total_connections,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "success_streak": self.consecutive_successes,
            "failure_streak": self.consecutive_failures,
            "passive_failures": self.passive_failures,
            "last_health_check": self.last_health_check,
            "last_health_ok": self.last_health_ok,
            "tags": list(self.tags),
            "retired": self.retired,
        }


class BackendPool:
    """Event-loop-owned backend state with reload-safe retirement."""

    def __init__(self, backends: Iterable[Backend]) -> None:
        self._backends: dict[str, Backend] = {}
        self._order: list[str] = []
        for backend in backends:
            if backend.id in self._backends:
                raise ValueError(f"duplicate backend id: {backend.id}")
            self._backends[backend.id] = backend
            self._order.append(backend.id)

    @classmethod
    def from_configs(cls, configs: Iterable[BackendConfig]) -> BackendPool:
        return cls(Backend.from_config(config) for config in configs)

    def all(self) -> list[Backend]:
        return [self._backends[name] for name in self._order if name in self._backends]

    def eligible(self, excluded: set[str] | None = None) -> list[Backend]:
        excluded = excluded or set()
        return [
            backend
            for backend in self.all()
            if backend.eligible and backend.id not in excluded
        ]

    def get(self, backend_id: str) -> Backend | None:
        return self._backends.get(backend_id)

    def require(self, backend_id: str) -> Backend:
        backend = self.get(backend_id)
        if backend is None:
            raise KeyError(backend_id)
        return backend

    def add(self, config: BackendConfig) -> Backend:
        if config.name in self._backends:
            raise ValueError(f"backend already exists: {config.name}")
        address = (config.host, config.port)
        for existing in self._backends.values():
            if (existing.host, existing.port) == address:
                raise ValueError(
                    f"backend address already in use: {config.host}:{config.port}"
                )
        backend = Backend.from_config(config)
        self._backends[backend.id] = backend
        self._order.append(backend.id)
        return backend

    def drain(self, backend_id: str, retired: bool = False) -> Backend:
        backend = self.require(backend_id)
        backend.state = BackendState.DRAINING
        backend.retired = retired
        self.prune_retired()
        return backend

    def enable(self, backend_id: str) -> Backend:
        backend = self.require(backend_id)
        backend.retired = False
        backend.state = BackendState.HEALTHY
        backend.reset_health_streaks()
        return backend

    def disable(self, backend_id: str) -> Backend:
        backend = self.require(backend_id)
        backend.state = BackendState.DISABLED
        return backend

    def remove(self, backend_id: str) -> Backend:
        return self.drain(backend_id, retired=True)

    def prune_retired(self) -> list[str]:
        removed: list[str] = []
        for backend_id in list(self._order):
            backend = self._backends[backend_id]
            if (
                backend.retired
                and backend.active_connections == 0
                and backend.pending_connections == 0
            ):
                removed.append(backend_id)
                self._order.remove(backend_id)
                del self._backends[backend_id]
        return removed

    def apply_configs(
        self,
        configs: Iterable[BackendConfig],
        *,
        reserved_addresses: frozenset[tuple[str, int]] | None = None,
    ) -> dict[str, list[str]]:
        candidate = list(configs)
        self._validate_candidate_addresses(candidate, reserved_addresses)
        candidate_ids = {config.name for config in candidate}
        current_ids = set(self._backends)
        added: list[str] = []
        updated: list[str] = []
        draining: list[str] = []

        for config in candidate:
            existing = self._backends.get(config.name)
            if existing is None:
                self.add(config)
                added.append(config.name)
            else:
                new_address = (config.host, config.port)
                old_address = (existing.host, existing.port)
                if new_address != old_address and existing.load_connections > 0:
                    raise ValueError(
                        f"cannot change address for backend {config.name} with "
                        f"active connections; drain or wait for connections to close"
                    )
                existing.update_config(config)
                updated.append(config.name)

        for removed_id in current_ids - candidate_ids:
            self.drain(removed_id, retired=True)
            draining.append(removed_id)

        # Stable config order first, followed by in-flight retired backends.
        retired_ids = [
            backend_id
            for backend_id in self._order
            if backend_id not in candidate_ids and backend_id in self._backends
        ]
        self._order = [config.name for config in candidate] + retired_ids
        self.prune_retired()
        return {"added": added, "updated": updated, "draining": draining}

    def _validate_candidate_addresses(
        self,
        candidate: list[BackendConfig],
        reserved_addresses: frozenset[tuple[str, int]] | None,
    ) -> None:
        reserved = reserved_addresses or frozenset()
        candidate_ids = {config.name for config in candidate}
        projected: dict[tuple[str, int], str] = {}
        for config in candidate:
            address = (config.host, config.port)
            if address in reserved:
                raise ValueError(
                    f"backend {config.name} address collides with a reserved address: "
                    f"{config.host}:{config.port}"
                )
            if address in projected:
                raise ValueError(
                    f"duplicate backend address: {config.host}:{config.port}"
                )
            projected[address] = config.name
        for backend in self._backends.values():
            if backend.id in candidate_ids:
                continue
            address = (backend.host, backend.port)
            if address in projected:
                raise ValueError(
                    f"backend address already in use: {backend.host}:{backend.port}"
                )
