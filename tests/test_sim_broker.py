"""SimBroker behaves like a broker: fills, partial fills, rejects, latency, idempotency."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import FakeMonotonic
from tv_broker_bridge.broker.sim import Behavior, BehaviorKind, SimBroker, SimConfig
from tv_broker_bridge.errors import (
    BrokerConnectionError,
    OrderNotCancelableError,
    OrderNotFoundError,
    OrderRejectedError,
)
from tv_broker_bridge.models import OrderRequest, OrderStatus, OrderType, Side

D = Decimal


def req(
    cid: str = "c1",
    *,
    side: Side = Side.BUY,
    qty: int = 10,
    order_type: OrderType = OrderType.MARKET,
    limit: str | None = None,
    symbol: str = "AAPL",
) -> OrderRequest:
    return OrderRequest(cid, symbol, side, D(qty), order_type, None if limit is None else D(limit))


@pytest.fixture
def mono() -> FakeMonotonic:
    return FakeMonotonic()


@pytest.fixture
def sim(mono: FakeMonotonic) -> SimBroker:
    return SimBroker(SimConfig(partial_fill_delay_s=2.0), clock=mono, sleep=lambda _s: None)


class TestFills:
    def test_market_order_fills_immediately_at_the_reference_price(self, sim: SimBroker) -> None:
        order = sim.submit_order(req(qty=10))
        assert order.status is OrderStatus.FILLED
        assert order.filled_qty == D(10)
        assert order.avg_fill_price == D("100.00")
        assert [(p.symbol, p.qty) for p in sim.list_positions()] == [("AAPL", D(10))]

    def test_sells_reduce_and_can_flip_the_position(self, sim: SimBroker) -> None:
        sim.submit_order(req("b", qty=10))
        sim.submit_order(req("s", side=Side.SELL, qty=25))
        assert sim.list_positions()[0].qty == D(-15)

    def test_flat_positions_are_not_reported(self, sim: SimBroker) -> None:
        sim.submit_order(req("b", qty=10))
        sim.submit_order(req("s", side=Side.SELL, qty=10))
        assert sim.list_positions() == []

    def test_marketable_limit_fills_at_the_market_price_not_the_limit(self, sim: SimBroker) -> None:
        order = sim.submit_order(req(order_type=OrderType.LIMIT, limit="105"))
        assert order.status is OrderStatus.FILLED
        assert order.avg_fill_price == D("100.00")

    def test_non_marketable_limit_rests_until_the_price_crosses(self, sim: SimBroker) -> None:
        order = sim.submit_order(req(order_type=OrderType.LIMIT, limit="95"))
        assert order.status is OrderStatus.ACCEPTED
        assert order.filled_qty == 0
        sim.set_price("AAPL", D("96"))
        assert sim.get_order(order.broker_order_id).status is OrderStatus.ACCEPTED
        sim.set_price("AAPL", D("94.50"))
        filled = sim.get_order(order.broker_order_id)
        assert filled.status is OrderStatus.FILLED
        assert filled.avg_fill_price == D("94.50")

    def test_resting_sell_limit_fills_when_price_rises(self, sim: SimBroker) -> None:
        order = sim.submit_order(req(side=Side.SELL, order_type=OrderType.LIMIT, limit="110"))
        assert order.status is OrderStatus.ACCEPTED
        sim.set_price("AAPL", D("111"))
        assert sim.get_order(order.broker_order_id).status is OrderStatus.FILLED

    def test_non_positive_quantity_is_rejected(self, sim: SimBroker) -> None:
        with pytest.raises(OrderRejectedError):
            sim.submit_order(req(qty=0))
        assert sim.order_count() == 0


class TestPartialFills:
    def test_first_chunk_fills_now_and_the_rest_after_the_delay(
        self, sim: SimBroker, mono: FakeMonotonic
    ) -> None:
        sim.script(Behavior(BehaviorKind.PARTIAL))
        order = sim.submit_order(req(qty=10))
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.filled_qty == D(5)
        assert sim.list_positions()[0].qty == D(5)

        mono.advance(1.9)
        assert sim.get_order(order.broker_order_id).filled_qty == D(5)  # not due yet
        mono.advance(0.2)
        done = sim.get_order(order.broker_order_id)
        assert done.status is OrderStatus.FILLED
        assert done.filled_qty == D(10)
        assert sim.list_positions()[0].qty == D(10)

    def test_explicit_partial_quantity(self, sim: SimBroker) -> None:
        sim.script(Behavior(BehaviorKind.PARTIAL, partial_qty=D(3)))
        assert sim.submit_order(req(qty=10)).filled_qty == D(3)

    def test_average_price_blends_the_chunks(self, sim: SimBroker, mono: FakeMonotonic) -> None:
        sim.script(Behavior(BehaviorKind.PARTIAL))
        order = sim.submit_order(req(qty=10))
        sim.set_price("AAPL", D("110"))
        mono.advance(3)
        assert sim.get_order(order.broker_order_id).avg_fill_price == D("105")

    def test_cancelling_a_partially_filled_order_keeps_the_fills(
        self, sim: SimBroker, mono: FakeMonotonic
    ) -> None:
        sim.script(Behavior(BehaviorKind.PARTIAL))
        order = sim.submit_order(req(qty=10))
        sim.cancel_order(order.broker_order_id)
        mono.advance(10)  # the pending chunk must not materialise after cancel
        final = sim.get_order(order.broker_order_id)
        assert final.status is OrderStatus.CANCELED
        assert final.filled_qty == D(5)
        assert sim.list_positions()[0].qty == D(5)

    def test_a_one_share_order_cannot_be_split(self, sim: SimBroker) -> None:
        sim.script(Behavior(BehaviorKind.PARTIAL))
        assert sim.submit_order(req(qty=1)).status is OrderStatus.FILLED

    def test_random_partial_fills_are_seeded_and_reproducible(self, mono: FakeMonotonic) -> None:
        def run() -> list[OrderStatus]:
            broker = SimBroker(
                SimConfig(partial_fill_probability=0.5, seed=7), clock=mono, sleep=lambda _s: None
            )
            return [broker.submit_order(req(f"c{i}", qty=10)).status for i in range(20)]

        first = run()
        assert first == run()
        assert OrderStatus.PARTIALLY_FILLED in first
        assert OrderStatus.FILLED in first


class TestRejectsAndFailures:
    def test_scripted_reject_creates_no_order(self, sim: SimBroker) -> None:
        sim.script(Behavior(BehaviorKind.REJECT, "insufficient buying power"))
        with pytest.raises(OrderRejectedError, match="insufficient buying power"):
            sim.submit_order(req())
        assert sim.order_count() == 0
        assert sim.submit_order(req()).status is OrderStatus.FILLED  # script consumed

    def test_random_rejects(self, mono: FakeMonotonic) -> None:
        broker = SimBroker(SimConfig(reject_probability=1.0), clock=mono, sleep=lambda _s: None)
        with pytest.raises(OrderRejectedError):
            broker.submit_order(req())

    def test_connection_failure_means_the_order_never_arrived(self, sim: SimBroker) -> None:
        sim.script(Behavior(BehaviorKind.CONNECTION_FAILURE))
        with pytest.raises(BrokerConnectionError):
            sim.submit_order(req())
        assert sim.order_count() == 0
        assert sim.submit_order(req()).status is OrderStatus.FILLED

    def test_lost_response_means_the_order_exists_but_the_caller_does_not_know(
        self, sim: SimBroker
    ) -> None:
        sim.script(Behavior(BehaviorKind.LOST_RESPONSE))
        with pytest.raises(BrokerConnectionError):
            sim.submit_order(req("c-lost"))
        assert sim.order_count() == 1  # it really was placed
        # The recovery path: look it up, or resubmit; either yields the same single order.
        found = sim.get_order_by_client_id("c-lost")
        assert found is not None
        assert sim.submit_order(req("c-lost")).broker_order_id == found.broker_order_id
        assert sim.order_count() == 1

    def test_offline_broker_fails_every_call(self, sim: SimBroker) -> None:
        sim.set_online(False)
        for call in (
            sim.ping,
            sim.list_orders,
            sim.list_positions,
            sim.cancel_all_orders,
            lambda: sim.submit_order(req()),
            lambda: sim.get_order("x"),
            lambda: sim.get_order_by_client_id("x"),
        ):
            with pytest.raises(BrokerConnectionError):
                call()
        sim.set_online(True)
        sim.ping()


class TestIdempotencyAndCancel:
    def test_resubmitting_a_client_order_id_returns_the_same_order(self, sim: SimBroker) -> None:
        first = sim.submit_order(req("same"))
        second = sim.submit_order(req("same"))
        assert second.broker_order_id == first.broker_order_id
        assert sim.order_count() == 1
        assert sim.list_positions()[0].qty == D(10)  # filled once

    def test_resubmission_does_not_consume_scripted_behaviour(self, sim: SimBroker) -> None:
        sim.submit_order(req("same"))
        sim.script(Behavior(BehaviorKind.REJECT))
        assert sim.submit_order(req("same")).status is OrderStatus.FILLED
        with pytest.raises(OrderRejectedError):
            sim.submit_order(req("new"))

    def test_cancel_all_cancels_only_working_orders(self, sim: SimBroker) -> None:
        filled = sim.submit_order(req("f"))
        rest1 = sim.submit_order(req("r1", order_type=OrderType.LIMIT, limit="50"))
        rest2 = sim.submit_order(req("r2", order_type=OrderType.LIMIT, limit="50"))
        canceled = sim.cancel_all_orders()
        assert sorted(canceled) == sorted([rest1.broker_order_id, rest2.broker_order_id])
        assert sim.get_order(filled.broker_order_id).status is OrderStatus.FILLED
        assert sim.get_order(rest1.broker_order_id).status is OrderStatus.CANCELED

    def test_cancel_errors(self, sim: SimBroker) -> None:
        done = sim.submit_order(req("f"))
        with pytest.raises(OrderNotCancelableError):
            sim.cancel_order(done.broker_order_id)
        with pytest.raises(OrderNotFoundError):
            sim.cancel_order("nope")
        with pytest.raises(OrderNotFoundError):
            sim.get_order("nope")
        assert sim.get_order_by_client_id("nope") is None

    def test_latency_is_simulated_on_every_call(self, mono: FakeMonotonic) -> None:
        slept: list[float] = []
        broker = SimBroker(SimConfig(latency_s=0.25), clock=mono, sleep=slept.append)
        broker.submit_order(req())
        broker.list_orders()
        broker.ping()
        assert slept == [0.25, 0.25, 0.25]

    def test_external_order_and_position_hooks(self, sim: SimBroker) -> None:
        external = sim.inject_external_order("MSFT", Side.BUY, D(5), D("10"))
        assert external.status is OrderStatus.ACCEPTED
        sim.inject_position("MSFT", D(-3))
        assert sim.list_positions()[0].qty == D(-3)
        sim.forget_order("external-1")
        assert sim.list_orders() == []
