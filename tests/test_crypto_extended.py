"""Extended crypto module tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from load_balancer.crypto import (
    MIN_ADMIN_SECRET_LENGTH,
    ReplayCache,
    ResponseNonceCache,
    authenticate_payload,
    canonical_payload,
    canonical_request_payload,
    canonical_response_payload,
    generate_secret,
    load_admin_secret,
    sign,
    sign_payload,
    sign_response,
    verify,
    verify_response,
)
from load_balancer.errors import AuthenticationError, ConfigError, ReplayError


class CryptoExtendedTests(unittest.TestCase):
    def test_generate_secret_length(self):
        secret = generate_secret()
        self.assertGreaterEqual(len(secret), MIN_ADMIN_SECRET_LENGTH)

    def test_load_admin_secret_missing_and_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.txt"
            with self.assertRaises(ConfigError):
                load_admin_secret(path)
            path.write_text("short", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_admin_secret(path)
            path.write_text("x" * MIN_ADMIN_SECRET_LENGTH, encoding="utf-8")
            self.assertEqual(len(load_admin_secret(path)), MIN_ADMIN_SECRET_LENGTH)

    def test_sign_and_verify(self):
        message = b"payload"
        secret = "test-secret-long-enough-for-hmac-signing"
        sig = sign(message, secret)
        self.assertTrue(verify(message, sig, secret))
        self.assertFalse(verify(message, "wrong", secret))
        self.assertFalse(verify(message, sig, "other-secret-long-enough-too"))

    def test_canonical_payload_alias(self):
        payload = {
            "version": 1,
            "timestamp": 1,
            "nonce": "n",
            "command": "status",
            "args": {},
        }
        self.assertEqual(canonical_payload(payload), canonical_request_payload(payload))

    def test_verify_response_error_path(self):
        secret = "test-secret-long-enough-for-hmac"
        payload = {
            "version": 1,
            "timestamp": int(time.time()),
            "nonce": "err-nonce",
            "ok": False,
            "error": "failed",
        }
        payload["signature"] = sign_response(payload, secret)
        verify_response(payload, secret, 30)

    def test_verify_response_missing_fields(self):
        with self.assertRaises(AuthenticationError):
            verify_response(
                {"version": 1},
                "secret-long-enough-for-hmac-test",
                30,
            )

    def test_verify_response_bad_signature(self):
        payload = {
            "version": 1,
            "timestamp": int(time.time()),
            "nonce": "n",
            "ok": True,
            "data": {},
            "signature": "bad",
        }
        with self.assertRaises(AuthenticationError):
            verify_response(payload, "secret-long-enough-for-hmac-test", 30)

    def test_replay_cache_ttl_and_max_entries(self):
        cache = ReplayCache(ttl_seconds=60, max_entries=2)
        now = 1000.0
        cache.check_and_store("a", now)
        cache.check_and_store("b", now)
        with self.assertRaises(ReplayError):
            cache.check_and_store("a", now + 1)
        cache.check_and_store("c", now + 1)
        cache.check_and_store("a", now + 70)

    def test_response_nonce_cache(self):
        cache = ResponseNonceCache(ttl_seconds=30, max_entries=2)
        cache.check_and_store("one", 100.0)
        with self.assertRaises(ReplayError):
            cache.check_and_store("one", 101.0)

    def test_authenticate_missing_nonce(self):
        secret = generate_secret()
        payload = {
            "version": 1,
            "timestamp": int(time.time()),
            "nonce": "",
            "command": "status",
            "args": {},
        }
        payload["signature"] = sign_payload(payload, secret)
        with self.assertRaises(AuthenticationError):
            authenticate_payload(payload, secret, ReplayCache(), 30)

    def test_canonical_response_ok_defaults_data(self):
        payload = canonical_response_payload(
            {"version": 1, "timestamp": 1, "nonce": "n", "ok": True}
        )
        self.assertIn(b'"data":{}', payload)

    def test_canonical_response_error_defaults_error(self):
        payload = canonical_response_payload(
            {"version": 1, "timestamp": 1, "nonce": "n", "ok": False}
        )
        self.assertIn(b'"error":""', payload)


if __name__ == "__main__":
    unittest.main()
