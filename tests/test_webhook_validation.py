"""Malformed input is rejected, with a logged reason, and never reaches the broker."""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import WEBHOOK_SECRET, Harness, make_alert


def _only_event(h: Harness, logs: Any, name: str) -> dict[str, Any]:
    events = logs.events(name)
    assert len(events) == 1, logs.names()
    event: dict[str, Any] = events[0]
    return event


class TestAuthentication:
    def test_missing_secret_is_401_and_logged(self, h: Harness, logs: Any) -> None:
        response = h.post(secret=None)
        assert response.status_code == 401
        event = _only_event(h, logs, "auth_failed")
        assert event["reason"] == "missing_secret"
        assert h.broker.order_count() == 0

    def test_wrong_secret_is_401_and_logged(self, h: Harness, logs: Any) -> None:
        response = h.post(secret="not-the-secret-not-the-secret")
        assert response.status_code == 401
        assert _only_event(h, logs, "auth_failed")["reason"] == "bad_secret"
        assert h.broker.order_count() == 0

    def test_wrong_secret_does_not_reveal_validation_errors(self, h: Harness) -> None:
        # An unauthenticated caller must not be able to probe the schema.
        response = h.post(secret="not-the-secret-not-the-secret", qty="garbage")
        assert response.status_code == 401
        assert response.json() == {"status": "unauthorized"}

    def test_secret_accepted_in_header(self, h: Harness) -> None:
        response = h.post(in_header=True)
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

    def test_secret_accepted_in_body_field(self, h: Harness) -> None:
        response = h.post()
        assert response.status_code == 200

    def test_non_string_secret_is_rejected(self, h: Harness) -> None:
        body = make_alert()
        body["secret"] = 12345
        assert h.client.post("/webhook/tradingview", json=body).status_code == 401

    def test_unparseable_body_without_credentials_is_401_not_400(self, h: Harness) -> None:
        response = h.client.post("/webhook/tradingview", content=b"{nope")
        assert response.status_code == 401

    def test_secret_never_appears_in_logs(self, h: Harness, logs: Any) -> None:
        h.post()  # success path, secret in body
        h.post(alert_id="alert-0002", in_header=True)  # success path, secret in header
        h.post(secret="wrong-wrong-wrong-wrong-wrong")  # failure path
        h.post(qty="bad")  # validation-failure path
        assert WEBHOOK_SECRET not in logs.text()
        assert "wrong-wrong-wrong" not in logs.text()


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        ("overrides", "bad_field"),
        [
            ({"alert_id": ...}, "alert_id"),
            ({"symbol": ...}, "symbol"),
            ({"side": ...}, "side"),
            ({"qty": ...}, "qty"),
            ({"order_type": ...}, "order_type"),
            ({"alert_id": ""}, "alert_id"),
            ({"alert_id": "x" * 65}, "alert_id"),
            ({"alert_id": "has space"}, "alert_id"),
            ({"alert_id": 123}, "alert_id"),
            ({"symbol": "aapl"}, "symbol"),  # no silent upper-casing
            ({"symbol": "NASDAQ:AAPL"}, "symbol"),
            ({"symbol": "A" * 11}, "symbol"),
            ({"symbol": ""}, "symbol"),
            ({"side": "BUY"}, "side"),
            ({"side": "long"}, "side"),
            ({"qty": "10"}, "qty"),  # strings are not coerced
            ({"qty": 10.5}, "qty"),
            ({"qty": 10.0}, "qty"),
            ({"qty": 0}, "qty"),
            ({"qty": -5}, "qty"),
            ({"qty": True}, "qty"),
            ({"qty": None}, "qty"),
            ({"qty": 10**9}, "qty"),
            ({"order_type": "stop"}, "order_type"),
            ({"order_type": "MARKET"}, "order_type"),
        ],
    )
    def test_field_violations_are_rejected_with_reason(
        self, h: Harness, logs: Any, overrides: dict[str, Any], bad_field: str
    ) -> None:
        response = h.post(**overrides)
        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "schema_validation_failed"
        assert bad_field in {d["field"] for d in body["details"]}
        event = _only_event(h, logs, "rejected_malformed")
        assert event["reason"] == "schema_validation_failed"
        assert bad_field in {d["field"] for d in event["details"]}
        assert h.broker.order_count() == 0
        assert h.alert_row_count() == 0  # a malformed alert never claims an alert_id

    def test_unknown_field_is_rejected(self, h: Harness) -> None:
        response = h.post(strategy="momentum")
        assert response.status_code == 400
        assert response.json()["details"][0]["field"] == "strategy"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"order_type": "limit"},  # missing price
            {"order_type": "limit", "limit_price": None},
            {"order_type": "limit", "limit_price": 0},
            {"order_type": "limit", "limit_price": -1.5},
            {"order_type": "limit", "limit_price": "10.5"},
            {"order_type": "limit", "limit_price": True},
            {"order_type": "limit", "limit_price": 10**9},
            {"order_type": "market", "limit_price": 10.5},  # price on a market order
        ],
    )
    def test_limit_price_rules(self, h: Harness, overrides: dict[str, Any]) -> None:
        response = h.post(**overrides)
        assert response.status_code == 400, response.text
        assert h.broker.order_count() == 0

    def test_market_order_with_null_limit_price_is_fine(self, h: Harness) -> None:
        assert h.post(limit_price=None).status_code == 200

    def test_valid_limit_order_is_accepted(self, h: Harness) -> None:
        response = h.post(order_type="limit", limit_price=101.25)
        assert response.status_code == 200
        order = h.order_for("alert-0001")
        assert str(order.limit_price) == "101.25"

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            (b"{not json", "invalid_json"),
            (b"", "invalid_json"),
            (b"[1, 2, 3]", "not_a_json_object"),
            (b'"just a string"', "not_a_json_object"),
            (b"null", "not_a_json_object"),
            (b'{"qty": NaN}', "invalid_json"),
            (b'{"qty": Infinity}', "invalid_json"),
            (b'{"qty": 1, "qty": 2}', "invalid_json"),  # duplicate keys are ambiguous
            (b"\xff\xfe\x00", "invalid_encoding"),
        ],
    )
    def test_unparseable_bodies_from_authenticated_callers(
        self, h: Harness, logs: Any, raw: bytes, reason: str
    ) -> None:
        response = h.client.post(
            "/webhook/tradingview", content=raw, headers={"X-Webhook-Secret": WEBHOOK_SECRET}
        )
        assert response.status_code == 400
        assert _only_event(h, logs, "rejected_malformed")["reason"] == reason
        assert h.broker.order_count() == 0

    def test_oversized_body_is_rejected(self, h: Harness, logs: Any) -> None:
        response = h.client.post(
            "/webhook/tradingview",
            content=b"x" * (h.settings.max_body_bytes + 1),
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )
        assert response.status_code == 413
        assert _only_event(h, logs, "rejected_malformed")["reason"] == "body_too_large"

    def test_oversized_streamed_body_without_content_length_is_rejected(
        self, h: Harness, logs: Any
    ) -> None:
        def chunks() -> Any:
            for _ in range(10):
                yield b"x" * (h.settings.max_body_bytes // 4)  # chunked: no Content-Length

        response = h.client.post(
            "/webhook/tradingview", content=chunks(), headers={"X-Webhook-Secret": WEBHOOK_SECRET}
        )
        assert response.status_code == 413
        assert _only_event(h, logs, "rejected_malformed")["reason"] == "body_too_large"
        assert h.broker.order_count() == 0

    def test_rejection_logs_do_not_echo_offending_values(self, h: Harness, logs: Any) -> None:
        h.post(qty="super-secret-looking-garbage")
        assert "super-secret-looking-garbage" not in logs.text()
