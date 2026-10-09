"""SimBroker: an in-process paper broker.

Used by default and by every test. It models the behaviours that make order
handling hard, deterministically and without a network:

* market orders fill immediately at a reference price;
* marketable limits fill immediately, non-marketable limits rest until the
  price crosses (``set_price``);
* **partial fills**: the first chunk fills at once, the remainder after a delay
  measured on an injectable clock;
* **rejects**: scripted or random;
* **connection failures** and **lost responses** (the order *was* accepted but
  the caller sees an error), which is the case idempotency exists for;
* simulated latency.

Test hooks (``script``, ``set_online``, ``set_price``, ``inject_position``,
``inject_external_order``) let a test put the broker into any state, including
states the bridge did not cause, which is how reconciliation is exercised.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.errors import (
    BrokerConnectionError,
    OrderNotCancelableError,
    OrderNotFoundError,
    OrderRejectedError,
)
from tv_broker_bridge.models import (
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)


@dataclass(frozen=True, slots=True)
class SimConfig:
    latency_s: float = 0.0
    default_price: Decimal = Decimal("100.00")
    partial_fill_probability: float = 0.0
    partial_fill_delay_s: float = 2.0
    reject_probability: float = 0.0
    seed: int | None = None


class BehaviorKind(StrEnum):
    FILL = "fill"  # fill completely (overrides random behaviour)
    PARTIAL = "partial"  # fill part now, the rest after partial_fill_delay_s
    REST = "rest"  # accept and leave working, no fill
    REJECT = "reject"  # definitive broker rejection
    CONNECTION_FAILURE = "connection_failure"  # request never reaches the broker
    LOST_RESPONSE = "lost_response"  # broker processes the order, response is lost


@dataclass(frozen=True, slots=True)
class Behavior:
    kind: BehaviorKind
    reason: str = "rejected by sim broker"
    partial_qty: Decimal | None = None  # PARTIAL only; default is half, rounded down (min 1)


@dataclass
class _SimOrder:
    broker_order_id: str
    request: OrderRequest
    status: OrderStatus = OrderStatus.ACCEPTED
    filled_qty: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)  # sum(fill_qty * price), for the average
    pending_fills: list[tuple[float, Decimal]] = field(default_factory=list)  # (due, qty)

    @property
    def avg_price(self) -> Decimal | None:
        return None if self.filled_qty == 0 else self.notional / self.filled_qty

    def snapshot(self) -> BrokerOrder:
        r = self.request
        return BrokerOrder(
            broker_order_id=self.broker_order_id,
            client_order_id=r.client_order_id,
            symbol=r.symbol,
            side=r.side,
            qty=r.qty,
            order_type=r.order_type,
            status=self.status,
            filled_qty=self.filled_qty,
            limit_price=r.limit_price,
            avg_fill_price=self.avg_price,
        )


class SimBroker(Broker):
    name = "sim"
    shared_across_processes = False

    def __init__(
        self,
        config: SimConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or SimConfig()
        self._clock = clock
        self._sleep = sleep
        self._rng = random.Random(self.config.seed)  # noqa: S311 - simulation, not security
        self._lock = threading.RLock()
        self._orders: dict[str, _SimOrder] = {}
        self._by_client: dict[str, str] = {}
        self._positions: dict[str, Decimal] = {}
        self._prices: dict[str, Decimal] = {}
        self._script: deque[Behavior] = deque()
        self._online = True
        self._seq = 0

    # -- test / demo hooks -------------------------------------------------------------------
    def script(self, *behaviors: Behavior) -> None:
        """Queue behaviours for upcoming submit calls (consumed in order)."""
        with self._lock:
            self._script.extend(behaviors)

    def set_online(self, online: bool) -> None:
        with self._lock:
            self._online = online

    def set_price(self, symbol: str, price: Decimal) -> None:
        """Move the market; resting limit orders that become marketable fill."""
        with self._lock:
            self._prices[symbol] = price
            self._match_resting(symbol)

    def inject_position(self, symbol: str, qty: Decimal) -> None:
        """Change a position behind the bridge's back (e.g. a manual trade)."""
        with self._lock:
            self._positions[symbol] = qty

    def inject_external_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        limit_price: Decimal,
        client_order_id: str = "external-1",
    ) -> BrokerOrder:
        """Create a resting limit order the bridge never sent."""
        with self._lock:
            order = self._create(
                OrderRequest(client_order_id, symbol, side, qty, OrderType.LIMIT, limit_price)
            )
            return order.snapshot()

    def forget_order(self, client_order_id: str) -> None:
        """Make the broker lose all trace of an order (it vanishes from every query)."""
        with self._lock:
            order_id = self._by_client.pop(client_order_id)
            del self._orders[order_id]

    def order_count(self) -> int:
        with self._lock:
            return len(self._orders)

    # -- Broker interface --------------------------------------------------------------------
    def ping(self) -> None:
        self._enter()

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        self._enter(consume_connection_failure=True)
        with self._lock:
            existing_id = self._by_client.get(request.client_order_id)
            if existing_id is not None:  # idempotent resubmission
                return self._orders[existing_id].snapshot()
            behavior = self._next_behavior()
            if behavior and behavior.kind is BehaviorKind.REJECT:
                raise OrderRejectedError(behavior.reason)
            if request.qty <= 0:
                raise OrderRejectedError("qty must be positive")
            order = self._create(request, behavior)
            if behavior and behavior.kind is BehaviorKind.LOST_RESPONSE:
                raise BrokerConnectionError("sim: response lost after the order was accepted")
            return order.snapshot()

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        self._enter()
        with self._lock:
            self._advance()
            try:
                return self._orders[broker_order_id].snapshot()
            except KeyError:
                raise OrderNotFoundError(broker_order_id) from None

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        self._enter()
        with self._lock:
            self._advance()
            order_id = self._by_client.get(client_order_id)
            return None if order_id is None else self._orders[order_id].snapshot()

    def list_orders(self) -> list[BrokerOrder]:
        self._enter()
        with self._lock:
            self._advance()
            return [o.snapshot() for o in self._orders.values()]

    def list_positions(self) -> list[BrokerPosition]:
        self._enter()
        with self._lock:
            self._advance()
            return [BrokerPosition(s, q) for s, q in sorted(self._positions.items()) if q != 0]

    def cancel_order(self, broker_order_id: str) -> None:
        self._enter()
        with self._lock:
            self._advance()
            order = self._orders.get(broker_order_id)
            if order is None:
                raise OrderNotFoundError(broker_order_id)
            if not order.status.is_working:
                raise OrderNotCancelableError(f"order is already {order.status.value}")
            self._cancel(order)

    def cancel_all_orders(self) -> list[str]:
        self._enter()
        with self._lock:
            self._advance()
            canceled = []
            for order in self._orders.values():
                if order.status.is_working:
                    self._cancel(order)
                    canceled.append(order.broker_order_id)
            return canceled

    # -- internals ---------------------------------------------------------------------------
    def _enter(self, *, consume_connection_failure: bool = False) -> None:
        """Common prologue: simulated latency, then the online/failure checks."""
        if self.config.latency_s > 0:
            self._sleep(self.config.latency_s)
        with self._lock:
            if not self._online:
                raise BrokerConnectionError("sim: broker is offline")
            if (
                consume_connection_failure
                and self._script
                and self._script[0].kind is BehaviorKind.CONNECTION_FAILURE
            ):
                self._script.popleft()
                raise BrokerConnectionError("sim: connection failed before reaching the broker")

    def _next_behavior(self) -> Behavior | None:
        if self._script:
            return self._script.popleft()
        if self._rng.random() < self.config.reject_probability:
            return Behavior(BehaviorKind.REJECT, "sim: random reject")
        if self._rng.random() < self.config.partial_fill_probability:
            return Behavior(BehaviorKind.PARTIAL)
        return None

    def _price(self, symbol: str) -> Decimal:
        return self._prices.get(symbol, self.config.default_price)

    def _create(self, request: OrderRequest, behavior: Behavior | None = None) -> _SimOrder:
        self._seq += 1
        order = _SimOrder(broker_order_id=f"sim-{self._seq:06d}", request=request)
        self._orders[order.broker_order_id] = order
        self._by_client[request.client_order_id] = order.broker_order_id

        kind = behavior.kind if behavior else None
        if kind is BehaviorKind.REST or not self._marketable(request):
            return order  # rests until the price crosses
        price = self._price(request.symbol)
        if kind is BehaviorKind.PARTIAL and request.qty > 1:
            first = behavior.partial_qty if behavior and behavior.partial_qty else None
            first = (
                first if first and 0 < first < request.qty else max(request.qty // 2, Decimal(1))
            )
            self._fill(order, first, price)
            order.pending_fills.append(
                (self._clock() + self.config.partial_fill_delay_s, request.qty - first)
            )
        else:
            self._fill(order, request.qty, price)
        return order

    def _marketable(self, request: OrderRequest) -> bool:
        if request.order_type is OrderType.MARKET or request.limit_price is None:
            return True
        price = self._price(request.symbol)
        if request.side is Side.BUY:
            return request.limit_price >= price
        return request.limit_price <= price

    def _fill(self, order: _SimOrder, qty: Decimal, price: Decimal) -> None:
        qty = min(qty, order.request.qty - order.filled_qty)
        if qty <= 0:
            return
        order.filled_qty += qty
        order.notional += qty * price
        symbol = order.request.symbol
        self._positions[symbol] = self._positions.get(symbol, Decimal(0)) + (
            order.request.side.sign * qty
        )
        order.status = (
            OrderStatus.FILLED
            if order.filled_qty >= order.request.qty
            else OrderStatus.PARTIALLY_FILLED
        )

    def _cancel(self, order: _SimOrder) -> None:
        order.pending_fills.clear()
        order.status = OrderStatus.CANCELED

    def _advance(self) -> None:
        """Apply time-driven fills that have come due."""
        now = self._clock()
        for order in self._orders.values():
            if not order.status.is_working or not order.pending_fills:
                continue
            due = [pf for pf in order.pending_fills if pf[0] <= now]
            for pf in due:
                order.pending_fills.remove(pf)
                self._fill(order, pf[1], self._price(order.request.symbol))

    def _match_resting(self, symbol: str) -> None:
        for order in self._orders.values():
            req = order.request
            if (
                req.symbol == symbol
                and order.status.is_working
                and not order.pending_fills
                and self._marketable(req)
            ):
                self._fill(order, req.qty - order.filled_qty, self._price(symbol))
