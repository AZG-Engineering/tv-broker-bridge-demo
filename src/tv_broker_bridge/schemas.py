"""Strict schema for the TradingView alert body.

Strict means: no coercion (``"10"`` is not ``10``), no unknown fields, no
duplicate keys, no NaN/Infinity, and cross-field rules for limit orders. A
payload that fails any of these is rejected with a machine-readable reason
rather than being "fixed up" into something the sender did not say.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tv_broker_bridge.models import OrderRequest, OrderType, Side

MAX_QTY = 1_000_000
MAX_PRICE = Decimal(10_000_000)
SECRET_FIELD = "secret"  # noqa: S105 - the JSON key name, not a credential


class PayloadError(ValueError):
    """The body could not be parsed or failed schema validation."""

    def __init__(self, code: str, details: list[dict[str, str]] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or []


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number literal {name!r} is not allowed")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse_json_object(raw: bytes) -> dict[str, Any]:
    """Parse ``raw`` as a JSON object, strictly."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadError("invalid_encoding") from exc
    try:
        data = json.loads(
            text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicates
        )
    except (ValueError, RecursionError) as exc:
        raise PayloadError("invalid_json", [{"msg": str(exc)[:200]}]) from exc
    if not isinstance(data, dict):
        raise PayloadError("not_a_json_object")
    return data


class Alert(BaseModel):
    """A validated TradingView alert. Field names follow the wire format."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    alert_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$")
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")
    side: Literal["buy", "sell"]
    qty: int = Field(gt=0, le=MAX_QTY)
    order_type: Literal["market", "limit"]
    limit_price: Decimal | None = None

    @field_validator("limit_price", mode="before")
    @classmethod
    def _price_from_json_number(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            # ValueError (not TypeError) so pydantic reports it as a validation error.
            raise ValueError("limit_price must be a JSON number")  # noqa: TRY004
        try:
            price = Decimal(str(value))
        except InvalidOperation as exc:  # pragma: no cover - str(float) is always parseable
            raise ValueError("limit_price is not a number") from exc
        if not price.is_finite() or price <= 0 or price > MAX_PRICE:
            raise ValueError(f"limit_price must be > 0 and <= {MAX_PRICE}")
        return price

    @model_validator(mode="after")
    def _limit_price_matches_order_type(self) -> Alert:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required when order_type is 'limit'")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("limit_price must be omitted or null when order_type is 'market'")
        return self

    # -- helpers -----------------------------------------------------------------------------
    @property
    def side_enum(self) -> Side:
        return Side(self.side)

    @property
    def order_type_enum(self) -> OrderType:
        return OrderType(self.order_type)

    @property
    def qty_decimal(self) -> Decimal:
        return Decimal(self.qty)

    def to_order_request(self, client_order_id: str) -> OrderRequest:
        return OrderRequest(
            client_order_id=client_order_id,
            symbol=self.symbol,
            side=self.side_enum,
            qty=self.qty_decimal,
            order_type=self.order_type_enum,
            limit_price=self.limit_price,
        )


def validate_alert(data: dict[str, Any]) -> Alert:
    """Validate an already-parsed (and secret-stripped) alert dict."""
    try:
        return Alert.model_validate(data)
    except ValidationError as exc:
        # Report *where* and *why*, never the offending value: it may be arbitrarily large
        # or attacker-controlled, and the log is not the place to echo it.
        details = [
            {
                "field": ".".join(str(p) for p in err["loc"]) or "(body)",
                "type": str(err["type"]),
                "msg": str(err["msg"])[:200],
            }
            for err in exc.errors()
        ]
        raise PayloadError("schema_validation_failed", details) from exc
