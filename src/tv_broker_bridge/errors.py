"""Exception hierarchy.

The split matters operationally: a ``BrokerConnectionError`` means "outcome
unknown, safe to retry because submits are idempotent on client_order_id",
whereas ``OrderRejectedError`` is a definitive "the broker said no".
"""

from __future__ import annotations


class BridgeError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(BridgeError):
    """Invalid or unsafe configuration. The process must refuse to start."""


class LiveEndpointRefusedError(ConfigError):
    """A broker base URL that is not the paper-trading endpoint was supplied."""


class BrokerError(BridgeError):
    """Base class for broker adapter failures."""


class BrokerConnectionError(BrokerError):
    """Transport-level failure (timeout, DNS, reset, 5xx, 429).

    The request may or may not have reached the broker.
    """


class BrokerAuthError(BrokerError):
    """The broker rejected our credentials. Retrying will not help."""


class OrderRejectedError(BrokerError):
    """The broker definitively refused the order (no order exists)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OrderNotFoundError(BrokerError):
    """The broker has no order with the requested id."""


class OrderNotCancelableError(BrokerError):
    """The order is already in a terminal state at the broker."""
