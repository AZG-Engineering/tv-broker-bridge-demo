"""Reconciliation: the broker is the source of truth; every mismatch is logged and alerted."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tests.conftest import FakeMonotonic, Harness, HarnessFactory, default_risk, make_alert
from tv_broker_bridge.broker.sim import Behavior, BehaviorKind, SimBroker, SimConfig
from tv_broker_bridge.errors import BrokerConnectionError, OrderNotFoundError
from tv_broker_bridge.models import BrokerOrder, BrokerPosition, OrderRequest, OrderStatus, Side
from tv_broker_bridge.schemas import validate_alert

D = Decimal


class TestCleanState:
    def test_matching_state_reports_clean_and_logs_it(self, h: Harness, logs: Any) -> None:
        h.post(alert_id="a1")
        h.post(alert_id="a2", order_type="limit", limit_price=90)
        report = h.reconcile()

        assert report.ok
        assert report.clean
        assert report.orders_seen == 2
        event = logs.events("reconciled")[-1]
        assert event["clean"] is True
        assert event["mismatches"] == 0
        assert event["level"] == "INFO"
        assert h.alerts.sent == []
        assert logs.events("reconcile_mismatch") == []

    def test_empty_system_is_clean(self, h: Harness) -> None:
        assert h.reconcile().clean

    def test_running_twice_is_idempotent(self, h: Harness) -> None:
        h.post()
        h.broker.inject_position("AAPL", D(99))
        first = h.reconcile()
        second = h.reconcile()
        assert len(first.mismatches) == 1
        assert second.clean


class TestPositionMismatch:
    def test_broker_position_wins_and_the_difference_is_alerted(
        self, h: Harness, logs: Any
    ) -> None:
        h.post()  # local and broker both say AAPL +10
        h.broker.inject_position("AAPL", D(500))  # e.g. a manual trade in the broker UI

        report = h.reconcile()

        assert [m.kind for m in report.mismatches] == ["position_mismatch"]
        assert h.position() == D(500)  # local state replaced by the broker's
        kind, message, details = h.alerts.sent[0]
        assert kind == "position_mismatch"
        assert "local position 10" in message
        assert "broker position 500" in message
        assert details["symbol"] == "AAPL"
        event = logs.events("reconcile_mismatch")[0]
        assert event["level"] == "ERROR"
        assert event["kind"] == "position_mismatch"
        assert logs.events("reconciled")[-1]["clean"] is False
        assert h.reconcile().clean  # corrected: the next pass is quiet

    def test_position_the_bridge_never_knew_about_is_adopted(self, h: Harness) -> None:
        h.broker.inject_position("MSFT", D(-7))
        report = h.reconcile()
        assert [m.symbol for m in report.mismatches] == ["MSFT"]
        assert h.position("MSFT") == D(-7)

    def test_local_position_the_broker_does_not_have_is_zeroed(self, h: Harness) -> None:
        h.post()
        h.broker.inject_position("AAPL", D(0))
        assert h.reconcile().mismatches[0].kind == "position_mismatch"
        assert h.position() == D(0)

    def test_risk_gate_sees_the_corrected_position(self, make_harness: HarnessFactory) -> None:
        h = make_harness(risk=default_risk(max_position_per_symbol=100))
        h.broker.inject_position("AAPL", D(95))
        assert h.post(alert_id="before", qty=10).json()["status"] == "accepted"  # local says 0
        h.reconcile()
        assert h.post(alert_id="after", qty=10).json()["status"] == "rejected_by_risk"


class TestUnknownBrokerOrder:
    def test_order_the_bridge_never_sent_is_flagged_and_adopted(
        self, h: Harness, logs: Any
    ) -> None:
        h.broker.inject_external_order("AAPL", Side.BUY, D(200), D("50"))

        report = h.reconcile()

        assert [m.kind for m in report.mismatches] == ["unknown_broker_order"]
        assert h.alerts.kinds == ["unknown_broker_order"]
        adopted = h.rt.store.get_order("external-1")
        assert adopted is not None
        assert adopted.source == "external"
        assert adopted.status is OrderStatus.ACCEPTED
        assert h.reconcile().clean

    def test_adopted_orders_count_toward_risk_exposure(self, h: Harness) -> None:
        h.broker.inject_external_order("AAPL", Side.BUY, D(200), D("50"))
        h.reconcile()
        # 200 working + 100 requested > 250
        assert h.post(qty=100).json()["status"] == "rejected_by_risk"

    def test_closed_historical_orders_are_not_alerted(self, h: Harness) -> None:
        h.broker.inject_external_order("AAPL", Side.BUY, D(5), D("50"))
        h.broker.cancel_all_orders()
        assert h.reconcile().clean

    def test_adopted_order_does_not_double_book_when_it_later_fills(self, h: Harness) -> None:
        h.broker.inject_external_order("AAPL", Side.BUY, D(10), D("90"))
        h.reconcile()
        h.broker.set_price("AAPL", D("85"))  # fills at the broker: position is now 10
        report = h.reconcile()
        assert h.position() == D(10)  # progress applied once; no phantom mismatch
        assert report.clean


class TestLocalOrderMissingAtBroker:
    def test_working_order_with_no_trace_is_flagged(self, h: Harness, logs: Any) -> None:
        h.post(order_type="limit", limit_price=90)
        order = h.order_for("alert-0001")
        h.broker.forget_order(order.client_order_id)

        report = h.reconcile()

        assert [m.kind for m in report.mismatches] == ["missing_at_broker"]
        assert h.order_for("alert-0001").status is OrderStatus.MISSING
        assert h.alerts.kinds == ["missing_at_broker"]
        assert h.reconcile().clean
        # and it no longer counts as live exposure
        assert h.rt.store.working_exposure("AAPL") == D(0)

    def test_broker_rejected_orders_are_not_expected_at_the_broker(self, h: Harness) -> None:
        h.broker.script(Behavior(BehaviorKind.REJECT))
        h.post()
        assert h.reconcile().clean


class TestOrderStateConflicts:
    def test_local_fill_the_broker_does_not_confirm_is_a_conflict(self, h: Harness) -> None:
        h.post(order_type="limit", limit_price=90)  # working at the broker, nothing filled
        order = h.order_for("alert-0001")
        # Corrupt local state: bookkeeping claims it filled.
        h.rt.store.update_order(
            order.client_order_id,
            status=OrderStatus.FILLED,
            filled_qty=D(10),
            now=h.clock(),
        )
        h.rt.store.add_to_position("AAPL", D(10), h.clock())

        report = h.reconcile()

        kinds = {m.kind for m in report.mismatches}
        assert kinds == {"terminal_state_conflict", "position_mismatch"}
        fixed = h.order_for("alert-0001")
        assert fixed.status is OrderStatus.ACCEPTED  # broker's view
        assert fixed.filled_qty == D(0)
        assert h.position() == D(0)
        assert h.reconcile().clean

    def test_fill_count_that_goes_backwards_is_flagged(self, h: Harness) -> None:
        h.post(order_type="limit", limit_price=90)
        order = h.order_for("alert-0001")
        h.rt.store.update_order(
            order.client_order_id,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_qty=D(4),
            now=h.clock(),
        )
        h.rt.store.add_to_position("AAPL", D(4), h.clock())

        report = h.reconcile()

        assert "fill_regression" in {m.kind for m in report.mismatches}
        assert h.order_for("alert-0001").filled_qty == D(0)
        assert h.position() == D(0)

    def test_local_cancel_the_broker_disagrees_with_is_a_conflict(self, h: Harness) -> None:
        h.post(order_type="limit", limit_price=90)
        order = h.order_for("alert-0001")
        h.rt.store.update_order(order.client_order_id, status=OrderStatus.CANCELED, now=h.clock())
        report = h.reconcile()
        assert [m.kind for m in report.mismatches] == ["terminal_state_conflict"]
        assert h.order_for("alert-0001").status is OrderStatus.ACCEPTED


class _RacyBroker(SimBroker):
    """Fills an order *between* our orders request and our positions request."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(SimConfig(), **kwargs)
        self.race_once = False

    def list_orders(self) -> list[BrokerOrder]:
        snapshot = super().list_orders()
        if self.race_once:
            self.race_once = False
            self.set_price("AAPL", D("90"))  # resting buy limit at 95 now fills
        return snapshot


