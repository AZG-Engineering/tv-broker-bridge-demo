"""Structured JSON logging: one parseable object per decision, secrets redacted."""

from __future__ import annotations

import json
import logging
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import WEBHOOK_SECRET, Harness
from tv_broker_bridge.broker.sim import Behavior, BehaviorKind
from tv_broker_bridge.logging_utils import (
    LOGGER_NAME,
    JsonFormatter,
    configure_logging,
    log_event,
    redact,
)


class TestEveryDecisionIsLogged:
    def test_full_lifecycle_emits_every_required_event(self, h: Harness, logs: Any) -> None:
        h.post(alert_id="ok-1")  # received, sent, filled
        h.post(alert_id="ok-1")  # deduped
        h.post(alert_id="too-big", qty=1000)  # rejected_by_risk
        h.post(alert_id="bad", qty="x")  # rejected_malformed
        h.post(secret="wrong-wrong-wrong-wrong-wrong")  # auth_failed
        h.broker.script(Behavior(BehaviorKind.REJECT, "nope"))
        h.post(alert_id="rejected")  # broker_rejected
        h.reconcile()  # reconciled

        names = set(logs.names())
        assert {
            "received",
            "deduped",
            "rejected_by_risk",
            "sent",
            "filled",
            "reconciled",
            "rejected_malformed",
            "auth_failed",
            "broker_rejected",
        } <= names

    def test_every_line_is_a_json_object_with_the_core_keys(self, h: Harness, logs: Any) -> None:
        h.post()
        h.reconcile()
        assert logs.lines
        for line in logs.lines:
            record = json.loads(line)
            assert {"ts", "level", "event"} <= record.keys()
            assert record["ts"].endswith("+00:00")
            assert "\n" not in line

    def test_decisions_carry_the_alert_id_for_correlation(self, h: Harness, logs: Any) -> None:
        h.post(alert_id="trace-me")
        h.post(alert_id="trace-me")
        h.post(alert_id="trace-big", qty=999)
        for event in ("received", "sent", "filled", "deduped", "rejected_by_risk"):
            assert all("alert_id" in e for e in logs.events(event)), event
        assert [e["alert_id"] for e in logs.events("received")] == [
            "trace-me",
            "trace-me",
            "trace-big",
        ]
        assert [e["alert_id"] for e in logs.events("sent")] == ["trace-me"]
        assert [e["alert_id"] for e in logs.events("filled")] == ["trace-me"]
        assert [e["alert_id"] for e in logs.events("deduped")] == ["trace-me"]
        assert [e["alert_id"] for e in logs.events("rejected_by_risk")] == ["trace-big"]

    def test_rejections_are_never_silent(self, h: Harness, logs: Any) -> None:
        h.post(alert_id="r1", qty=1000)
        h.post(alert_id="r2", symbol="ZZZZ")
        h.post(alert_id="r3", qty=-1)
        h.post(alert_id="r4", secret=None)
        for event in ("rejected_by_risk", "rejected_malformed", "auth_failed"):
            assert logs.events(event), f"no {event} event was logged"
        assert all(e["level"] == "WARNING" for e in logs.events("rejected_by_risk"))


class TestRedaction:
    def test_credential_like_keys_are_redacted_recursively(self) -> None:
        data = {
            "alert_id": "a",
            "secret": "s3",
            "nested": {"api_key": "k", "Authorization": "Bearer x", "ok": 1},
            "items": [{"password": "p"}, {"token": "t"}, "plain"],
            "APCA-API-SECRET-KEY": "z",
            "webhook_secret": "w",
        }
        out = redact(data)
        assert out["alert_id"] == "a"
        assert out["nested"] == {
            "api_key": "***redacted***",
            "Authorization": "***redacted***",
            "ok": 1,
        }
        assert out["items"] == [
            {"password": "***redacted***"},
            {"token": "***redacted***"},
            "plain",
        ]
        assert (
            out["secret"] == out["APCA-API-SECRET-KEY"] == out["webhook_secret"] == "***redacted***"
        )

    def test_log_event_redacts_even_if_a_caller_passes_a_secret(self, logs: Any) -> None:
        log_event("oops", webhook_secret=WEBHOOK_SECRET, detail={"api_key": "abc"})
        assert WEBHOOK_SECRET not in logs.text()
        assert "abc" not in logs.text()
        assert logs.events("oops")[0]["webhook_secret"] == "***redacted***"


class TestFormatting:
    def test_decimals_and_other_types_serialise(self, logs: Any) -> None:
        log_event("types", qty=Decimal("10.50"), path=Path("/x"), none=None)
        record = logs.events("types")[0]
        assert record["qty"] == "10.50"
        assert record["path"] == "/x"
        assert record["none"] is None

    def test_reserved_field_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="reserved"):
            log_event("x", fields="clash")
        with pytest.raises(TypeError):  # `event` is the first argument: Python itself refuses
            log_event("x", event="clash")  # type: ignore[misc]

    def test_exceptions_are_included(self) -> None:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            record = logging.LogRecord("t", logging.ERROR, "f", 1, "msg", None, None)
            record.exc_info = sys.exc_info()
        text = JsonFormatter().format(record)
        assert "RuntimeError: boom" in json.loads(text)["exception"]

    def test_plain_log_records_without_fields_still_format(self) -> None:
        record = logging.LogRecord("t", logging.INFO, "f", 1, "hello %s", ("world",), None)
        assert json.loads(JsonFormatter().format(record))["event"] == "hello world"


class TestConfigureLogging:
    def test_writes_json_to_stdout_and_a_rotating_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        saved_handlers, saved_level, saved_propagate = (
            list(logger.handlers),
            logger.level,
            logger.propagate,
        )
        try:
            log_file = tmp_path / "logs" / "bridge.log"
            configure_logging("INFO", log_file)
            configure_logging("INFO", log_file)  # idempotent: no duplicated handlers
            log_event("hello", n=1)
            for handler in logger.handlers:
                handler.flush()
            out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
            file_lines = log_file.read_text().splitlines()
            assert len(out_lines) == 1
            assert len(file_lines) == 1
            assert json.loads(file_lines[0])["event"] == "hello"
            assert logger.propagate is False
        finally:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            for handler in saved_handlers:
                logger.addHandler(handler)
            logger.setLevel(saved_level)
            logger.propagate = saved_propagate

    def test_level_filters_events(self, capsys: pytest.CaptureFixture[str]) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        saved_handlers, saved_level, saved_propagate = (
            list(logger.handlers),
            logger.level,
            logger.propagate,
        )
        try:
            configure_logging("WARNING")
            log_event("quiet")
            log_event("loud", level=logging.ERROR)
            names = [json.loads(ln)["event"] for ln in capsys.readouterr().out.splitlines() if ln]
            assert names == ["loud"]
        finally:
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            for handler in saved_handlers:
                logger.addHandler(handler)
            logger.setLevel(saved_level)
            logger.propagate = saved_propagate
