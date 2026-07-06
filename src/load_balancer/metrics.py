"""In-memory counters and a non-blocking event queue."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any


@dataclass(slots=True)
class MetricEvent:
    kind: str
    timestamp: float
    data: dict[str, Any]
    critical: bool = False


class Metrics:
    """Mutable metrics owned by the asyncio event loop."""

    def __init__(self, queue_size: int = 10_000) -> None:
        self.queue: asyncio.Queue[MetricEvent] = asyncio.Queue(maxsize=queue_size)
        # Critical events (lifecycle, reload, health transitions, admin rejects)
        # get a dedicated intake queue so a flood of high-volume connection
        # events can never crowd them out at enqueue time.
        self.critical_queue: asyncio.Queue[MetricEvent] = asyncio.Queue(
            maxsize=max(256, queue_size // 8)
        )
        self.started_monotonic = time.monotonic()
        self.active_connections = 0
        self.total_connections = 0
        self.no_eligible_backend = 0
        self.all_connects_failed = 0
        self.backend_connect_failures = 0
        self.bytes_client_to_backend = 0
        self.bytes_backend_to_client = 0
        self.dropped_events = 0
        self.dropped_critical_events = 0
        self.dropped_snapshots = 0
        self._counters: dict[str, int] = {}

    @property
    def no_backend_available(self) -> int:
        """Total routing failures (no eligible backend or all connects failed)."""
        return self.no_eligible_backend + self.all_connects_failed

    def increment(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    def emit(self, kind: str, data: dict[str, Any], critical: bool = False) -> None:
        event = MetricEvent(kind, time.time(), data, critical)
        target = self.critical_queue if critical else self.queue
        try:
            target.put_nowait(event)
            self.increment(kind)
        except asyncio.QueueFull:
            # The hot path wins over telemetry. The dropped count remains visible,
            # and critical drops are counted separately so they stand out.
            if critical:
                self.dropped_critical_events += 1
            else:
                self.dropped_events += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime_seconds": time.monotonic() - self.started_monotonic,
            "active_connections": self.active_connections,
            "total_connections": self.total_connections,
            "no_eligible_backend": self.no_eligible_backend,
            "all_connects_failed": self.all_connects_failed,
            "no_backend_available": self.no_backend_available,
            "backend_connect_failures": self.backend_connect_failures,
            "bytes_client_to_backend": self.bytes_client_to_backend,
            "bytes_backend_to_client": self.bytes_backend_to_client,
            "dropped_events": self.dropped_events,
            "dropped_critical_events": self.dropped_critical_events,
            "dropped_snapshots": self.dropped_snapshots,
            "counters": dict(self._counters),
        }