class _TruncatingBroker(SimBroker):
    """list_orders returns nothing (as if the order is older than the page size)."""

    def list_orders(self) -> list[BrokerOrder]:
        return []


class TestConsistencyAndRobustness:
    def test_a_fill_landing_between_the_two_requests_is_not_a_false_alarm(
        self, make_harness: HarnessFactory
    ) -> None:
        mono = FakeMonotonic()
        racy = _RacyBroker(clock=mono, sleep=lambda _s: None)
        h = make_harness(broker=racy)
        h.post(order_type="limit", limit_price=95)
        racy.race_once = True

        report = h.reconcile()

        # First snapshot: order still ACCEPTED but position already +10 -> looks like a
        # mismatch. The confirming second snapshot shows a consistent fill, so nothing alerts.
        assert report.clean, report.mismatches
        assert report.updates == 1
        assert h.position() == D(10)
        assert h.alerts.sent == []

    def test_working_orders_missing_from_the_list_are_looked_up_directly(
        self, make_harness: HarnessFactory
    ) -> None:
        h = make_harness(broker=_TruncatingBroker())
        h.post(order_type="limit", limit_price=90)
        report = h.reconcile()
        assert report.clean
        assert h.order_for("alert-0001").status is OrderStatus.ACCEPTED

    def test_a_pass_is_discarded_if_local_state_changes_while_it_is_observing(
        self, h: Harness
    ) -> None:
        h.post(order_type="limit", limit_price=90)
        h.broker.set_price("AAPL", D("85"))
        rec = h.rt.reconciler
        token, _snapshot, findings = rec._observe()
        assert [f.kind for f in findings] == ["progress"]
        # The live submit path writes to the same order after we read it...
        order = h.order_for("alert-0001")
        h.rt.store.update_order(order.client_order_id, status=OrderStatus.ACCEPTED, now=h.clock())
        # ...so applying the now-stale findings must not clobber anything.
        assert rec._apply(findings, token) is None
        assert h.position() == D(0)
        # The next pass starts from a settled picture and applies it properly.
        assert h.reconcile().clean
        assert h.position() == D(10)

    def test_a_skipped_pass_is_reported_and_logged(self, h: Harness, logs: Any) -> None:
        h.post(order_type="limit", limit_price=90)
        rec = h.rt.reconciler
        real_observe = rec._observe

        def observe_then_a_write_lands() -> Any:
            result = real_observe()
            h.rt.store.set_position("MSFT", D(1), h.clock())  # any concurrent write
            return result

        rec._observe = observe_then_a_write_lands  # type: ignore[method-assign]
        report = rec.run_once()
        assert report.skipped
        assert report.ok
        assert report.mismatches == ()
        assert logs.events("reconcile_skipped")
        assert h.alerts.sent == []

    def test_cancelled_by_someone_else_is_learned_as_progress(self, h: Harness) -> None:
        h.post(order_type="limit", limit_price=90)
        h.broker.cancel_all_orders()
        report = h.reconcile()
        assert report.clean
        assert report.updates == 1
        assert h.order_for("alert-0001").status is OrderStatus.CANCELED


