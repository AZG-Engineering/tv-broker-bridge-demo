"""Each risk rule, as a pure function and through the webhook."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import DEFAULT_NOW, FakeClock, Harness, HarnessFactory, default_risk, make_alert
from tv_broker_bridge.broker.sim import Behavior, BehaviorKind
from tv_broker_bridge.errors import ConfigError
from tv_broker_bridge.risk import (
    DAILY_ORDER_LIMIT,
    OUTSIDE_TRADING_HOURS,
    POSITION_LIMIT,
    QTY_EXCEEDS_MAX,
    SYMBOL_NOT_ALLOWED,
    RiskConfig,
    RiskContext,
    TradingHours,
    evaluate,
    load_risk_config,
    local_day_bounds,
)
from tv_broker_bridge.schemas import validate_alert

NY = "America/New_York"


def decide(
    config: RiskConfig | None = None,
    *,
    position: int | str = 0,
    orders_today: int = 0,
    now: datetime = DEFAULT_NOW,
    **alert_overrides: Any,
) -> list[str]:
    ctx = RiskContext(now=now, effective_position=Decimal(position), orders_today=orders_today)
    alert = validate_alert(make_alert(**alert_overrides))
    return evaluate(config or default_risk(), alert, ctx).codes


def ny_hours(**overrides: Any) -> TradingHours:
    values: dict[str, Any] = {
        "enabled": True,
        "timezone": NY,
        "start": "09:30",
        "end": "16:00",
        "days": ["mon", "tue", "wed", "thu", "fri"],
    }
    values.update(overrides)
    return TradingHours(**values)


class TestMaxQtyPerOrder:
    def test_at_the_limit_is_allowed(self) -> None:
        assert decide(qty=100) == []

    def test_over_the_limit_is_rejected(self) -> None:
        assert decide(qty=101) == [QTY_EXCEEDS_MAX]

    def test_applies_to_sells_too(self) -> None:
        assert QTY_EXCEEDS_MAX in decide(side="sell", qty=101)


class TestMaxPositionPerSymbol:
    def test_reaching_the_limit_exactly_is_allowed(self) -> None:
        assert decide(position=240, qty=10) == []

    def test_exceeding_the_limit_is_rejected(self) -> None:
        assert decide(position=240, qty=11) == [POSITION_LIMIT]

    def test_short_side_is_limited_symmetrically(self) -> None:
        assert decide(position=-240, side="sell", qty=10) == []
        assert decide(position=-240, side="sell", qty=11) == [POSITION_LIMIT]

    def test_working_orders_count_toward_the_limit(self) -> None:
        # effective_position already includes signed working orders: 200 held + 40 working.
        assert decide(position=240, qty=11) == [POSITION_LIMIT]

    def test_flipping_through_zero_is_judged_on_the_resulting_position(self) -> None:
        assert decide(position=10, side="sell", qty=100) == []  # -> -90
        cfg = default_risk(max_position_per_symbol=50, max_qty_per_order=500)
        assert decide(cfg, position=10, side="sell", qty=100) == [POSITION_LIMIT]  # -> -90

    def test_orders_that_reduce_exposure_are_allowed_even_when_over_the_limit(self) -> None:
        # e.g. a manual trade pushed the position to 300 (limit 250): working it down must work.
        assert decide(position=300, side="sell", qty=10) == []
        assert decide(position=300, side="buy", qty=1) == [POSITION_LIMIT]
        assert decide(position=-300, side="buy", qty=10) == []

    def test_other_symbols_are_unaffected(self, h: Harness) -> None:
        # Handled by the store: positions are tracked per symbol.
        h.broker.inject_position("MSFT", Decimal(500))
        assert h.post(symbol="AAPL", qty=100).json()["status"] == "accepted"


class TestMaxOrdersPerDay:
    def test_below_the_limit_is_allowed(self) -> None:
        assert decide(orders_today=49) == []

    def test_at_the_limit_is_rejected(self) -> None:
        assert decide(orders_today=50) == [DAILY_ORDER_LIMIT]


class TestAllowedSymbols:
    def test_listed_symbol_is_allowed(self) -> None:
        assert decide(symbol="MSFT") == []

    def test_unlisted_symbol_is_rejected(self) -> None:
        assert decide(symbol="TSLA") == [SYMBOL_NOT_ALLOWED]

    def test_config_symbols_are_normalised(self) -> None:
        cfg = default_risk(allowed_symbols=[" aapl "])
        assert cfg.allowed_symbols == ["AAPL"]
        assert decide(cfg, symbol="AAPL") == []


class TestTradingHours:
    def cfg(self, **overrides: Any) -> RiskConfig:
        return default_risk(trading_hours=ny_hours(**overrides))

    @pytest.mark.parametrize(
        ("utc", "allowed"),
        [
            # 2026-03-04 is a Wednesday; New York is on EST (UTC-5) until 8 March.
            (datetime(2026, 3, 4, 14, 29, tzinfo=UTC), False),  # 09:29 local
            (datetime(2026, 3, 4, 14, 30, tzinfo=UTC), True),  # 09:30 local: start is inclusive
            (datetime(2026, 3, 4, 20, 59, tzinfo=UTC), True),  # 15:59 local
            (datetime(2026, 3, 4, 21, 0, tzinfo=UTC), False),  # 16:00 local: end is exclusive
            # After DST begins (Mon 9 March) the same local window is an hour earlier in UTC.
            (datetime(2026, 3, 9, 13, 29, tzinfo=UTC), False),
            (datetime(2026, 3, 9, 13, 30, tzinfo=UTC), True),
            (datetime(2026, 3, 9, 19, 59, tzinfo=UTC), True),
            (datetime(2026, 3, 9, 20, 0, tzinfo=UTC), False),
            # Weekend, inside the clock window.
            (datetime(2026, 3, 7, 16, 0, tzinfo=UTC), False),  # Saturday
            (datetime(2026, 3, 8, 16, 0, tzinfo=UTC), False),  # Sunday
        ],
    )
    def test_window_boundaries_timezone_and_weekdays(self, utc: datetime, allowed: bool) -> None:
        codes = decide(self.cfg(), now=utc)
        assert (codes == []) is allowed
        if not allowed:
            assert codes == [OUTSIDE_TRADING_HOURS]

    def test_disabled_window_allows_any_time(self) -> None:
        cfg = self.cfg(enabled=False)
        assert decide(cfg, now=datetime(2026, 3, 7, 3, 0, tzinfo=UTC)) == []

    def test_all_day_window_includes_the_last_minute(self) -> None:
        cfg = default_risk()  # 00:00-24:00 UTC
        assert decide(cfg, now=datetime(2026, 3, 4, 23, 59, tzinfo=UTC)) == []

    def test_day_bounds_follow_the_local_calendar_across_dst(self) -> None:
        start, end = local_day_bounds(datetime(2026, 3, 8, 12, 0, tzinfo=UTC), ZoneInfo(NY))
        assert (end - start).total_seconds() == 23 * 3600  # spring-forward day is 23h long
        assert start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)


class TestViolationReporting:
    def test_all_violations_are_reported_together(self) -> None:
        cfg = default_risk(trading_hours=ny_hours())
        codes = decide(
            cfg,
            symbol="TSLA",
            qty=101,
            position=250,
            orders_today=50,
            now=datetime(2026, 3, 7, 3, 0, tzinfo=UTC),
        )
        assert set(codes) == {
            SYMBOL_NOT_ALLOWED,
            OUTSIDE_TRADING_HOURS,
            QTY_EXCEEDS_MAX,
            POSITION_LIMIT,
            DAILY_ORDER_LIMIT,
        }


class TestRejectionThroughTheWebhook:
    """Rejections are logged (never silent), recorded, and never reach the broker."""

    def assert_rejected(
        self, h: Harness, response: Any, logs: Any, code: str, *, orders_before: int = 0
    ) -> None:
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected_by_risk"
        assert code in [v["code"] for v in body["violations"]]
        events = logs.events("rejected_by_risk")
        assert len(events) == 1
        assert code in events[0]["codes"]
        assert events[0]["alert_id"] == body["alert_id"]
        assert events[0]["level"] == "WARNING"
        assert h.broker.order_count() == orders_before  # nothing new reached the broker
        assert h.order_row_count() == orders_before  # nor was reserved locally
        row = h.rt.store.get_alert(body["alert_id"])
        assert row is not None
        assert row.state.value == "rejected_risk"
        assert code in (row.reason or "")

    def test_max_qty(self, h: Harness, logs: Any) -> None:
        self.assert_rejected(h, h.post(qty=101), logs, QTY_EXCEEDS_MAX)

    def test_allowed_symbols(self, h: Harness, logs: Any) -> None:
        self.assert_rejected(h, h.post(symbol="TSLA"), logs, SYMBOL_NOT_ALLOWED)

    def test_trading_hours(self, make_harness: HarnessFactory, logs: Any) -> None:
        h = make_harness(
            risk=default_risk(trading_hours=ny_hours()),
            clock=FakeClock(datetime(2026, 3, 4, 3, 0, tzinfo=UTC)),
        )
        self.assert_rejected(h, h.post(), logs, OUTSIDE_TRADING_HOURS)
        # The same alert text under a new id is accepted once the window opens.
        h.clock.now = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
        assert h.post(alert_id="alert-0002").json()["status"] == "accepted"

    def test_max_position_uses_fills_accumulated_through_the_pipeline(
        self, make_harness: HarnessFactory, logs: Any
    ) -> None:
        h = make_harness(risk=default_risk(max_position_per_symbol=250))
        assert h.post(alert_id="p1", qty=100).json()["status"] == "accepted"
        assert h.post(alert_id="p2", qty=100).json()["status"] == "accepted"
        assert h.position() == Decimal(200)
        response = h.post(alert_id="p3", qty=100)  # would make 300
        assert response.json()["status"] == "rejected_by_risk"
        assert POSITION_LIMIT in [v["code"] for v in response.json()["violations"]]
        assert h.broker.order_count() == 2
        # A smaller order that stays within the limit is still fine.
        assert h.post(alert_id="p4", qty=50).json()["status"] == "accepted"
        assert h.position() == Decimal(250)

    def test_max_position_counts_orders_still_working(self, h: Harness) -> None:
        # Resting limit buys (price below the market) fill nothing yet, but they are exposure.
        assert h.post(alert_id="w1", qty=100, order_type="limit", limit_price=50).status_code == 200
        assert h.post(alert_id="w2", qty=100, order_type="limit", limit_price=50).status_code == 200
        assert h.position() == Decimal(0)
        response = h.post(alert_id="w3", qty=100, order_type="limit", limit_price=50)
        assert response.json()["status"] == "rejected_by_risk"
        assert POSITION_LIMIT in [v["code"] for v in response.json()["violations"]]

    def test_max_orders_per_day_and_rollover(self, make_harness: HarnessFactory, logs: Any) -> None:
        h = make_harness(risk=default_risk(max_orders_per_day=3, max_position_per_symbol=10_000))
        for i in range(3):
            assert h.post(alert_id=f"d{i}").json()["status"] == "accepted"
        self.assert_rejected(h, h.post(alert_id="d3"), logs, DAILY_ORDER_LIMIT, orders_before=3)
        # Rejected alerts do not consume quota; the next calendar day starts fresh.
        h.clock.advance(24 * 3600)
        assert h.post(alert_id="d4").json()["status"] == "accepted"

    def test_broker_rejected_orders_still_count_toward_the_daily_limit(
        self, make_harness: HarnessFactory
    ) -> None:
        h = make_harness(risk=default_risk(max_orders_per_day=2))
        h.broker.script(Behavior(BehaviorKind.REJECT), Behavior(BehaviorKind.REJECT))
        assert h.post(alert_id="b1").json()["status"] == "rejected_by_broker"
        assert h.post(alert_id="b2").json()["status"] == "rejected_by_broker"
        # A runaway sender cannot burn unlimited broker round-trips.
        assert h.post(alert_id="b3").json()["status"] == "rejected_by_risk"


class TestRiskConfigLoading:
    GOOD = """
