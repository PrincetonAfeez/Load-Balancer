"""Active TCP health checks with hysteresis."""

from __future__ import annotations

import asyncio
import logging
import time

from .config_parser import HealthConfig
from .metrics import Metrics
from .pool import Backend, BackendPool, BackendState

LOG = logging.getLogger(__name__)


class HealthChecker:
    def __init__(
        self, pool: BackendPool, config: HealthConfig, metrics: Metrics
    ) -> None:
        self.pool = pool
        self.config = config
        self.metrics = metrics
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def update_config(self, config: HealthConfig) -> None:
        self.config = config
        self._wake.set()

    def request_check(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            # Clear before doing work so a wake (stop/reload/request) that arrives
            # during the checks is preserved and shortens the wait below.
            self._wake.clear()
            started = time.monotonic()
            candidates = [
                backend
                for backend in self.pool.all()
                if backend.state not in {BackendState.DISABLED, BackendState.DRAINING}
            ]
            if candidates:
                await asyncio.gather(
                    *(self._check_backend(backend) for backend in candidates),
                    return_exceptions=True,
                )
            if self._stop.is_set():
                break
            elapsed = time.monotonic() - started
            delay = max(0.0, self.config.interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _check_backend(self, backend: Backend) -> None:
        success = False
        error: str | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(backend.host, backend.port),
                timeout=self.config.timeout_seconds,
            )
            success = True
        except (OSError, TimeoutError) as exc:
            error = str(exc)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(
                        writer.wait_closed(), timeout=self.config.timeout_seconds
                    )
                except (TimeoutError, ConnectionError, OSError):
                    pass

        transition = backend.record_active_check(
            success,
            self.config.failures_to_unhealthy,
            self.config.successes_to_healthy,
        )
        self.metrics.emit(
            "health_check",
            {
                "backend_id": backend.id,
                "success": success,
                "error": error,
                "success_streak": backend.consecutive_successes,
                "failure_streak": backend.consecutive_failures,
            },
        )
        if transition:
            old, new = transition
            LOG.warning(
                "backend %s health transition %s -> %s",
                backend.id,
                old.value,
                new.value,
            )
            self.metrics.emit(
                "health_transition",
                {
                    "backend_id": backend.id,
                    "from": old.value,
                    "to": new.value,
                },
                critical=True,
            )