class TestBrokerErrorsDuringReconcile:
    def test_unreachable_broker_is_reported_not_raised_then_recovers(
        self, h: Harness, logs: Any
    ) -> None:
        h.post()
        h.broker.set_online(False)

        report = h.reconcile()
        assert report.ok is False
        assert "offline" in (report.error or "")
        assert h.rt.status.reconcile().consecutive_failures == 1
        failed = logs.events("reconcile_failed")
        assert failed[0]["level"] == "ERROR"
        assert failed[0]["consecutive_failures"] == 1
        assert "reconciled" not in logs.names()[-1:]

        h.reconcile()
        assert h.rt.status.reconcile().consecutive_failures == 2

        h.broker.set_online(True)  # reconnect
        recovered = h.reconcile()
        assert recovered.ok
        assert recovered.clean
        assert h.rt.status.reconcile().consecutive_failures == 0
        reconnected = logs.events("broker_reconnected")
        assert len(reconnected) == 1
        assert reconnected[0]["failed_passes"] == 2

    def test_a_failure_midway_leaves_local_state_untouched(self, h: Harness) -> None:
        h.post(order_type="limit", limit_price=90)
        before = h.order_for("alert-0001")

        def explode() -> list[BrokerPosition]:
            raise BrokerConnectionError("positions endpoint timed out")

        h.broker.list_positions = explode  # type: ignore[method-assign]
        report = h.reconcile()
        assert report.ok is False
        assert h.order_for("alert-0001") == before

    def test_order_lookup_not_found_is_handled(self, h: Harness) -> None:
        h.post(order_type="limit", limit_price=90)

        def not_found(_id: str) -> BrokerOrder:
            raise OrderNotFoundError(_id)

        h.broker.list_orders = lambda: []  # type: ignore[method-assign]
        h.broker.get_order = not_found  # type: ignore[method-assign,assignment]
        report = h.reconcile()
        assert [m.kind for m in report.mismatches] == ["missing_at_broker"]


