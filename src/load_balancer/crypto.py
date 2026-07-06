"""HMAC signing, verification, secure secret generation, and replay defense."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
from typing import Any

from .errors import AuthenticationError, ConfigError, ReplayError

MIN_ADMIN_SECRET_LENGTH = 32

SIGNED_REQUEST_FIELDS = ("version", "timestamp", "nonce", "command", "args")
SIGNED_RESPONSE_FIELDS = ("version", "timestamp", "nonce", "ok", "data", "error")


def generate_secret() -> str:
    return secrets.token_urlsafe(48)


def load_admin_secret(path: str | Path) -> str:
    secret_path = Path(path)
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"admin secret not found: {secret_path}; run init-secret"
        ) from exc
    if len(secret) < MIN_ADMIN_SECRET_LENGTH:
        raise ConfigError("admin secret is too short; run init-secret again")
    return secret


def canonical_request_payload(payload: Mapping[str, Any]) -> bytes:
    canonical = {field: payload[field] for field in SIGNED_REQUEST_FIELDS}
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_response_payload(payload: Mapping[str, Any]) -> bytes:
    canonical: dict[str, Any] = {
        "version": payload["version"],
        "timestamp": payload["timestamp"],
        "nonce": payload["nonce"],
        "ok": payload["ok"],
    }
    if payload.get("ok"):
        canonical["data"] = payload.get("data", {})
    else:
        canonical["error"] = payload.get("error", "")
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign(message: bytes, secret: str | bytes) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify(message: bytes, signature: str, secret: str | bytes) -> bool:
    if not isinstance(signature, str):
        return False
    expected = sign(message, secret)
    return hmac.compare_digest(expected, signature)


def sign_payload(payload: Mapping[str, Any], secret: str | bytes) -> str:
    return sign(canonical_request_payload(payload), secret)


def sign_response(payload: Mapping[str, Any], secret: str | bytes) -> str:
    return sign(canonical_response_payload(payload), secret)


def verify_response(
    payload: Mapping[str, Any],
    secret: str | bytes,
    max_clock_skew_seconds: int,
    response_cache: ResponseNonceCache | None = None,
    now: float | None = None,
) -> None:
    required = {"version", "timestamp", "nonce", "ok", "signature"}
    if payload.get("ok"):
        required.add("data")
    else:
        required.add("error")
    missing = required - set(payload)
    if missing:
        raise AuthenticationError(
            f"admin response missing fields: {', '.join(sorted(missing))}"
        )
    if payload.get("version") != 1:
        raise AuthenticationError("unsupported admin response version")
    timestamp = payload.get("timestamp")
    nonce = payload.get("nonce")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise AuthenticationError("response timestamp must be an integer")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
        raise AuthenticationError("response nonce must be a non-empty string")
    if not isinstance(payload.get("ok"), bool):
        raise AuthenticationError("response ok must be a boolean")
    current = time.time() if now is None else now
    if abs(current - timestamp) > max_clock_skew_seconds:
        raise AuthenticationError("admin response timestamp is expired")
    try:
        message = canonical_response_payload(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("admin response is not canonicalizable") from exc
    if not verify(message, payload.get("signature", ""), secret):
        raise AuthenticationError("invalid admin response signature")
    if response_cache is not None:
        response_cache.check_and_store(nonce, current)


# Backward-compatible alias for request canonicalization.
def canonical_payload(payload: Mapping[str, Any]) -> bytes:
    return canonical_request_payload(payload)


SIGNED_FIELDS = SIGNED_REQUEST_FIELDS


class ResponseNonceCache:
    """Bounded cache so signed admin responses cannot be replayed by a client."""

    def __init__(self, ttl_seconds: int = 60, max_entries: int = 10_000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, float] = OrderedDict()

    def check_and_store(self, nonce: str, now: float | None = None) -> None:
        current = time.time() if now is None else now
        cutoff = current - self.ttl_seconds
        while self._entries:
            _, timestamp = next(iter(self._entries.items()))
            if timestamp >= cutoff:
                break
            self._entries.popitem(last=False)
        if nonce in self._entries:
            raise ReplayError("admin response nonce was already seen")
        self._entries[nonce] = current
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


class ReplayCache:
    def __init__(self, ttl_seconds: int = 30, max_entries: int = 10_000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, float] = OrderedDict()

    def check_and_store(self, nonce: str, now: float | None = None) -> None:
        current = time.time() if now is None else now
        cutoff = current - self.ttl_seconds
        while self._entries:
            _, timestamp = next(iter(self._entries.items()))
            if timestamp >= cutoff:
                break
            self._entries.popitem(last=False)
        if nonce in self._entries:
            raise ReplayError("admin nonce was already used")
        self._entries[nonce] = current
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


def authenticate_payload(
    payload: Mapping[str, Any],
    secret: str | bytes,
    replay_cache: ReplayCache,
    max_clock_skew_seconds: int,
    now: float | None = None,
) -> None:
    required = set(SIGNED_REQUEST_FIELDS) | {"signature"}
    missing = required - set(payload)
    if missing:
        raise AuthenticationError(
            f"admin payload missing fields: {', '.join(sorted(missing))}"
        )
    if payload.get("version") != 1:
        raise AuthenticationError("unsupported admin protocol version")
    timestamp = payload.get("timestamp")
    nonce = payload.get("nonce")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise AuthenticationError("timestamp must be an integer")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
        raise AuthenticationError("nonce must be a non-empty string")
    current = time.time() if now is None else now
    if abs(current - timestamp) > max_clock_skew_seconds:
        raise AuthenticationError("admin command timestamp is expired")
    try:
        message = canonical_request_payload(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("admin payload is not canonicalizable") from exc
    if not verify(message, payload.get("signature", ""), secret):
        raise AuthenticationError("invalid admin command signature")
    replay_cache.check_and_store(nonce, current)
