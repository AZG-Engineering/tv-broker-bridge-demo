"""A repeated alert_id never places a second order."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any

from tests.conftest import Harness, HarnessFactory, default_risk, make_alert
from tv_broker_bridge.broker.sim import Behavior, BehaviorKind, SimBroker
from tv_broker_bridge.processor import client_order_id_for
from tv_broker_bridge.schemas import validate_alert
from tv_broker_bridge.store import AlertState


class TestDuplicateAlerts:
    def test_repeated_alert_places_exactly_one_order(self, h: Harness, logs: Any) -> None:
        first, second, third = h.post(), h.post(), h.post()

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"
        assert third.json()["status"] == "duplicate"
        assert (first.status_code, second.status_code, third.status_code) == (200, 200, 200)
        assert h.broker.order_count() == 1
        assert h.position() == Decimal(10)  # filled once, not three times
        assert len(logs.events("sent")) == 1
        assert len(logs.events("deduped")) == 2

    def test_dedupe_is_logged_with_the_original_outcome(self, h: Harness, logs: Any) -> None:
        h.post()
        h.post()
        event = logs.events("deduped")[0]
        assert event["alert_id"] == "alert-0001"
        assert event["original_state"] == "accepted"
        assert event["payload_matches"] is True

    def test_distinct_alert_ids_are_independent(self, h: Harness) -> None:
        h.post(alert_id="a-1")
        h.post(alert_id="a-2")
        assert h.broker.order_count() == 2
        assert h.position() == Decimal(20)

    def test_same_alert_id_with_different_payload_is_still_deduped_and_flagged(
        self, h: Harness, logs: Any
    ) -> None:
        h.post(qty=10)
        response = h.post(qty=90, side="sell")
        assert response.json()["status"] == "duplicate"
        assert h.broker.order_count() == 1
        assert h.position() == Decimal(10)
        event = logs.events("deduped")[0]
        assert event["payload_matches"] is False
        assert event["level"] == "WARNING"

    def test_resending_a_risk_rejected_alert_is_not_reevaluated(
        self, h: Harness, logs: Any
    ) -> None:
        first = h.post(qty=1000)
        second = h.post(qty=1000)
        assert first.json()["status"] == "rejected_by_risk"
        assert second.json() == {
            "status": "duplicate",
            "alert_id": "alert-0001",
            "original_state": "rejected_risk",
        }
        assert len(logs.events("rejected_by_risk")) == 1

    def test_resending_a_broker_rejected_alert_is_not_retried(self, h: Harness) -> None:
        h.broker.script(Behavior(BehaviorKind.REJECT, "no"))
        assert h.post().json()["status"] == "rejected_by_broker"
        assert h.post().json()["status"] == "duplicate"
        assert h.broker.order_count() == 0

    def test_malformed_alert_does_not_consume_the_alert_id(self, h: Harness) -> None:
        assert h.post(qty="oops").status_code == 400
        assert h.post().json()["status"] == "accepted"

    def test_unauthenticated_alert_does_not_consume_the_alert_id(self, h: Harness) -> None:
        assert h.post(secret="wrong-wrong-wrong-wrong-wrong").status_code == 401
        assert h.post().json()["status"] == "accepted"

    def test_duplicate_is_detected_after_a_restart(self, make_harness: HarnessFactory) -> None:
        broker_state_owner = make_harness()
        broker_state_owner.post()
        # "Restart": a brand-new app and runtime over the same database file and broker.
        restarted = make_harness(broker=broker_state_owner.broker)
        response = restarted.post()
        assert response.json()["status"] == "duplicate"
        assert restarted.broker.order_count() == 1

    def test_concurrent_duplicates_race_to_a_single_order(self, h: Harness) -> None:
        alert = validate_alert(make_alert(alert_id="race-1"))
        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(lambda _: h.rt.processor.process(alert), range(32)))
        statuses = sorted(o.status for o in outcomes)
        assert statuses.count("accepted") == 1
        assert statuses.count("duplicate") == 31
        assert h.broker.order_count() == 1
        assert h.position() == Decimal(10)

    def test_client_order_id_is_deterministic_per_alert(self, h: Harness) -> None:
        h.post(alert_id="stable-id")
        order = h.order_for("stable-id")
        assert order.client_order_id == client_order_id_for("stable-id")
        assert client_order_id_for("stable-id") != client_order_id_for("other-id")

    def test_broker_level_idempotency_backs_up_the_database(self, h: Harness) -> None:
        # Even if the local claim were lost (say the DB was wiped), the broker refuses to
        # create a second order for the same client_order_id.
        h.post()
        row = h.rt.db.query_one("SELECT client_order_id FROM orders")
        assert row is not None
        order = h.order_for("alert-0001")
        again = h.broker.submit_order(h.rt.processor._request_for(order))
        assert again.broker_order_id == order.broker_order_id
        assert h.broker.order_count() == 1

    def test_alert_state_is_recorded(self, h: Harness) -> None:
        h.post()
        row = h.rt.store.get_alert("alert-0001")
        assert row is not None
        assert row.state is AlertState.ACCEPTED
        assert isinstance(h.broker, SimBroker)
        assert default_risk().max_qty_per_order == 100
