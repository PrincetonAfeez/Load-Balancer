"""TOML configuration parsing and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import tomllib
from typing import Any

from .errors import ConfigError
from .rule_dsl import compile_rule

STRATEGIES = {
    "round_robin",
    "weighted_round_robin",
    "least_connections",
    "consistent_hash",
}

KNOWN_SECTIONS = {
    "listener",
    "balancer",
    "health",
    "timeouts",
    "metrics",
    "control",
    "crypto",
    "rules",
    "backends",
}

# The consistent-hash ring allocates weight * virtual_nodes_per_weight points
# per backend and is built on the event loop, so both inputs are capped to keep
# ring construction bounded and the hot path responsive.
MAX_BACKEND_WEIGHT = 1000
MAX_VIRTUAL_NODES_PER_WEIGHT = 1024

# Upper bounds on sizing knobs so a misconfiguration cannot enable a runaway
# allocation (read_frame reads up to max_frame_bytes) or a degenerate queue.
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_QUEUE_SIZE = 1_000_000
MAX_BATCH_SIZE = 100_000
MAX_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True, slots=True)
class BalancerConfig:
    strategy: str = "round_robin"
    drain_timeout_seconds: float = 30.0
    pid_file: str = "load-balancer.pid"
    virtual_nodes_per_weight: int = 64
    max_connections: int = 0


@dataclass(frozen=True, slots=True)
class HealthConfig:
    interval_seconds: float = 2.0
    timeout_seconds: float = 1.0
    failures_to_unhealthy: int = 3
    successes_to_healthy: int = 2


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    connect_seconds: float = 3.0
    idle_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    database_path: str = "load-balancer.db"
    flush_interval_seconds: float = 1.0
    snapshot_interval_seconds: float = 10.0
    queue_size: int = 10_000
    batch_size: int = 250


@dataclass(frozen=True, slots=True)
class ControlConfig:
    host: str = "127.0.0.1"
    port: int = 9900
    max_frame_bytes: int = 65_536
    max_clock_skew_seconds: int = 30


@dataclass(frozen=True, slots=True)
class CryptoConfig:
    secret_file: str = "admin.secret"


@dataclass(frozen=True, slots=True)
class RuleConfig:
    source: str = 'return "default"'
    max_instructions: int = 256


@dataclass(frozen=True, slots=True)
class BackendConfig:
    name: str
    host: str
    port: int
    weight: int = 1
    enabled: bool = True
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AppConfig:
    listener: ListenerConfig = field(default_factory=ListenerConfig)
    balancer: BalancerConfig = field(default_factory=BalancerConfig)
    health: HealthConfig = field(default_factory=HealthConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    crypto: CryptoConfig = field(default_factory=CryptoConfig)
    rules: RuleConfig = field(default_factory=RuleConfig)
    backends: tuple[BackendConfig, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _make(cls: type, values: dict[str, Any], section: str) -> Any:
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"unknown keys in [{section}]: {', '.join(unknown)}")
    try:
        return cls(**values)
    except TypeError as exc:
        raise ConfigError(f"invalid [{section}] settings: {exc}") from exc


def parse_config_data(data: dict[str, Any]) -> AppConfig:
    unknown_sections = sorted(set(data) - KNOWN_SECTIONS)
    if unknown_sections:
        raise ConfigError(
            f"unknown config section(s): {', '.join(unknown_sections)}; "
            f"valid sections are {', '.join(sorted(KNOWN_SECTIONS))}"
        )
    raw_backends = data.get("backends", [])
    if not isinstance(raw_backends, list):
        raise ConfigError("[[backends]] entries must be an array of tables")
    backends: list[BackendConfig] = []
    for index, raw in enumerate(raw_backends):
        if not isinstance(raw, dict):
            raise ConfigError(f"backend #{index + 1} must be a table")
        values = dict(raw)
        tags = values.get("tags", ())
        if not isinstance(tags, (list, tuple)) or not all(
            isinstance(tag, str) for tag in tags
        ):
            raise ConfigError(f"backend #{index + 1} tags must be strings")
        values["tags"] = tuple(tags)
        backends.append(_make(BackendConfig, values, f"backends #{index + 1}"))

    config = AppConfig(
        listener=_make(ListenerConfig, _table(data, "listener"), "listener"),
        balancer=_make(BalancerConfig, _table(data, "balancer"), "balancer"),
        health=_make(HealthConfig, _table(data, "health"), "health"),
        timeouts=_make(TimeoutConfig, _table(data, "timeouts"), "timeouts"),
        metrics=_make(MetricsConfig, _table(data, "metrics"), "metrics"),
        control=_make(ControlConfig, _table(data, "control"), "control"),
        crypto=_make(CryptoConfig, _table(data, "crypto"), "crypto"),
        rules=_make(RuleConfig, _table(data, "rules"), "rules"),
        backends=tuple(backends),
    )
    validate_config(config)
    return config


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    return parse_config_data(data)


def validate_config(config: AppConfig) -> None:
    _validate_host_port(config.listener.host, config.listener.port, "listener")
    _validate_host_port(config.control.host, config.control.port, "control")
    if (
        config.listener.host == config.control.host
        and config.listener.port == config.control.port
    ):
        raise ConfigError("listener and control socket cannot use the same address")
    if (
        not isinstance(config.balancer.strategy, str)
        or config.balancer.strategy not in STRATEGIES
    ):
        raise ConfigError(
            f"unknown strategy {config.balancer.strategy!r}; "
            f"choose from {', '.join(sorted(STRATEGIES))}"
        )
    _nonempty_string(config.balancer.pid_file, "PID file")
    _positive_number(config.balancer.drain_timeout_seconds, "drain timeout")
    _positive_int(
        config.balancer.virtual_nodes_per_weight,
        "virtual nodes per weight",
        maximum=MAX_VIRTUAL_NODES_PER_WEIGHT,
    )
    if (
        not isinstance(config.balancer.max_connections, int)
        or isinstance(config.balancer.max_connections, bool)
        or config.balancer.max_connections < 0
    ):
        raise ConfigError("max connections must be a non-negative integer (0 = unlimited)")
    _positive_number(config.health.interval_seconds, "health interval")
    _positive_number(config.health.timeout_seconds, "health timeout")
    if config.health.timeout_seconds > config.health.interval_seconds:
        raise ConfigError(
            "health timeout_seconds must not exceed interval_seconds, "
            "otherwise checks overlap with no idle gap"
        )
    _positive_int(config.health.failures_to_unhealthy, "health failure threshold")
    _positive_int(config.health.successes_to_healthy, "health success threshold")
    _positive_number(config.timeouts.connect_seconds, "connect timeout")
    _positive_number(config.timeouts.idle_seconds, "idle timeout")
    _nonempty_string(config.metrics.database_path, "metrics database path")
    _positive_number(
        config.metrics.flush_interval_seconds, "metrics flush interval"
    )
    _positive_number(
        config.metrics.snapshot_interval_seconds, "snapshot interval"
    )
    _positive_int(
        config.metrics.queue_size, "metrics queue size", maximum=MAX_QUEUE_SIZE
    )
    _positive_int(
        config.metrics.batch_size, "metrics batch size", maximum=MAX_BATCH_SIZE
    )
    if config.metrics.batch_size > config.metrics.queue_size:
        raise ConfigError("metrics batch size cannot exceed the queue size")
    _positive_int(
        config.control.max_frame_bytes, "max admin frame size", maximum=MAX_FRAME_BYTES
    )
    _positive_int(
        config.control.max_clock_skew_seconds,
        "admin max clock skew",
        maximum=MAX_CLOCK_SKEW_SECONDS,
    )
    _nonempty_string(config.crypto.secret_file, "admin secret file")
    _positive_int(config.rules.max_instructions, "rule instruction limit")
    if not isinstance(config.rules.source, str):
        raise ConfigError("rule source must be a string")
    if not config.backends:
        raise ConfigError("at least one [[backends]] entry is required")
    names: set[str] = set()
    addresses: set[tuple[str, int]] = set()
    for backend in config.backends:
        if not backend.name or not backend.name.strip():
            raise ConfigError("backend names cannot be empty")
        if backend.name in names:
            raise ConfigError(f"duplicate backend name: {backend.name}")
        names.add(backend.name)
        _validate_host_port(backend.host, backend.port, f"backend {backend.name}")
        if (
            not isinstance(backend.weight, int)
            or isinstance(backend.weight, bool)
            or backend.weight <= 0
        ):
            raise ConfigError(f"backend {backend.name} weight must be positive")
        if backend.weight > MAX_BACKEND_WEIGHT:
            raise ConfigError(
                f"backend {backend.name} weight must not exceed {MAX_BACKEND_WEIGHT}"
            )
        if not isinstance(backend.enabled, bool):
            raise ConfigError(f"backend {backend.name} enabled must be boolean")
        address = (backend.host, backend.port)
        if address == (config.listener.host, config.listener.port):
            raise ConfigError(
                f"backend {backend.name} address collides with the listener address"
            )
        if address == (config.control.host, config.control.port):
            raise ConfigError(
                f"backend {backend.name} address collides with the control address"
            )
        if address in addresses:
            raise ConfigError(f"duplicate backend address: {backend.host}:{backend.port}")
        addresses.add(address)
    compiled_rule = compile_rule(config.rules.source)
    # Compiled programs only ever jump forward, so the number of executed
    # instructions can never exceed the program length. Rejecting an oversized
    # program here means the rule VM can never hit its limit at runtime (which
    # would otherwise fail every connection while the daemon kept running).
    if len(compiled_rule) > config.rules.max_instructions:
        raise ConfigError(
            f"rule compiles to {len(compiled_rule)} instructions, exceeding the "
            f"configured max_instructions limit of {config.rules.max_instructions}"
        )


def _validate_host_port(host: Any, port: Any, label: str) -> None:
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f"{label} host must be a non-empty string")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"{label} port must be between 1 and 65535")


def _positive_number(value: Any, label: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(f"{label} must be positive")


def _positive_int(value: Any, label: str, maximum: int | None = None) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{label} must not exceed {maximum}")


def _nonempty_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
