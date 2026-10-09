"""Broker failures: retry, reconnect, lost responses, and the 'outcome unknown' state."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tests.conftest import Harness, HarnessFactory, default_risk
from tv_broker_bridge.broker.sim import Behavior, BehaviorKind, SimBroker
from tv_broker_bridge.errors import BrokerAuthError
from tv_broker_bridge.models import BrokerOrder, OrderRequest, OrderStatus
from tv_broker_bridge.store import AlertState

D = Decimal


class TestRetryWithinOneRequest:
    def test_transient_connection_failure_is_retried_and_succeeds(
        self, h: Harness, logs: Any
    ) -> None:
        h.broker.script(Behavior(BehaviorKind.CONNECTION_FAILURE))
        response = h.post()

        assert response.json()["status"] == "accepted"
        assert h.broker.order_count() == 1
        assert h.position() == D(10)
        retries = logs.events("submit_retry")
        assert len(retries) == 1
        assert retries[0]["attempt"] == 1
        assert h.sleeps == [0.2]  # one backoff before the second attempt

    def test_backoff_grows_between_attempts(self, h: Harness) -> None:
        h.broker.script(
            Behavior(BehaviorKind.CONNECTION_FAILURE), Behavior(BehaviorKind.CONNECTION_FAILURE)
        )
        assert h.post().json()["status"] == "accepted"
        assert h.sleeps == [0.2, 0.4]

    def test_lost_response_is_retried_without_creating_a_second_order(
        self, h: Harness, logs: Any
    ) -> None:
        # The broker accepts and fills the order, but the answer never reaches us. The retry
        # reuses the same client_order_id, so the broker hands back the order it already has.
        h.broker.script(Behavior(BehaviorKind.LOST_RESPONSE))
        response = h.post()

        assert response.json()["status"] == "accepted"
        assert h.broker.order_count() == 1
        assert h.position() == D(10)  # booked once
        assert len(logs.events("submit_retry")) == 1
        assert len(logs.events("filled")) == 1


class TestPersistentOutage:
    def test_outage_parks_the_order_as_unknown_and_tells_the_sender_to_retry(
        self, h: Harness, logs: Any
    ) -> None:
        h.broker.set_online(False)
        response = h.post()

        assert response.status_code == 503
        assert response.json() == {
            "status": "broker_unavailable",
            "alert_id": "alert-0001",
            "retryable": True,
        }
        order = h.order_for("alert-0001")
        assert order.status is OrderStatus.SUBMIT_UNKNOWN
        assert h.rt.store.get_alert("alert-0001").state is AlertState.SUBMIT_UNKNOWN  # type: ignore[union-attr]
        assert h.position() == D(0)
        assert [e["event"] for e in logs.records if e["event"].startswith("submit_")] == [
            "submit_retry",
            "submit_retry",
            "submit_gave_up",
            "submit_unknown",
        ]

    def test_same_alert_id_completes_delivery_once_the_broker_is_back(
        self, h: Harness, logs: Any
    ) -> None:
        h.broker.set_online(False)
        assert h.post().status_code == 503
        h.broker.set_online(True)  # reconnect

        retry = h.post()  # the sender re-delivers the same alert_id
        assert retry.status_code == 200
        assert retry.json()["status"] == "accepted"
        assert h.broker.order_count() == 1
        assert h.position() == D(10)
        assert h.order_for("alert-0001").status is OrderStatus.FILLED
        assert h.rt.store.get_alert("alert-0001").state is AlertState.ACCEPTED  # type: ignore[union-attr]
        assert h.post().json()["status"] == "duplicate"  # and now it is final
        assert h.broker.order_count() == 1

    def test_an_unknown_order_still_counts_as_exposure(self, make_harness: HarnessFactory) -> None:
        h = make_harness(risk=default_risk(max_position_per_symbol=15))
        h.broker.set_online(False)
        assert h.post(alert_id="u1", qty=10).status_code == 503
        h.broker.set_online(True)
        # u1 may have reached the broker, so it is assumed live: 10 + 10 > 15.
        assert h.post(alert_id="u2", qty=10).json()["status"] == "rejected_by_risk"

    def test_unknown_order_is_resolved_by_reconciliation_when_the_broker_has_it(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h = make_harness(env={"SUBMIT_ATTEMPTS": "1"})
        h.broker.script(Behavior(BehaviorKind.LOST_RESPONSE))
        assert h.post().status_code == 503  # response lost, no retries left
        assert h.position() == D(0)  # we do not know about the fill yet

        report = h.reconcile()

        assert [m.kind for m in report.mismatches] == ["lost_response"]
        assert h.alerts.kinds == ["lost_response"]
        assert h.order_for("alert-0001").status is OrderStatus.FILLED
        assert h.position() == D(10)  # picked up from the broker, exactly once
        assert h.rt.store.get_alert("alert-0001").state is AlertState.ACCEPTED  # type: ignore[union-attr]
        assert h.post().json()["status"] == "duplicate"
        assert h.broker.order_count() == 1
        assert h.reconcile().clean

    def test_unconfirmed_order_is_failed_after_the_grace_period_and_never_auto_resent(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h = make_harness(env={"SUBMIT_ATTEMPTS": "1"})
        h.broker.script(Behavior(BehaviorKind.CONNECTION_FAILURE))
        assert h.post().status_code == 503

        h.clock.advance(60)
        assert h.reconcile().clean  # still inside the 120s window: the sender may retry
        assert h.order_for("alert-0001").status is OrderStatus.SUBMIT_UNKNOWN

        h.clock.advance(61)
        report = h.reconcile()

        assert [m.kind for m in report.mismatches] == ["unsent"]
        assert h.alerts.kinds == ["unsent"]
        assert h.order_for("alert-0001").status is OrderStatus.FAILED
        assert h.rt.store.get_alert("alert-0001").state is AlertState.FAILED  # type: ignore[union-attr]
        assert h.broker.order_count() == 0  # the bridge did not quietly send it late
        assert h.post().json()["status"] == "duplicate"  # a new alert_id is needed now
        assert h.broker.order_count() == 0
        assert h.position() == D(0)

    def test_a_failed_order_no_longer_counts_as_exposure(
        self, make_harness: HarnessFactory
    ) -> None:
        h = make_harness(
            risk=default_risk(max_position_per_symbol=15), env={"SUBMIT_ATTEMPTS": "1"}
        )
        h.broker.set_online(False)
        h.post(alert_id="u1", qty=10)
        h.broker.set_online(True)
        h.clock.advance(200)
        h.reconcile()
        assert h.post(alert_id="u2", qty=10).json()["status"] == "accepted"


class _AuthFailBroker(SimBroker):
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        raise BrokerAuthError("bad credentials")


class _CrashingBroker(SimBroker):
    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        raise RuntimeError("unexpected bug in the adapter")


class TestOtherFailureModes:
    def test_auth_failure_is_not_retried_and_fails_the_order(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h = make_harness(broker=_AuthFailBroker())
        response = h.post()
        assert response.status_code == 502
        assert response.json()["retryable"] is False
        assert h.order_for("alert-0001").status is OrderStatus.FAILED
        assert logs.events("broker_auth_error")[0]["level"] == "CRITICAL"
        assert logs.events("submit_retry") == []
        assert h.sleeps == []

    def test_unexpected_error_leaves_a_claim_that_reconciliation_cleans_up(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h = make_harness(broker=_CrashingBroker())
        response = h.post()
        assert response.status_code == 500
        assert response.json() == {"status": "error"}  # no stack trace leaks to the caller
        assert logs.events("internal_error")[0]["error_type"] == "RuntimeError"
        assert h.order_for("alert-0001").status is OrderStatus.PENDING_SUBMIT

        # While it could still be in flight (< 30s) reconciliation leaves it alone...
        h.clock.advance(10)
        assert h.reconcile().clean
        # ...and once it is clearly orphaned and past the grace window, it is closed out.
        h.clock.advance(200)
        report = h.reconcile()
        assert [m.kind for m in report.mismatches] == ["unsent"]
        assert h.order_for("alert-0001").status is OrderStatus.FAILED


class _AsyncRejectBroker(SimBroker):
    """Accepts the HTTP call but reports the order as rejected in the response body."""

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id="rej-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            status=OrderStatus.REJECTED,
        )


class TestRejectReportedInTheResponse:
    def test_a_rejected_status_is_treated_like_a_reject(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h = make_harness(broker=_AsyncRejectBroker())
        response = h.post()
        assert response.json()["status"] == "rejected_by_broker"
        assert h.order_for("alert-0001").status is OrderStatus.REJECTED
        assert h.position() == D(0)
        assert logs.events("broker_rejected")
        assert "sent" not in logs.names()
