"""Alpaca **paper-trading** REST adapter.

Safety properties, each covered by tests:

* The base URL must be exactly ``https://paper-api.alpaca.markets``. Anything
  else (the live host, look-alike hosts, ``http``, embedded credentials, odd
  ports or paths) raises ``LiveEndpointRefusedError`` at construction, so a
  misconfigured process refuses to start rather than trading real money.
* The same check runs again on every outgoing request, and redirects are never
  followed, so a rogue 3xx cannot steer a request to another host.
* API keys come from environment variables only, are sent only as request
  headers, and never appear in ``repr`` or in logs.
* Alpaca's REST API is not touched by the test suite: tests inject an
  ``httpx.MockTransport``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.errors import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    ConfigError,
    LiveEndpointRefusedError,
    OrderNotCancelableError,
    OrderNotFoundError,
    OrderRejectedError,
)
from tv_broker_bridge.logging_utils import log_event
from tv_broker_bridge.models import (
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)

PAPER_HOST = "paper-api.alpaca.markets"
PAPER_BASE_URL = f"https://{PAPER_HOST}"
LIVE_HOST = "api.alpaca.markets"
_ALLOWED_PATHS = {"", "/", "/v2", "/v2/"}

_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.ACCEPTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
    "pending_cancel": OrderStatus.ACCEPTED,
    "pending_replace": OrderStatus.ACCEPTED,
    "calculated": OrderStatus.ACCEPTED,
    "stopped": OrderStatus.ACCEPTED,
    "suspended": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "done_for_day": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


def validate_paper_base_url(url: str) -> str:
    """Return the canonical paper base URL, or raise ``LiveEndpointRefusedError``."""

    def refuse(why: str) -> LiveEndpointRefusedError:
        return LiveEndpointRefusedError(
            f"refusing to start: ALPACA_BASE_URL must be {PAPER_BASE_URL} ({why}). "
            "This project only supports paper trading."
        )

    if not url or any(ch.isspace() or ord(ch) < 32 or ord(ch) > 126 for ch in url):
        raise refuse("empty, or contains whitespace/control/non-ASCII characters")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == LIVE_HOST:
        raise refuse("that is the LIVE trading host")
    if parts.scheme != "https":
        raise refuse("scheme must be https")
    if parts.username is not None or parts.password is not None:
        raise refuse("credentials in the URL are not allowed")
    if host != PAPER_HOST:
        raise refuse(f"unexpected host {host or '(none)'!r}")
    try:
        port = parts.port
    except ValueError:
        raise refuse("invalid port") from None
    if port not in (None, 443):
        raise refuse(f"unexpected port {port}")
    if parts.path not in _ALLOWED_PATHS or parts.query or parts.fragment:
        raise refuse("unexpected path, query or fragment")
    return PAPER_BASE_URL


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _parse_order(data: Mapping[str, Any]) -> BrokerOrder:
    raw_status = str(data["status"])
    status = _STATUS_MAP.get(raw_status)
    if status is None:
        # Unknown => treat as working: the conservative choice for exposure and cancel-all.
        log_event("broker_unknown_status", level=logging.WARNING, status=raw_status)
        status = OrderStatus.ACCEPTED
    raw_type = str(data.get("type", "market"))
    order_type = OrderType(raw_type) if raw_type in ("market", "limit") else OrderType.OTHER
    limit_price = data.get("limit_price")
    avg = data.get("filled_avg_price")
    return BrokerOrder(
        broker_order_id=str(data["id"]),
        client_order_id=str(data.get("client_order_id", "")),
        symbol=str(data["symbol"]),
        side=Side(str(data["side"])),
        qty=_dec(data.get("qty")),
        order_type=order_type,
        status=status,
        filled_qty=_dec(data.get("filled_qty")),
        limit_price=None if limit_price in (None, "") else _dec(limit_price),
        avg_fill_price=None if avg in (None, "") else _dec(avg),
    )


class AlpacaPaperBroker(Broker):
    name = "alpaca_paper"

    def __init__(
        self,
        *,
        key_id: str | None,
        secret_key: str | None,
        base_url: str = PAPER_BASE_URL,
        timeout_s: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = validate_paper_base_url(base_url)
        if not key_id or not secret_key:
            raise ConfigError(
                "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be set (paper-trading keys)"
            )
        self._key_hint = key_id[:4] + "..."
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [self._assert_paper_request]},
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> AlpacaPaperBroker:
        """Build from ``ALPACA_API_KEY_ID`` / ``ALPACA_API_SECRET_KEY`` / ``ALPACA_BASE_URL``."""
        env = os.environ if environ is None else environ
        return cls(
            key_id=env.get("ALPACA_API_KEY_ID"),
            secret_key=env.get("ALPACA_API_SECRET_KEY"),
            base_url=env.get("ALPACA_BASE_URL", PAPER_BASE_URL),
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"AlpacaPaperBroker(base_url={self.base_url!r}, key_id={self._key_hint!r})"

    @staticmethod
    def _assert_paper_request(request: httpx.Request) -> None:
        if request.url.scheme != "https" or request.url.host != PAPER_HOST:
            raise LiveEndpointRefusedError(
                f"blocked a request to {request.url.host}: only {PAPER_HOST} is permitted"
            )

    # -- HTTP plumbing -----------------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.TransportError as exc:  # timeouts, DNS, connection resets, ...
            raise BrokerConnectionError(f"{type(exc).__name__} calling Alpaca") from exc
        status = response.status_code
        if 300 <= status < 400:
            raise BrokerError(f"unexpected redirect (HTTP {status}) refused")
        if status == 401:
            raise BrokerAuthError("Alpaca rejected the API credentials (HTTP 401)")
        if status == 429 or status >= 500:
            raise BrokerConnectionError(f"Alpaca returned HTTP {status}")
        return response

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("message"):
                return str(body["message"])[:300]
        except ValueError:
            pass
        return f"HTTP {response.status_code}"

    @staticmethod
    def _order_from(response: httpx.Response) -> BrokerOrder:
        try:
            return _parse_order(response.json())
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise BrokerConnectionError("malformed order payload from Alpaca") from exc

    # -- Broker interface --------------------------------------------------------------------
    def ping(self) -> None:
        response = self._request("GET", "/v2/clock")
        if response.status_code != 200:
            raise BrokerConnectionError(f"clock check returned HTTP {response.status_code}")

    def submit_order(self, request: OrderRequest) -> BrokerOrder:
        body: dict[str, Any] = {
            "symbol": request.symbol,
            "qty": str(request.qty),
            "side": request.side.value,
            "type": request.order_type.value,
            "time_in_force": "day",
            "client_order_id": request.client_order_id,
        }
        if request.limit_price is not None:
            body["limit_price"] = str(request.limit_price)
        response = self._request("POST", "/v2/orders", json=body)
        if response.status_code == 200:
            return self._order_from(response)
        message = self._message(response)
        if response.status_code == 422 and "client_order_id" in message.lower():
            # Duplicate client_order_id: an earlier attempt got through. Return that order.
            existing = self.get_order_by_client_id(request.client_order_id)
            if existing is not None:
                return existing
        raise OrderRejectedError(message)

    def get_order(self, broker_order_id: str) -> BrokerOrder:
        response = self._request("GET", f"/v2/orders/{quote(broker_order_id, safe='')}")
        if response.status_code == 404:
            raise OrderNotFoundError(broker_order_id)
        if response.status_code != 200:
            raise BrokerError(f"get_order failed: {self._message(response)}")
        return self._order_from(response)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        response = self._request(
            "GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BrokerError(f"order lookup failed: {self._message(response)}")
        return self._order_from(response)

    def list_orders(self) -> list[BrokerOrder]:
        response = self._request(
            "GET", "/v2/orders", params={"status": "all", "limit": "500", "direction": "desc"}
        )
        if response.status_code != 200:
            raise BrokerError(f"list_orders failed: {self._message(response)}")
        try:
            return [_parse_order(item) for item in response.json()]
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise BrokerConnectionError("malformed order list from Alpaca") from exc

    def list_positions(self) -> list[BrokerPosition]:
        response = self._request("GET", "/v2/positions")
        if response.status_code != 200:
            raise BrokerError(f"list_positions failed: {self._message(response)}")
        positions: list[BrokerPosition] = []
        try:
            for item in response.json():
                qty = abs(_dec(item["qty"]))
                signed = -qty if str(item.get("side", "long")) == "short" else qty
                positions.append(BrokerPosition(str(item["symbol"]), signed))
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            raise BrokerConnectionError("malformed positions payload from Alpaca") from exc
        return positions

    def cancel_order(self, broker_order_id: str) -> None:
        response = self._request("DELETE", f"/v2/orders/{quote(broker_order_id, safe='')}")
        if response.status_code == 404:
            raise OrderNotFoundError(broker_order_id)
        if response.status_code == 422:
            raise OrderNotCancelableError(self._message(response))
        if response.status_code not in (200, 204):
            raise BrokerError(f"cancel failed: {self._message(response)}")

    def cancel_all_orders(self) -> list[str]:
        response = self._request("DELETE", "/v2/orders")
        if response.status_code not in (200, 207):
            raise BrokerError(f"cancel-all failed: {self._message(response)}")
        try:
            results = response.json()
            canceled = [str(r["id"]) for r in results if r.get("status") == 200]
            failed = [str(r["id"]) for r in results if r.get("status") not in (200, 404, 422)]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BrokerConnectionError("malformed cancel-all payload from Alpaca") from exc
        if failed:
            raise BrokerError(f"{len(failed)} order(s) could not be canceled: {failed}")
        return canceled

    def close(self) -> None:
        self._client.close()
