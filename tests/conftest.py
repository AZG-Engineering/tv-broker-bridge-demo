"""Shared fixtures: a fully wired app around SimBroker, a fake clock, and log capture."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tv_broker_bridge.app import create_app
from tv_broker_bridge.broker.sim import SimBroker, SimConfig
from tv_broker_bridge.config import Settings
from tv_broker_bridge.logging_utils import LOGGER_NAME, JsonFormatter
from tv_broker_bridge.risk import RiskConfig, TradingHours
from tv_broker_bridge.runtime import Runtime

WEBHOOK_SECRET = "test-webhook-secret-0123456789"
ADMIN_SECRET = "test-admin-secret-9876543210"
# Wednesday, mid-day UTC: inside any sensible trading window.
DEFAULT_NOW = datetime(2026, 3, 4, 15, 0, 0, tzinfo=UTC)


class FakeClock:
    """Wall clock the tests control."""

    def __init__(self, start: datetime = DEFAULT_NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeMonotonic:
    """Monotonic clock for SimBroker's time-driven partial fills."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class LogCapture(logging.Handler):
    """Collects every structured event as the parsed JSON a real handler would emit."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))

    @property
    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.records if name is None or r["event"] == name]

    def names(self) -> list[str]:
        return [r["event"] for r in self.records]

    def text(self) -> str:
        return "\n".join(self.lines)


class ListAlertSink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    def send(self, kind: str, message: str, **details: Any) -> None:
        self.sent.append((kind, message, details))

    @property
    def kinds(self) -> list[str]:
        return [k for k, _, _ in self.sent]


@pytest.fixture(autouse=True)
def logs() -> Iterator[LogCapture]:
    """Capture structured events, and undo anything a test (e.g. a CLI call) did to logging.

    ``tv-bridge`` commands call ``configure_logging``, which replaces the package logger's
    handlers globally. Without restoring them, a later test would log into a stale handler
    attached to a closed capture stream.
    """
    logger = logging.getLogger(LOGGER_NAME)
    saved = (list(logger.handlers), logger.level, logger.propagate)
    capture = LogCapture()
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        yield capture
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if handler is not capture and handler not in saved[0]:
                handler.close()  # created during the test (e.g. by configure_logging)
        for handler in saved[0]:
            logger.addHandler(handler)
        logger.setLevel(saved[1])
        logger.propagate = saved[2]


def default_risk(**overrides: Any) -> RiskConfig:
    """Generous limits; individual tests tighten exactly the rule they exercise."""
    values: dict[str, Any] = {
        "max_qty_per_order": 100,
        "max_position_per_symbol": 250,
        "max_orders_per_day": 50,
        "allowed_symbols": ["AAPL", "MSFT", "SPY"],
        "trading_hours": TradingHours(
            enabled=True,
            timezone="UTC",
            start="00:00",
            end="24:00",
            days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        ),
    }
    values.update(overrides)
    return RiskConfig(**values)


def make_alert(**overrides: Any) -> dict[str, Any]:
    alert: dict[str, Any] = {
        "alert_id": "alert-0001",
        "symbol": "AAPL",
        "side": "buy",
        "qty": 10,
        "order_type": "market",
    }
    alert.update(overrides)
    return {k: v for k, v in alert.items() if v is not ...}  # `...` removes a field


@dataclass
class Harness:
    """A running app plus handles on everything a test may want to poke."""

    client: TestClient
    rt: Runtime
    broker: SimBroker
    clock: FakeClock
    mono: FakeMonotonic
    alerts: ListAlertSink
    settings: Settings
    sleeps: list[float] = field(default_factory=list)

    def post(
        self,
        alert: dict[str, Any] | None = None,
        *,
        secret: str | None = WEBHOOK_SECRET,
        in_header: bool = False,
        **overrides: Any,
    ) -> Any:
        body = make_alert(**overrides) if alert is None else dict(alert)
        headers = {}
        if secret is not None:
            if in_header:
                headers["X-Webhook-Secret"] = secret
            else:
                body["secret"] = secret
        return self.client.post("/webhook/tradingview", json=body, headers=headers)

    def reconcile(self) -> Any:
        return self.rt.reconciler.run_once()

    def kill(self) -> Any:
        return self.client.post("/admin/kill", headers={"X-Admin-Secret": ADMIN_SECRET})

    # convenient state readers
    def position(self, symbol: str = "AAPL") -> Any:
        return self.rt.store.get_position(symbol)

    def order_for(self, alert_id: str) -> Any:
        return self.rt.store.get_order_by_alert(alert_id)

    def alert_row_count(self) -> int:
        row = self.rt.db.query_one("SELECT COUNT(*) AS n FROM alerts")
        return int(row["n"]) if row else 0

    def order_row_count(self) -> int:
        row = self.rt.db.query_one("SELECT COUNT(*) AS n FROM orders")
        return int(row["n"]) if row else 0


def make_settings(tmp_path: Path, **env_overrides: str) -> Settings:
    env = {
        "WEBHOOK_SECRET": WEBHOOK_SECRET,
        "ADMIN_SECRET": ADMIN_SECRET,
        "DB_PATH": str(tmp_path / "bridge.db"),
        "HEARTBEAT_PATH": str(tmp_path / "heartbeat.json"),
        "RISK_CONFIG_PATH": str(tmp_path / "unused-risk.yaml"),
        "UNKNOWN_GRACE_S": "120",
        **env_overrides,
    }
    return Settings.from_env(env)


HarnessFactory = Callable[..., Harness]


@pytest.fixture
def make_harness(tmp_path: Path) -> Iterator[HarnessFactory]:
    created: list[Harness] = []

    def factory(
        *,
        risk: RiskConfig | None = None,
        broker: SimBroker | None = None,
        clock: FakeClock | None = None,
        mono: FakeMonotonic | None = None,
        env: dict[str, str] | None = None,
        sim_config: SimConfig | None = None,
    ) -> Harness:
        settings = make_settings(tmp_path, **(env or {}))
        fake_clock = clock or FakeClock()
        fake_mono = mono or FakeMonotonic()
        sleeps: list[float] = []
        sim = broker or SimBroker(sim_config, clock=fake_mono, sleep=sleeps.append)
        sink = ListAlertSink()
        app = create_app(
            settings,
            broker=sim,
            risk=risk or default_risk(),
            clock=fake_clock,
            alert_sink=sink,
            sleep=sleeps.append,
            start_background=False,
        )
        harness = Harness(
            client=TestClient(app),
            rt=app.state.runtime,
            broker=sim,
            clock=fake_clock,
            mono=fake_mono,
            alerts=sink,
            settings=settings,
            sleeps=sleeps,
        )
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.client.close()
        harness.rt.db.close()


@pytest.fixture
def h(make_harness: HarnessFactory) -> Harness:
    """Default harness: generous risk limits, SimBroker, fixed clock."""
    return make_harness()
