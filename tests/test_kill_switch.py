"""Kill switch: stops accepting alerts and cancels all working orders."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import (
    ADMIN_SECRET,
    WEBHOOK_SECRET,
    FakeMonotonic,
    Harness,
    HarnessFactory,
)
from tests.test_alpaca_broker import KEY_ID, SECRET_KEY, Recorder, make_broker
from tv_broker_bridge import cli as cli_module
from tv_broker_bridge.broker.sim import SimBroker
from tv_broker_bridge.cli import main
from tv_broker_bridge.clock import utc_now
from tv_broker_bridge.db import Database
from tv_broker_bridge.errors import BrokerConnectionError
from tv_broker_bridge.kill_switch import KillSwitch
from tv_broker_bridge.models import BrokerOrder, OrderRequest, OrderStatus
from tv_broker_bridge.store import Store

D = Decimal


def place_working_orders(h: Harness) -> None:
    """One filled order and two resting limit orders."""
    h.post(alert_id="filled-1", qty=10)
    h.post(alert_id="rest-1", qty=5, order_type="limit", limit_price=50)
    h.post(alert_id="rest-2", qty=7, order_type="limit", limit_price=50)


class TestHttpKill:
    def test_requires_the_admin_secret(self, h: Harness, logs: Any) -> None:
        assert h.client.post("/admin/kill").status_code == 401
        assert (
            h.client.post(
                "/admin/kill", headers={"X-Admin-Secret": "wrong-wrong-wrong-wrong"}
            ).status_code
            == 401
        )
        assert not h.rt.kill.is_engaged()
        events = logs.events("auth_failed")
        assert [e["endpoint"] for e in events] == ["admin_kill", "admin_kill"]
        assert [e["reason"] for e in events] == ["missing_secret", "bad_secret"]

    def test_the_webhook_secret_does_not_unlock_the_kill_endpoint(self, h: Harness) -> None:
        response = h.client.post("/admin/kill", headers={"X-Admin-Secret": WEBHOOK_SECRET})
        assert response.status_code == 401
        assert not h.rt.kill.is_engaged()

    def test_the_admin_secret_does_not_work_as_a_webhook_secret(self, h: Harness) -> None:
        assert h.post(secret=ADMIN_SECRET).status_code == 401

    def test_kill_cancels_working_orders_and_keeps_filled_ones(self, h: Harness, logs: Any) -> None:
        place_working_orders(h)

        response = h.kill()

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "halted"
        assert body["cancel_complete"] is True
        assert len(body["canceled_order_ids"]) == 2
        # Broker side: both resting orders canceled, the filled one untouched.
        statuses = {o.client_order_id: o.status for o in h.broker.list_orders()}
        assert sorted(s.value for s in statuses.values()) == ["canceled", "canceled", "filled"]
        # Local side was synced as part of the kill, with no spurious mismatch alerts.
        assert h.order_for("rest-1").status is OrderStatus.CANCELED
        assert h.order_for("rest-2").status is OrderStatus.CANCELED
        assert h.order_for("filled-1").status is OrderStatus.FILLED
        assert h.position() == D(10)
        assert h.alerts.sent == []
        assert logs.events("kill_switch_engaged")[0]["level"] == "CRITICAL"
        assert logs.events("kill_switch_orders_canceled")[0]["count"] == 2

    def test_alerts_are_refused_after_a_kill(self, h: Harness, logs: Any) -> None:
        h.kill()
        response = h.post(alert_id="after-kill")
        assert response.status_code == 503
        assert response.json() == {
            "status": "halted",
            "alert_id": "after-kill",
            "reason": "kill_switch_engaged",
        }
        assert h.broker.order_count() == 0
        assert h.rt.store.get_alert("after-kill") is None  # not consumed: replayable after resume
        assert logs.events("rejected_halted")[0]["alert_id"] == "after-kill"

    def test_authentication_is_still_enforced_while_halted(self, h: Harness) -> None:
        h.kill()
        assert h.post(secret=None).status_code == 401

    def test_kill_is_idempotent(self, h: Harness) -> None:
        place_working_orders(h)
        first = h.kill().json()
        engaged_at = h.rt.kill.state().engaged_at
        h.clock.advance(60)
        second = h.kill()
        assert first["cancel_complete"]
        assert second.status_code == 200
        assert h.rt.kill.state().engaged_at == engaged_at  # original timestamp kept

    def test_optional_reason_is_recorded(self, h: Harness) -> None:
        h.client.post(
            "/admin/kill",
            json={"reason": "desk is closing"},
            headers={"X-Admin-Secret": ADMIN_SECRET},
        )
        assert h.rt.kill.state().reason == "desk is closing"

    def test_a_garbage_body_cannot_block_a_kill(self, h: Harness) -> None:
        response = h.client.post(
            "/admin/kill", content=b"{{{{", headers={"X-Admin-Secret": ADMIN_SECRET}
        )
        assert response.status_code == 200
        assert h.rt.kill.is_engaged()

    def test_health_reports_halted(self, h: Harness) -> None:
        h.kill()
        body = h.client.get("/health").json()
        assert body["status"] == "halted"
        assert body["kill_switch"]["engaged"] is True


class TestPersistence:
    def test_kill_survives_a_restart(self, make_harness: HarnessFactory) -> None:
        first = make_harness()
        first.kill()
        restarted = make_harness(broker=first.broker)  # new app, same database file
        assert restarted.post(alert_id="x").status_code == 503

    def test_resume_reopens_the_gate(self, h: Harness) -> None:
        h.kill()
        assert h.rt.kill.release("test") is True
        assert h.post().json()["status"] == "accepted"
        assert h.rt.kill.release("test") is False  # already released


class TestBrokerUnreachableDuringKill:
    def test_alerts_stop_immediately_and_cancel_is_retried_until_it_works(
        self, h: Harness, logs: Any
    ) -> None:
        place_working_orders(h)
        h.broker.set_online(False)

        response = h.kill()

        assert response.status_code == 202  # engaged, but cancel is still pending
        assert response.json()["cancel_complete"] is False
        assert h.post(alert_id="late").status_code == 503  # halted regardless
        state = h.rt.kill.state()
        assert state.engaged
        assert state.cancel_pending
        assert state.last_cancel_error
        assert logs.events("kill_switch_cancel_failed")

        h.rt.supervise_once()  # still offline: stays pending, heartbeat still written
        assert h.rt.kill.state().cancel_pending

        h.broker.set_online(True)
        h.rt.supervise_once()  # the supervisor loop finishes the job

        assert h.rt.kill.state().cancel_pending is False
        assert h.order_for("rest-1").status is OrderStatus.CANCELED
        assert h.order_for("rest-2").status is OrderStatus.CANCELED


class TestCli:
    def env(self, h: Harness) -> dict[str, str]:
        return {
            "DB_PATH": h.settings.db_path,
            "HEARTBEAT_PATH": h.settings.heartbeat_path,
            "BROKER": "sim",
        }

    def test_cli_kill_stops_a_running_server_and_the_server_cancels_its_orders(
        self, h: Harness, capsys: pytest.CaptureFixture[str]
    ) -> None:
        place_working_orders(h)

        code = main(["kill", "--reason", "from the terminal"], env=self.env(h))

        assert code == 0
        out = capsys.readouterr().out
        assert '"status": "halted"' in out
        assert "SimBroker lives inside the server process" in out
        # The server (a different "process" here) obeys the shared flag straight away...
        assert h.post(alert_id="after-cli-kill").status_code == 503
        assert h.rt.kill.state().reason == "from the terminal"
        # ...and cancels its in-process SimBroker orders on its next supervisor tick.
        assert h.rt.kill.state().cancel_pending
        h.rt.supervise_once()
        assert h.order_for("rest-1").status is OrderStatus.CANCELED
        assert h.rt.kill.state().cancel_pending is False

    def test_cli_resume(self, h: Harness, capsys: pytest.CaptureFixture[str]) -> None:
        h.kill()
        assert main(["resume"], env=self.env(h)) == 0
        assert '"status": "resumed"' in capsys.readouterr().out
        assert h.post().json()["status"] == "accepted"
        assert main(["resume"], env=self.env(h)) == 0
        assert "was_not_halted" in capsys.readouterr().out

    def test_cli_status(self, h: Harness, capsys: pytest.CaptureFixture[str]) -> None:
        place_working_orders(h)
        assert main(["status"], env=self.env(h)) == 0
        out = capsys.readouterr().out
        assert '"AAPL": "10"' in out
        assert out.count('"status": "accepted"') == 2  # the two resting orders


class _KillDuringSubmit(SimBroker):
    """Throws the kill switch after the broker has accepted an order, before the reply."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.on_submit: Any = None
        self.fail_cancel = False

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        order = super().submit_order(request)
        if self.on_submit is not None:
            self.on_submit()
        return order

    def cancel_order(self, broker_order_id: str) -> None:
        if self.fail_cancel:
            raise BrokerConnectionError("cancel timed out")
        super().cancel_order(broker_order_id)


