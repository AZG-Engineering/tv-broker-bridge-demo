"""Pre-trade risk gate.

``evaluate`` is a pure function: given the configuration, the alert and a
snapshot of current exposure, it returns every rule the order violates. All
rules are evaluated (no short-circuit) so one log line explains the whole
rejection. The caller decides what to do; this module never touches I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tv_broker_bridge.errors import ConfigError
from tv_broker_bridge.schemas import Alert

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-4]):([0-5]\d)$")

# Violation codes (stable; they appear in logs and API responses).
SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
OUTSIDE_TRADING_HOURS = "outside_trading_hours"
QTY_EXCEEDS_MAX = "qty_exceeds_max_per_order"
POSITION_LIMIT = "position_limit_exceeded"
DAILY_ORDER_LIMIT = "daily_order_limit_exceeded"


def _to_minutes(value: str, *, allow_24: bool) -> int:
    match = _HHMM.match(value)
    if not match:
        raise ValueError(f"{value!r} is not HH:MM")
    minutes = int(match.group(1)) * 60 + int(match.group(2))
    if minutes > 24 * 60 or (minutes == 24 * 60 and not allow_24):
        raise ValueError(f"{value!r} is out of range")
    return minutes


class TradingHours(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    timezone: str
    start: str
    end: str
    days: list[Weekday] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("start")
    @classmethod
    def _valid_start(cls, value: str) -> str:
        _to_minutes(value, allow_24=False)
        return value

    @field_validator("end")
    @classmethod
    def _valid_end(cls, value: str) -> str:
        _to_minutes(value, allow_24=True)
        return value

    @model_validator(mode="after")
    def _ordered_window(self) -> TradingHours:
        if self.start_minutes >= self.end_minutes:
            raise ValueError("start must be earlier than end (overnight windows are unsupported)")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def start_minutes(self) -> int:
        return _to_minutes(self.start, allow_24=False)

    @property
    def end_minutes(self) -> int:
        return _to_minutes(self.end, allow_24=True)


class RiskConfig(BaseModel):
    """Risk limits. Every field is mandatory: there is no 'unlimited' default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_qty_per_order: int = Field(gt=0)
    max_position_per_symbol: int = Field(gt=0)
    max_orders_per_day: int = Field(gt=0)
    allowed_symbols: list[str] = Field(min_length=1)
    trading_hours: TradingHours

    @field_validator("allowed_symbols")
    @classmethod
    def _normalise_symbols(cls, value: list[str]) -> list[str]:
        cleaned = [s.strip().upper() for s in value]
        if any(not s for s in cleaned):
            raise ValueError("allowed_symbols must not contain blank entries")
        return cleaned


def load_risk_config(path: str | Path) -> RiskConfig:
    """Load and validate the YAML risk file; raises ``ConfigError`` on any problem."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"risk config not found at {p}; copy config/risk.example.yaml to {p}"
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read risk config {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"risk config {p} must be a YAML mapping")
    try:
        return RiskConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"invalid risk config {p}: {problems}") from exc


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Snapshot of exposure at decision time."""

    now: datetime  # timezone-aware
    effective_position: Decimal  # signed: net position + signed remaining qty of working orders
    orders_today: int  # orders attempted today, excluding the one being evaluated


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RiskDecision:
    violations: tuple[Violation, ...]

    @property
    def allowed(self) -> bool:
        return not self.violations

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]


def local_day_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` of the calendar day containing ``now`` in ``tz`` (DST-safe)."""
    local_day: date = now.astimezone(tz).date()
    start = datetime.combine(local_day, time.min, tzinfo=tz)
    end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def _within_trading_hours(hours: TradingHours, now: datetime) -> bool:
    local = now.astimezone(hours.tz)
    if _WEEKDAYS[local.weekday()] not in hours.days:
        return False
    minute_of_day = local.hour * 60 + local.minute
    return hours.start_minutes <= minute_of_day < hours.end_minutes


def evaluate(config: RiskConfig, alert: Alert, ctx: RiskContext) -> RiskDecision:
    """Return every rule ``alert`` violates given the exposure snapshot ``ctx``."""
    violations: list[Violation] = []

    if alert.symbol not in config.allowed_symbols:
        violations.append(
            Violation(SYMBOL_NOT_ALLOWED, f"{alert.symbol} is not in allowed_symbols")
        )

    if config.trading_hours.enabled and not _within_trading_hours(config.trading_hours, ctx.now):
        local = ctx.now.astimezone(config.trading_hours.tz).strftime("%a %H:%M")
        violations.append(
            Violation(
                OUTSIDE_TRADING_HOURS,
                f"{local} {config.trading_hours.timezone} is outside "
                f"{config.trading_hours.start}-{config.trading_hours.end}",
            )
        )

    if alert.qty > config.max_qty_per_order:
        violations.append(
            Violation(
                QTY_EXCEEDS_MAX, f"qty {alert.qty} > max_qty_per_order {config.max_qty_per_order}"
            )
        )

    # Position limit: projected = net position + working orders + this order. An order that
    # reduces |exposure| is always allowed, otherwise a position that drifted over the limit
    # (e.g. a manual trade) could never be worked back down.
    projected = ctx.effective_position + alert.side_enum.sign * alert.qty_decimal
    if abs(projected) > config.max_position_per_symbol and abs(projected) >= abs(
        ctx.effective_position
    ):
        violations.append(
            Violation(
                POSITION_LIMIT,
                f"projected position {projected} would exceed "
                f"max_position_per_symbol {config.max_position_per_symbol}",
            )
        )

    if ctx.orders_today >= config.max_orders_per_day:
        violations.append(
            Violation(
                DAILY_ORDER_LIMIT,
                f"{ctx.orders_today} orders already attempted today "
                f"(max_orders_per_day {config.max_orders_per_day})",
            )
        )

    return RiskDecision(tuple(violations))
