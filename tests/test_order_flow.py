"""Order lifecycle through the webhook: fills, partial fills, broker rejects."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tests.conftest import Harness, HarnessFactory, default_risk
from tv_broker_bridge.broker.sim import Behavior, BehaviorKind
from tv_broker_bridge.models import OrderStatus
from tv_broker_bridge.store import AlertState

D = Decimal


class TestFullFill:
    def test_market_order_is_sent_filled_and_booked(self, h: Harness, logs: Any) -> None:
        response = h.post(qty=10)
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "accepted"
        assert body["order"]["status"] == "filled"
        assert body["order"]["filled_qty"] == "10"

        order = h.order_for("alert-0001")
        assert order.status is OrderStatus.FILLED
        assert order.filled_qty == D(10)
        assert order.avg_fill_price == D("100.00")
        assert h.position() == D(10)
        assert h.rt.store.get_alert("alert-0001").state is AlertState.ACCEPTED  # type: ignore[union-attr]

    def test_the_decision_trail_is_in_the_log_in_order(self, h: Harness, logs: Any) -> None:
        h.post()
        assert logs.names() == ["received", "sent", "filled"]
        sent, filled = logs.events("sent")[0], logs.events("filled")[0]
        assert (
            sent["client_order_id"]
            == filled["client_order_id"]
            == h.order_for("alert-0001").client_order_id
        )
        assert filled["fill_qty"] == "10"
        assert filled["partial"] is False

    def test_sell_reduces_the_position(self, h: Harness) -> None:
        h.post(alert_id="b", qty=30)
        h.post(alert_id="s", side="sell", qty=10)
        assert h.position() == D(20)

    def test_resting_limit_order_fills_later_and_is_learned_by_reconciliation(
        self, h: Harness, logs: Any
    ) -> None:
        body = h.post(order_type="limit", limit_price=95).json()
        assert body["order"]["status"] == "accepted"  # working at the broker, nothing filled
        assert h.position() == D(0)

        h.broker.set_price("AAPL", D("94"))
        report = h.reconcile()

        assert report.clean  # a fill arriving is progress, not a mismatch
        assert report.updates == 1
        assert h.order_for("alert-0001").status is OrderStatus.FILLED
        assert h.position() == D(10)
        assert h.alerts.sent == []
        assert logs.events("filled")[0]["fill_qty"] == "10"


class TestPartialFills:
    def test_partial_fill_books_only_what_filled_and_keeps_the_rest_working(
        self, h: Harness, logs: Any
    ) -> None:
        h.broker.script(Behavior(BehaviorKind.PARTIAL))
        body = h.post(qty=10).json()

        assert body["status"] == "accepted"
        assert body["order"]["status"] == "partially_filled"
        assert body["order"]["filled_qty"] == "5"
        order = h.order_for("alert-0001")
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.remaining_qty == D(5)
        assert h.position() == D(5)
        filled = logs.events("filled")
        assert len(filled) == 1
        assert filled[0]["partial"] is True
        assert filled[0]["fill_qty"] == "5"
        assert filled[0]["remaining_qty"] == "5"

    def test_remaining_quantity_still_counts_as_exposure(
        self, make_harness: HarnessFactory
    ) -> None:
        h = make_harness(risk=default_risk(max_position_per_symbol=12))
        h.broker.script(Behavior(BehaviorKind.PARTIAL))
        assert h.post(alert_id="a1", qty=10).json()["status"] == "accepted"
        # 5 held + 5 still working = 10 of exposure; another 5 would breach the limit of 12.
        assert h.post(alert_id="a2", qty=5).json()["status"] == "rejected_by_risk"
        assert h.post(alert_id="a3", qty=2).json()["status"] == "accepted"

    def test_the_remainder_is_booked_when_it_fills(self, h: Harness, logs: Any) -> None:
        h.broker.script(Behavior(BehaviorKind.PARTIAL))
        h.post(qty=10)

        h.mono.advance(1)
        assert h.reconcile().updates == 0  # remainder not due yet
        assert h.position() == D(5)

        h.mono.advance(5)
        report = h.reconcile()

        assert report.clean
        assert report.updates == 1
        order = h.order_for("alert-0001")
        assert order.status is OrderStatus.FILLED
        assert order.filled_qty == D(10)
        assert h.position() == D(10)
        fills = logs.events("filled")
        assert [f["fill_qty"] for f in fills] == ["5", "5"]
        assert [f["partial"] for f in fills] == [True, False]
        assert h.alerts.sent == []

    def test_applying_the_same_broker_report_twice_does_not_double_count(self, h: Harness) -> None:
        h.broker.script(Behavior(BehaviorKind.PARTIAL))
        h.post(qty=10)
        order = h.order_for("alert-0001")
        broker_order = h.broker.get_order(order.broker_order_id)
        h.rt.ledger.apply_broker_order(order, broker_order)
        h.rt.ledger.apply_broker_order(order, broker_order)
        assert h.position() == D(5)

    def test_cancelled_remainder_leaves_the_partial_fill_booked(self, h: Harness) -> None:
        h.broker.script(Behavior(BehaviorKind.PARTIAL))
        h.post(qty=10)
        h.broker.cancel_all_orders()
        assert h.reconcile().clean
        order = h.order_for("alert-0001")
        assert order.status is OrderStatus.CANCELED
        assert order.filled_qty == D(5)
        assert h.position() == D(5)


class TestBrokerReject:
    def test_reject_is_recorded_logged_and_leaves_no_position(self, h: Harness, logs: Any) -> None:
        h.broker.script(Behavior(BehaviorKind.REJECT, "insufficient buying power"))
        response = h.post()

        assert response.status_code == 200
        assert response.json() == {
            "status": "rejected_by_broker",
            "alert_id": "alert-0001",
            "reason": "insufficient buying power",
        }
        order = h.order_for("alert-0001")
        assert order.status is OrderStatus.REJECTED
        assert order.reject_reason == "insufficient buying power"
        assert h.rt.store.get_alert("alert-0001").state is AlertState.REJECTED_BROKER  # type: ignore[union-attr]
        assert h.position() == D(0)
        event = logs.events("broker_rejected")[0]
        assert event["reason"] == "insufficient buying power"
        assert event["level"] == "WARNING"
        assert "filled" not in logs.names()

    def test_a_reject_does_not_poison_later_alerts(self, h: Harness) -> None:
        h.broker.script(Behavior(BehaviorKind.REJECT))
        assert h.post(alert_id="x1").json()["status"] == "rejected_by_broker"
        assert h.post(alert_id="x2").json()["status"] == "accepted"
        assert h.position() == D(10)

    def test_reconciliation_after_a_reject_is_clean(self, h: Harness) -> None:
        h.broker.script(Behavior(BehaviorKind.REJECT))
        h.post()
        assert h.reconcile().clean
