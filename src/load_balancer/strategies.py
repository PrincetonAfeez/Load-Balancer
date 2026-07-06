"""Pure, testable load-balancing strategies."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
import hashlib
from typing import Protocol

from .errors import NoHealthyBackendError
from .pool import Backend, BackendPool


@dataclass(slots=True)
class ConnectionContext:
    client_host: str
    client_port: int | None = None
    sticky_key: str | None = None
    excluded_backend_ids: set[str] = field(default_factory=set)


class Strategy(Protocol):
    name: str

    def select(
        self, pool: BackendPool, context: ConnectionContext, *, advance: bool = True
    ) -> Backend: ...

    def on_success(self, backend: Backend) -> None: ...

    def on_failure(self, backend: Backend) -> None: ...


class BaseStrategy:
    """Common base for strategies: eligibility filtering and no-op feedback hooks."""

    name = "base"

    def select(
        self, pool: BackendPool, context: ConnectionContext, *, advance: bool = True
    ) -> Backend:
        """Return one eligible backend for this connection. Overridden per strategy."""
        raise NotImplementedError

    @staticmethod
    def _eligible(pool: BackendPool, context: ConnectionContext) -> list[Backend]:
        candidates = pool.eligible(context.excluded_backend_ids)
        if not candidates:
            raise NoHealthyBackendError("no healthy eligible backend")
        return candidates

    def on_success(self, backend: Backend) -> None:
        return None

    def on_failure(self, backend: Backend) -> None:
        return None


class RoundRobinStrategy(BaseStrategy):
    """Cycle through eligible backends in stable order with a moving index."""

    name = "round_robin"

    def __init__(self) -> None:
        self._index = 0

    def select(
        self, pool: BackendPool, context: ConnectionContext, *, advance: bool = True
    ) -> Backend:
        candidates = self._eligible(pool, context)
        backend = candidates[self._index % len(candidates)]
        if advance:
            self._index = (self._index + 1) % max(1, len(candidates))
        return backend


class SmoothWeightedRoundRobinStrategy(BaseStrategy):
    """Nginx-style smooth weighted round robin (no bursts; converges to the ratio)."""

    name = "weighted_round_robin"

    def __init__(self) -> None:
        self._current: dict[str, int] = {}

    def select(
        self, pool: BackendPool, context: ConnectionContext, *, advance: bool = True
    ) -> Backend:
        candidates = self._eligible(pool, context)
        candidate_ids = {backend.id for backend in candidates}
        self._current = {
            backend_id: value
            for backend_id, value in self._current.items()
            if backend_id in candidate_ids
        }
        total = sum(backend.weight for backend in candidates)
        best: Backend | None = None
        best_weight = 0
        scratch = dict(self._current) if not advance else self._current
        for backend in candidates:
            current = scratch.get(backend.id, 0) + backend.weight
            scratch[backend.id] = current
            if best is None or current > best_weight:
                best = backend
                best_weight = current
            elif current == best_weight and backend.load_connections < best.load_connections:
                best = backend
        assert best is not None
        if advance:
            self._current = scratch
            self._current[best.id] -= total
        return best


class LeastConnectionsStrategy(BaseStrategy):
    """Pick the fewest active connections, breaking ties round-robin."""

    name = "least_connections"

    def __init__(self) -> None:
        self._tie_index = 0

    def select(
        self, pool: BackendPool, context: ConnectionContext, *, advance: bool = True
    ) -> Backend:
        candidates = self._eligible(pool, context)
        minimum = min(backend.load_connections for backend in candidates)
        tied = [
            backend for backend in candidates if backend.load_connections == minimum
        ]
        backend = tied[self._tie_index % len(tied)]
        if advance:
            self._tie_index = (self._tie_index + 1) % max(1, len(tied))
        return backend


class ConsistentHashStrategy(BaseStrategy):
    """SHA-256 hash ring with weighted virtual nodes for sticky source-IP routing."""

    name = "consistent_hash"

    def __init__(self, virtual_nodes_per_weight: int = 64) -> None:
        if virtual_nodes_per_weight <= 0:
            raise ValueError("virtual_nodes_per_weight must be positive")
        self.virtual_nodes_per_weight = virtual_nodes_per_weight
        self._signature: tuple[tuple[str, int], ...] = ()
        self._points: list[int] = []
        self._owners: list[str] = []

    @staticmethod
    def _hash(value: str) -> int:
        return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest(), "big")

    def _rebuild(self, candidates: list[Backend]) -> None:
        signature = tuple((backend.id, backend.weight) for backend in candidates)
        if signature == self._signature:
            return
        ring: list[tuple[int, str]] = []
        for backend in candidates:
            count = backend.weight * self.virtual_nodes_per_weight
            for virtual_index in range(count):
                ring.append(
                    (self._hash(f"{backend.id}:{virtual_index}"), backend.id)
                )
        ring.sort(key=lambda item: item[0])
        self._points = [item[0] for item in ring]
        self._owners = [item[1] for item in ring]
        self._signature = signature

    def select(
        self, pool: BackendPool, context: ConnectionContext, *, advance: bool = True
    ) -> Backend:
        # advance is unused: sticky routing is keyed by client identity, and
        # connect retries exclude failed backends via context.excluded_backend_ids.
        candidates = self._eligible(pool, context)
        self._rebuild(candidates)
        key = context.sticky_key or context.client_host
        position = bisect_left(self._points, self._hash(key))
        if position == len(self._points):
            position = 0
        backend = pool.get(self._owners[position])
        if backend is None or not backend.eligible:
            self._signature = ()
            return self.select(pool, context, advance=advance)
        return backend


def create_strategy(name: str, virtual_nodes_per_weight: int = 64) -> BaseStrategy:
    strategies: dict[str, type[BaseStrategy]] = {
        RoundRobinStrategy.name: RoundRobinStrategy,
        SmoothWeightedRoundRobinStrategy.name: SmoothWeightedRoundRobinStrategy,
        LeastConnectionsStrategy.name: LeastConnectionsStrategy,
    }
    if name == ConsistentHashStrategy.name:
        return ConsistentHashStrategy(virtual_nodes_per_weight)
    try:
        return strategies[name]()
    except KeyError as exc:
        raise ValueError(f"unknown strategy: {name}") from exc
