"""Project-specific exceptions."""


class LoadBalancerError(Exception):
    """Base exception for expected load balancer failures."""


class ConfigError(LoadBalancerError):
    """Configuration is invalid."""


class NoHealthyBackendError(LoadBalancerError):
    """No backend is eligible for a new connection."""


class BackendConnectError(LoadBalancerError):
    """All eligible backend connection attempts failed."""


class AdminProtocolError(LoadBalancerError):
    """An admin protocol frame is invalid."""


class AuthenticationError(AdminProtocolError):
    """An admin command failed authentication."""


class ReplayError(AuthenticationError):
    """An admin nonce was already used."""


class RuleSyntaxError(ConfigError):
    """A policy rule could not be parsed."""


class VMError(LoadBalancerError):
    """The policy VM rejected or failed a program."""
