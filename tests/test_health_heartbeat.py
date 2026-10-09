"""/health, the heartbeat file, and the background loops that keep both fresh."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    ADMIN_SECRET,
    WEBHOOK_SECRET,
    Harness,
    default_risk,
    make_settings,
)
from tv_broker_bridge.app import create_app
from tv_broker_bridge.broker.sim import SimBroker
from tv_broker_bridge.cli import main
from tv_broker_bridge.heartbeat import check_heartbeat, read_heartbeat, write_heartbeat
from tv_broker_bridge.runtime import PeriodicWorker

D = Decimal


class TestHealthEndpoint:
    def test_healthy_shape_and_no_secrets(self, h: Harness) -> None:
        response = h.client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["problems"] == []
        assert body["broker"] == "sim"
        assert body["kill_switch"]["engaged"] is False
        assert body["reconcile"]["consecutive_failures"] == 0
        assert WEBHOOK_SECRET not in response.text
        assert ADMIN_SECRET not in response.text

    def test_needs_no_credentials(self, h: Harness) -> None:
        assert h.client.get("/health", headers={}).status_code == 200

    def test_repeated_reconcile_failures_degrade_health_until_recovery(self, h: Harness) -> None:
        h.broker.set_online(False)
        h.reconcile()
        h.reconcile()
        assert h.client.get("/health").status_code == 200  # two failures: still tolerated
        h.reconcile()
        degraded = h.client.get("/health")
        assert degraded.status_code == 503
        assert degraded.json()["status"] == "degraded"
        assert "reconcile_failing" in degraded.json()["problems"]

        h.broker.set_online(True)
        h.reconcile()
        assert h.client.get("/health").json()["status"] == "ok"

    def test_a_mismatch_marks_health_degraded_until_the_next_clean_pass(self, h: Harness) -> None:
        h.broker.inject_position("AAPL", D(5))
        h.reconcile()
        body = h.client.get("/health").json()
        assert body["status"] == "degraded"
        assert body["problems"] == ["reconcile_mismatch"]
        h.reconcile()
        assert h.client.get("/health").json()["status"] == "ok"

    def test_unknown_routes_and_docs_are_not_exposed(self, h: Harness) -> None:
        for path in ("/docs", "/redoc", "/openapi.json", "/admin/resume"):
            assert h.client.get(path).status_code == 404
        assert h.client.post("/admin/resume").status_code == 404  # resume is CLI-only
        assert h.client.get("/webhook/tradingview").status_code == 405


class TestHeartbeatFile:
    def test_supervisor_tick_writes_a_fresh_heartbeat(self, h: Harness) -> None:
        h.rt.supervise_once()
        data = read_heartbeat(h.settings.heartbeat_path)
        assert data is not None
        assert data["status"] == "ok"
        assert data["broker"] == "sim"
        assert data["kill_switch"] is False
        assert data["epoch"] == h.clock().timestamp()
        assert data["ts"].startswith("2026-03-04T15:00:00")

    def test_heartbeat_reflects_kill_switch_and_degradation(self, h: Harness) -> None:
        h.kill()
        h.rt.supervise_once()
        data = read_heartbeat(h.settings.heartbeat_path)
        assert data is not None
        assert data["status"] == "halted"
        assert data["kill_switch"] is True

    def test_writes_are_atomic_and_leave_no_temp_files(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "hb.json"
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(5):
            write_heartbeat(target, now + timedelta(seconds=i), status="ok")
        assert json.loads(target.read_text())["ts"] == "2026-01-01T00:00:04+00:00"
        assert [p.name for p in target.parent.iterdir()] == ["hb.json"]

    def test_failed_write_does_not_corrupt_the_previous_heartbeat(self, tmp_path: Path) -> None:
        target = tmp_path / "hb.json"
        now = datetime(2026, 1, 1, tzinfo=UTC)
        write_heartbeat(target, now, status="ok")
        before = target.read_text()
        with pytest.raises(TypeError):  # a tuple key cannot be serialised, even with default=str
            write_heartbeat(target, now + timedelta(seconds=5), status={(1, 2): "x"})
        assert target.read_text() == before  # the old heartbeat is intact
        assert [p.name for p in tmp_path.iterdir()] == ["hb.json"]  # no temp-file litter


class TestHeartbeatWatcher:
    NOW = datetime(2026, 3, 4, 15, 0, 0, tzinfo=UTC)

    def test_fresh_heartbeat_is_healthy(self, tmp_path: Path) -> None:
        write_heartbeat(tmp_path / "hb.json", self.NOW, status="ok")
        ok, message = check_heartbeat(tmp_path / "hb.json", 30, self.NOW + timedelta(seconds=10))
        assert ok
        assert "10.0s old" in message

    def test_stale_heartbeat_is_unhealthy(self, tmp_path: Path) -> None:
        write_heartbeat(tmp_path / "hb.json", self.NOW, status="ok")
        ok, message = check_heartbeat(tmp_path / "hb.json", 30, self.NOW + timedelta(seconds=31))
        assert not ok
        assert "stale" in message

    def test_missing_or_corrupt_file_is_unhealthy(self, tmp_path: Path) -> None:
        assert check_heartbeat(tmp_path / "nope.json", 30, self.NOW)[0] is False
        (tmp_path / "bad.json").write_text("{not json")
        assert check_heartbeat(tmp_path / "bad.json", 30, self.NOW)[0] is False
        (tmp_path / "noepoch.json").write_text('{"status": "ok"}')
        assert check_heartbeat(tmp_path / "noepoch.json", 30, self.NOW)[0] is False
        (tmp_path / "list.json").write_text("[1, 2]")
        assert check_heartbeat(tmp_path / "list.json", 30, self.NOW)[0] is False

    def test_cli_exit_codes_for_use_by_cron_or_a_container_probe(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        path = tmp_path / "hb.json"
        env = {"HEARTBEAT_PATH": str(path)}
        assert main(["check-heartbeat"], env=env) == 1  # nothing written yet
        write_heartbeat(path, datetime.now(UTC), status="ok")
        assert main(["check-heartbeat", "--max-age", "30"], env=env) == 0
        write_heartbeat(path, datetime.now(UTC) - timedelta(minutes=5), status="ok")
        assert main(["check-heartbeat", "--max-age", "30"], env=env) == 1
        assert "stale" in capsys.readouterr().out
        assert main(["check-heartbeat", "--path", str(tmp_path / "other.json")], env=env) == 1


class TestBackgroundLoops:
    """Real threads and a real clock, with short intervals."""

    def test_lifespan_starts_heartbeat_and_reconcile_loops_and_stops_them(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path, HEARTBEAT_INTERVAL_S="0.1", RECONCILE_INTERVAL_S="0.1")
        broker = SimBroker()
        app = create_app(settings, broker=broker, risk=default_risk())
        rt = app.state.runtime

        with TestClient(app) as client:
            assert {w.name for w in rt.workers} == {"heartbeat", "reconcile"}
            first = self.wait_for(lambda: read_heartbeat(settings.heartbeat_path))
            self.wait_for(lambda: rt.status.reconcile().passes >= 2)
            later = self.wait_for(
                lambda: (
                    d
                    if (d := read_heartbeat(settings.heartbeat_path))
                    and d["epoch"] > first["epoch"]
                    else None
                )
            )
            assert later["epoch"] > first["epoch"]  # the file keeps moving
            assert client.get("/health").json()["status"] == "ok"
            workers = list(rt.workers)

        assert rt.workers == []
        assert not any(w.is_alive() for w in workers)

    def test_the_loop_survives_an_exception_in_its_task(self, logs: Any) -> None:
        calls: list[int] = []
        done = threading.Event()

        def task() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first run explodes")
            done.set()

        worker = PeriodicWorker("flaky", 0.01, task)
        worker.start()
        try:
            assert done.wait(timeout=5), "loop did not continue after an exception"
        finally:
            worker.stop()
        error = logs.events("loop_error")[0]
        assert error["loop"] == "flaky"
        assert error["error"] == "first run explodes"
        assert not worker.is_alive()

    @staticmethod
    def wait_for(probe: Any, timeout: float = 5.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = probe()
            if value:
                return value
            time.sleep(0.02)
        raise AssertionError("condition not met in time")
