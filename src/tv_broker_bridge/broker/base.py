"""Broker adapter interface.

Contract every implementation must honour:

* ``submit_order`` is **idempotent on ``client_order_id``**: submitting the same
  id twice returns the existing order instead of creating a second one. The
  retry-after-timeout logic in the pipeline depends on this.
* Transport failures raise ``BrokerConnectionError`` (outcome unknown);
  definitive refusals raise ``OrderRejectedError``.
* The broker's view of orders and positions is the source of truth.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from tv_broker_bridge.models import BrokerOrder, BrokerPosition, OrderRequest


class Broker(ABC):
    #: short identifier used in logs and /health
    name: str = "broker"
    #: True if separate processes (e.g. the CLI) reach the same broker state. False for the
    #: in-process SimBroker, whose orders only the server process can cancel.
    shared_across_processes: bool = True

    @abstractmethod
    def ping(self) -> None:
        """Cheap connectivity check; raises ``BrokerConnectionError`` on failure."""

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        """Place an order (idempotent on ``request.client_order_id``)."""

    @abstractmethod
    def get_order(self, broker_order_id: str) -> BrokerOrder:
        """Fetch one order; raises ``OrderNotFoundError`` if unknown."""

    @abstractmethod
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Fetch one order by our id; ``None`` if the broker has never seen it."""

    @abstractmethod
    def list_orders(self) -> list[BrokerOrder]:
        """Recent orders, open and closed."""

    @abstractmethod
    def list_positions(self) -> list[BrokerPosition]:
        """Open positions (signed quantities)."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel one working order."""

    @abstractmethod
    def cancel_all_orders(self) -> list[str]:
        """Cancel every working order; returns the broker ids that were canceled."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release resources (HTTP clients etc.)."""
