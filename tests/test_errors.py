"""Tests for the exception hierarchy."""

from __future__ import annotations

import unittest

from load_balancer.errors import (
    AdminProtocolError,
    AuthenticationError,
    BackendConnectError,
    ConfigError,
    LoadBalancerError,
    NoHealthyBackendError,
    ReplayError,
    RuleSyntaxError,
    VMError,
)


class ErrorHierarchyTests(unittest.TestCase):
    def test_base_and_subclasses(self):
        self.assertIsInstance(ConfigError("x"), LoadBalancerError)
        self.assertIsInstance(RuleSyntaxError("x"), ConfigError)
        self.assertIsInstance(NoHealthyBackendError("x"), LoadBalancerError)
        self.assertIsInstance(BackendConnectError("x"), LoadBalancerError)
        self.assertIsInstance(AdminProtocolError("x"), LoadBalancerError)
        self.assertIsInstance(AuthenticationError("x"), AdminProtocolError)
        self.assertIsInstance(ReplayError("x"), AuthenticationError)
        self.assertIsInstance(VMError("x"), LoadBalancerError)


if __name__ == "__main__":
    unittest.main()
