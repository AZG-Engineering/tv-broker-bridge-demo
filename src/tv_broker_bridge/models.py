"""Domain types shared by the store, the broker adapters and the pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    OTHER = "other"  # only ever seen on orders adopted from the broker (stop, trailing, ...)


class OrderStatus(StrEnum):
    """Lifecycle of an order as the bridge tracks it.

    ``PENDING_SUBMIT``, ``SUBMIT_UNKNOWN``, ``FAILED`` and ``MISSING`` are
    bridge-side states describing what we *know* about delivery; the rest map
    onto what a broker reports.
    """

    PENDING_SUBMIT = "pending_submit"  # reserved locally, not yet confirmed sent
    SUBMIT_UNKNOWN = "submit_unknown"  # send failed ambiguously; broker may have it
    ACCEPTED = "accepted"  # live at the broker, nothing filled yet
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"  # broker said no
    FAILED = "failed"  # confirmed never to have reached the broker
    MISSING = "missing"  # we believed it was live; broker has no trace of it

    @property
    def is_working(self) -> bool:
        """True while the order can still change position or exposure."""
        return self in WORKING_STATUSES

    @property
    def is_terminal(self) -> bool:
        return not self.is_working


WORKING_STATUSES = frozenset(
    {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.SUBMIT_UNKNOWN,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
    }
)


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType
    limit_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """An order as reported by a broker (the source of truth)."""

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType
    status: OrderStatus
    filled_qty: Decimal = Decimal(0)
    limit_price: Decimal | None = None
    avg_fill_price: Decimal | None = None
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """Signed quantity: positive is long, negative is short."""

    symbol: str
    qty: Decimal


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """The bridge's own record of an order."""

    client_order_id: str
    alert_id: str | None  # None for orders adopted from the broker
    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType
    status: OrderStatus
    created_at: str
    updated_at: str
    limit_price: Decimal | None = None
    broker_order_id: str | None = None
    filled_qty: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    reject_reason: str | None = None
    source: str = "bridge"  # "bridge" = created from an alert; "external" = adopted from broker

    @property
    def remaining_qty(self) -> Decimal:
        return max(self.qty - self.filled_qty, Decimal(0))

    @property
    def signed_remaining(self) -> Decimal:
        """Exposure this order could still add (positive = long)."""
        return self.side.sign * self.remaining_qty