class _ReconcileDuringSubmit(SimBroker):
    """Runs a reconciliation pass at the worst moment: order exists at the broker, but the
    submit path has not yet recorded the response."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.during_submit: Any = None
        self.report: Any = None

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        order = super().submit_order(request)
        if self.during_submit is not None:
            self.report = self.during_submit()
        return order


class TestRacesWithTheLiveSubmitPath:
    def test_a_submit_still_in_flight_is_not_reported_as_a_lost_response(
        self, make_harness: HarnessFactory
    ) -> None:
        broker = _ReconcileDuringSubmit(clock=FakeMonotonic(), sleep=lambda _s: None)
        h = make_harness(broker=broker)
        broker.during_submit = h.reconcile

        assert h.post().json()["status"] == "accepted"

        # The mid-submit pass saw PENDING_SUBMIT locally and the order at the broker. It must
        # leave both alone: the submit path owns the row and records it a moment later.
        assert broker.report.clean
        assert h.alerts.sent == []
        assert h.order_for("alert-0001").status is OrderStatus.FILLED
        assert h.position() == D(10)  # booked once, by the submit path
        assert h.reconcile().clean

    def test_position_findings_are_dropped_if_the_position_moved_since_they_were_read(
        self, h: Harness
    ) -> None:
        h.post()  # local +10
        h.broker.inject_position("AAPL", D(40))
        token, _snapshot, findings = h.rt.reconciler._observe()
        assert [f.kind for f in findings] == ["position_mismatch"]
        # A fill is booked by the live path between our read and our write...
        h.rt.store.add_to_position("AAPL", D(5), h.clock())
        assert h.rt.reconciler._apply(findings, token) is None
        assert h.position() == D(15)  # not clobbered with a stale correction
        assert [m.kind for m in h.reconcile().mismatches] == ["position_mismatch"]  # next pass
        assert h.position() == D(40)

    def test_an_order_reserved_and_sent_after_the_local_read_is_not_adopted_as_foreign(
        self, make_harness: HarnessFactory
    ) -> None:
        # During the confirming observation an alert is reserved locally and its order reaches
        # the broker, but the submit path has not recorded the response yet. The reconciler's
        # local read predates the reservation, so the order looks like one the bridge never
        # sent. Adopting it would collide with the real row; the change token must stop that.
        h = make_harness()
        h.broker.inject_position("MSFT", D(5))  # forces a confirming observation
        alert = validate_alert(
            make_alert(alert_id="reserved-mid-pass", order_type="limit", limit_price=90)
        )
        original = h.rt.reconciler._snapshot
        calls = {"n": 0}

        def snapshot(local_working: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                claim = h.rt.processor._claim(alert)  # reserve locally (PENDING_SUBMIT)
                assert claim.order is not None
                h.broker.submit_order(h.rt.processor._request_for(claim.order))  # reaches broker
            return original(local_working)

        h.rt.reconciler._snapshot = snapshot  # type: ignore[method-assign]
        report = h.rt.reconciler.run_once()  # must not raise an IntegrityError

        assert report.skipped
        assert h.alerts.sent == []
        order = h.order_for("reserved-mid-pass")
        assert order is not None
        assert order.source == "bridge"
        # The submit path then finishes normally (idempotent resubmit returns the same order).
        outcome = h.rt.processor._deliver(order, retry=False)
        assert outcome.status == "accepted"
        assert h.broker.order_count() == 1
        later = h.reconcile()
        assert [m.symbol for m in later.mismatches] == ["MSFT"]  # only the genuine mismatch

    def test_an_alert_that_lands_between_the_local_read_and_the_broker_read_is_not_double_booked(
        self, make_harness: HarnessFactory
    ) -> None:
        # Worst case for the change token: during the *confirming* observation the entire alert
        # (reserve, fill at the broker, book) happens after local state was read but before the
        # broker's was. Without the guard the broker's +10 looks like a stray position, is
        # written over the local one, and the booking then adds another +10 (20, not 10).
        h = make_harness()
        h.broker.inject_position("MSFT", D(5))  # a real mismatch, so a confirm pass happens
        original = h.rt.reconciler._snapshot
        calls = {"n": 0}

        def snapshot(local_working: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:  # the confirming observation
                assert h.post(alert_id="landed-mid-pass").json()["status"] == "accepted"
            return original(local_working)

        h.rt.reconciler._snapshot = snapshot  # type: ignore[method-assign]
        report = h.rt.reconciler.run_once()

        assert calls["n"] == 2
        assert report.skipped  # stale picture: discarded, nothing applied
        assert report.mismatches == ()
        assert h.alerts.sent == []
        assert h.position() == D(10)  # booked exactly once
        assert h.position("MSFT") == D(0)  # the real mismatch is untouched until a clean pass

        later = h.reconcile()  # next pass sees a settled picture
        assert [m.symbol for m in later.mismatches] == ["MSFT"]
        assert h.position() == D(10)
        assert h.position("MSFT") == D(5)