max_qty_per_order: 10
max_position_per_symbol: 20
max_orders_per_day: 5
allowed_symbols: [spy]
trading_hours:
  enabled: true
  timezone: UTC
  start: "09:30"
  end: "16:00"
  days: [mon, tue]
"""

    def write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "risk.yaml"
        path.write_text(text)
        return path

    def test_valid_file_loads(self, tmp_path: Path) -> None:
        cfg = load_risk_config(self.write(tmp_path, self.GOOD))
        assert cfg.max_qty_per_order == 10
        assert cfg.allowed_symbols == ["SPY"]
        assert cfg.trading_hours.start_minutes == 570

    def test_the_shipped_example_is_valid(self) -> None:
        cfg = load_risk_config(Path(__file__).parent.parent / "config" / "risk.example.yaml")
        assert cfg.allowed_symbols
        assert cfg.trading_hours.end == "24:00"

    @pytest.mark.parametrize(
        ("find", "replace", "message"),
        [
            ("max_qty_per_order: 10\n", "", "max_qty_per_order"),  # missing key
            ("max_qty_per_order: 10", "max_qty_per_order: 0", "max_qty_per_order"),
            ("max_qty_per_order: 10", "max_qty_per_order: -3", "max_qty_per_order"),
            ("max_qty_per_order: 10", "max_qty_per_order: lots", "max_qty_per_order"),
            ("max_orders_per_day: 5\n", "max_orders_per_day: 5\nmax_losses: 1\n", "max_losses"),
            ("[spy]", "[]", "allowed_symbols"),
            ("[spy]", '["  "]', "allowed_symbols"),
            ("timezone: UTC", "timezone: Mars/Base", "timezone"),
            ('start: "09:30"', 'start: "9:30"', "start"),
            ('start: "09:30"', 'start: "25:00"', "start"),
            ('end: "16:00"', 'end: "09:00"', "start must be earlier"),
            ('end: "16:00"', 'end: "09:30"', "start must be earlier"),
            ("days: [mon, tue]", "days: [funday]", "days"),
            ("days: [mon, tue]", "days: []", "days"),
            ("  enabled: true\n", "", "enabled"),
        ],
    )
    def test_invalid_files_are_refused(
        self, tmp_path: Path, find: str, replace: str, message: str
    ) -> None:
        assert find in self.GOOD
        with pytest.raises(ConfigError, match=message):
            load_risk_config(self.write(tmp_path, self.GOOD.replace(find, replace)))

    def test_missing_file_explains_how_to_fix(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"risk\.example\.yaml"):
            load_risk_config(tmp_path / "nope.yaml")

    @pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string", ": : :"])
    def test_non_mapping_or_unparseable_yaml_is_refused(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(ConfigError):
            load_risk_config(self.write(tmp_path, text))