class TestKillRacingAnInFlightOrder:
    def build(self, make_harness: HarnessFactory) -> tuple[Harness, _KillDuringSubmit]:
        broker = _KillDuringSubmit(clock=FakeMonotonic(), sleep=lambda _s: None)
        h = make_harness(broker=broker)
        broker.on_submit = lambda: h.rt.kill.engage("race", "test", cancel=False)
        return h, broker

    def test_an_order_that_slips_past_the_gate_is_cancelled_straight_away(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h, broker = self.build(make_harness)
        h.post(order_type="limit", limit_price=90)  # rests at the broker
        assert h.order_for("alert-0001").status is OrderStatus.ACCEPTED
        # the kill had already started cancel-all before this order existed; the processor
        # notices the engaged switch after submit and cancels the straggler itself.
        assert [o.status for o in broker.list_orders()] == [OrderStatus.CANCELED]
        assert logs.events("kill_switch_late_cancel")

    def test_if_that_late_cancel_fails_the_supervisor_is_asked_to_retry(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h, broker = self.build(make_harness)
        broker.fail_cancel = True
        h.post(order_type="limit", limit_price=90)
        assert logs.events("kill_switch_late_cancel_failed")
        assert h.rt.kill.state().cancel_pending
        broker.fail_cancel = False
        h.rt.supervise_once()
        assert [o.status for o in broker.list_orders()] == [OrderStatus.CANCELED]


class TestKillSwitchUnit:
    def test_without_a_broker_it_still_halts_and_reports_the_cancel_as_incomplete(
        self, h: Harness
    ) -> None:
        kill = KillSwitch(h.rt.store, None, h.clock)
        outcome = kill.engage("no broker handle", "test")
        assert outcome.engaged
        assert not outcome.cancel_complete
        assert kill.is_engaged()
        assert kill.cancel_working_orders().error == "no broker available"

    def test_a_failing_post_cancel_sync_does_not_undo_the_cancel(
        self, h: Harness, logs: Any
    ) -> None:
        def broken_sync() -> None:
            raise RuntimeError("sync failed")

        kill = KillSwitch(h.rt.store, h.broker, h.clock, after_cancel=broken_sync)
        outcome = kill.engage("x", "test")
        assert outcome.cancel_complete
        assert logs.events("kill_switch_sync_failed")
        assert kill.state().cancel_pending is False

    def test_retry_is_a_no_op_when_there_is_nothing_pending(self, h: Harness) -> None:
        assert h.rt.kill.retry_pending_cancel() is None  # not engaged
        h.kill()
        assert h.rt.kill.retry_pending_cancel() is None  # engaged, nothing pending


class TestCliAgainstASharedBroker:
    """With Alpaca, the CLI can cancel orders itself, even if the server is down."""

    def run_kill(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        handler: Any,
    ) -> tuple[int, Recorder]:
        recorder = Recorder(handler)
        monkeypatch.setattr(cli_module, "create_broker", lambda settings: make_broker(recorder))
        env = {
            "DB_PATH": str(tmp_path / "bridge.db"),
            "BROKER": "alpaca_paper",
            "ALPACA_API_KEY_ID": KEY_ID,
            "ALPACA_API_SECRET_KEY": SECRET_KEY,
        }
        return main(["kill", "--reason", "cli test"], env=env), recorder

    def test_cancels_directly_at_the_broker_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = self.run_kill(
            tmp_path,
            monkeypatch,
            lambda r: httpx.Response(207, json=[{"id": "o1", "status": 200}]),
        )
        assert code == 0
        assert '"cancel_complete": true' in capsys.readouterr().out
        assert [(r.method, r.url.path) for r in recorder.requests] == [("DELETE", "/v2/orders")]
        assert {r.url.host for r in recorder.requests} == {"paper-api.alpaca.markets"}
        db = Database(tmp_path / "bridge.db")
        assert KillSwitch(Store(db), None, utc_now).state().cancel_pending is False
        db.close()

    def test_exit_code_signals_an_incomplete_cancel_and_the_flag_stays_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _ = self.run_kill(tmp_path, monkeypatch, lambda r: httpx.Response(503, text="down"))
        assert code == cli_module.EXIT_CANCEL_INCOMPLETE
        assert '"cancel_complete": false' in capsys.readouterr().out
        db = Database(tmp_path / "bridge.db")
        state = KillSwitch(Store(db), None, utc_now).state()
        assert state.engaged  # alerts are stopped even though the cancel failed
        assert state.cancel_pending  # and the server will keep retrying
        db.close()
