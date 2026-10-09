"""The alert pipeline: dedupe -> risk gate -> reserve -> submit -> record.

Design notes (the reasons this module is shaped the way it is):

* **Claim before you send.** The alert_id, the risk decision and a
  ``PENDING_SUBMIT`` order row are written in one ``BEGIN IMMEDIATE`` transaction
  *before* any broker call. A duplicate therefore finds the claim and stops, and
  the reservation counts toward position/daily limits while the order is in flight.
* **Deterministic client_order_id.** Derived from the alert_id, so re-sending the
  same order to the broker is idempotent at the broker too.
* **Unknown outcome is a state, not an exception.** If the broker connection
  fails, we cannot know whether the order arrived. After a few quick retries the
  order is parked as ``SUBMIT_UNKNOWN``: it still counts as exposure, the same
  alert_id may retry delivery, and the reconciler resolves it against the broker.
  We never fire-and-forget a second order to "make sure".
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.clock import Clock
from tv_broker_bridge.errors import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    OrderRejectedError,
)
from tv_broker_bridge.kill_switch import KillSwitch
from tv_broker_bridge.ledger import OrderLedger
from tv_broker_bridge.logging_utils import log_event
from tv_broker_bridge.models import BrokerOrder, OrderRecord, OrderRequest, OrderStatus
from tv_broker_bridge.risk import (
    RiskConfig,
    RiskContext,
    Violation,
    evaluate,
    local_day_bounds,
)
from tv_broker_bridge.schemas import Alert
from tv_broker_bridge.store import AlertRow, AlertState, Store, iso


def client_order_id_for(alert_id: str) -> str:
    """Stable broker-side id for an alert (fits Alpaca's 128-char limit)."""
    return "tvb-" + hashlib.sha256(alert_id.encode()).hexdigest()[:32]


def payload_hash(alert: Alert) -> str:
    canonical = json.dumps(alert.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Outcome:
    """What to tell the caller. ``http_status`` reflects request handling, not trade quality."""

    http_status: int
    status: str
    alert_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {"status": self.status, "alert_id": self.alert_id, **self.detail}


@dataclass(frozen=True)
class _Claim:
    kind: str  # halted | duplicate | rejected_risk | reserved | retry
    existing: AlertRow | None = None
    payload_matches: bool = True
    violations: tuple[Violation, ...] = ()
    order: OrderRecord | None = None


def _order_summary(order: OrderRecord) -> dict[str, Any]:
    return {
        "client_order_id": order.client_order_id,
        "broker_order_id": order.broker_order_id,
        "status": order.status.value,
        "qty": str(order.qty),
        "filled_qty": str(order.filled_qty),
    }


class AlertProcessor:
    def __init__(
        self,
        *,
        store: Store,
        broker: Broker,
        risk: RiskConfig,
        kill: KillSwitch,
        ledger: OrderLedger,
        clock: Clock,
        attempts: int = 3,
        backoff_s: float = 0.2,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.risk = risk
        self.kill = kill
        self.ledger = ledger
        self.clock = clock
        self.attempts = max(1, attempts)
        self.backoff_s = backoff_s
        self._sleep = sleep or time.sleep

    # -- entry point -------------------------------------------------------------------------
    def process(self, alert: Alert) -> Outcome:
        log_event(
            "received",
            alert_id=alert.alert_id,
            symbol=alert.symbol,
            side=alert.side,
            qty=alert.qty,
            order_type=alert.order_type,
            limit_price=alert.limit_price,
        )
        claim = self._claim(alert)

        if claim.kind == "halted":
            log_event(
                "rejected_halted",
                level=logging.WARNING,
                alert_id=alert.alert_id,
                reason="kill_switch_engaged",
            )
            return Outcome(503, "halted", alert.alert_id, {"reason": "kill_switch_engaged"})

        if claim.kind == "duplicate":
            assert claim.existing is not None  # noqa: S101
            log_event(
                "deduped",
                level=logging.INFO if claim.payload_matches else logging.WARNING,
                alert_id=alert.alert_id,
                original_state=claim.existing.state.value,
                original_received_at=claim.existing.received_at,
                payload_matches=claim.payload_matches,
            )
            return Outcome(
                200,
                "duplicate",
                alert.alert_id,
                {"original_state": claim.existing.state.value},
            )

        if claim.kind == "rejected_risk":
            codes = [v.code for v in claim.violations]
            log_event(
                "rejected_by_risk",
                level=logging.WARNING,
                alert_id=alert.alert_id,
                symbol=alert.symbol,
                side=alert.side,
                qty=alert.qty,
                violations=[v.as_dict() for v in claim.violations],
                codes=codes,
            )
            return Outcome(
                200,
                "rejected_by_risk",
                alert.alert_id,
                {"violations": [v.as_dict() for v in claim.violations]},
            )

        assert claim.order is not None  # noqa: S101 - reserved / retry always carry an order
        return self._deliver(claim.order, retry=claim.kind == "retry")

    # -- step 1: atomic claim + risk decision + reservation -----------------------------------
    def _claim(self, alert: Alert) -> _Claim:
        now = self.clock()
        digest = payload_hash(alert)
        with self.store.db.transaction():
            if self.kill.is_engaged():
                return _Claim("halted")

            existing = self.store.get_alert(alert.alert_id)
            if existing is not None:
                order = self.store.get_order_by_alert(alert.alert_id)
                if existing.state is AlertState.SUBMIT_UNKNOWN and order is not None:
                    # Same alert_id, delivery outcome unknown: complete delivery of the
                    # already-approved order (idempotent at the broker). Flip the state inside
                    # this transaction so a concurrent retry sees "duplicate".
                    self.store.set_alert_state(alert.alert_id, AlertState.ACCEPTED, now)
                    self.store.update_order(
                        order.client_order_id, status=OrderStatus.PENDING_SUBMIT, now=now
                    )
                    return _Claim("retry", order=self.store.get_order(order.client_order_id))
                return _Claim(
                    "duplicate", existing=existing, payload_matches=existing.payload_hash == digest
                )

            tz = self.risk.trading_hours.tz
            day_start, day_end = local_day_bounds(now, tz)
            ctx = RiskContext(
                now=now,
                effective_position=self.store.get_position(alert.symbol)
                + self.store.working_exposure(alert.symbol),
                orders_today=self.store.count_orders_created_between(day_start, day_end),
            )
            decision = evaluate(self.risk, alert, ctx)

            def record_alert(state: AlertState, reason: str | None) -> None:
                self.store.insert_alert(
                    alert_id=alert.alert_id,
                    state=state,
                    payload_hash=digest,
                    symbol=alert.symbol,
                    side=alert.side_enum,
                    qty=alert.qty_decimal,
                    order_type=alert.order_type_enum,
                    limit_price=alert.limit_price,
                    reason=reason,
                    now=now,
                )

            if not decision.allowed:
                record_alert(AlertState.REJECTED_RISK, ",".join(decision.codes))
                return _Claim("rejected_risk", violations=decision.violations)

            record_alert(AlertState.ACCEPTED, None)
            order = OrderRecord(
                client_order_id=client_order_id_for(alert.alert_id),
                alert_id=alert.alert_id,
                symbol=alert.symbol,
                side=alert.side_enum,
                qty=alert.qty_decimal,
                order_type=alert.order_type_enum,
                limit_price=alert.limit_price,
                status=OrderStatus.PENDING_SUBMIT,
                created_at=iso(now),
                updated_at=iso(now),
            )
            self.store.insert_order(order)
            return _Claim("reserved", order=order)

    # -- step 2: deliver to the broker (no DB transaction held across network I/O) ------------
    def _deliver(self, order: OrderRecord, *, retry: bool) -> Outcome:
        request = self._request_for(order)
        last_error: BrokerError | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                broker_order = self.broker.submit_order(request)
            except OrderRejectedError as exc:
                return self._on_rejected(order, exc.reason)
            except BrokerAuthError as exc:
                return self._on_auth_failure(order, exc)
            except BrokerError as exc:  # connection failure / malformed reply: outcome unknown
                last_error = exc
                log_event(
                    "submit_retry" if attempt < self.attempts else "submit_gave_up",
                    level=logging.WARNING,
                    alert_id=order.alert_id,
                    client_order_id=order.client_order_id,
                    attempt=attempt,
                    max_attempts=self.attempts,
                    error=str(exc),
                    retry_of_unknown=retry,
                )
                if attempt < self.attempts:
                    self._sleep(self.backoff_s * 2 ** (attempt - 1))
                continue
            return self._on_sent(order, broker_order)
        return self._on_unknown(order, last_error)

    @staticmethod
    def _request_for(order: OrderRecord) -> OrderRequest:
        return OrderRequest(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            order_type=order.order_type,
            limit_price=order.limit_price,
        )

    # -- step 3: record what happened ---------------------------------------------------------
    def _on_sent(self, order: OrderRecord, broker_order: BrokerOrder) -> Outcome:
        if broker_order.status is OrderStatus.REJECTED:
            return self._on_rejected(order, broker_order.reject_reason or "rejected by broker")
        log_event(
            "sent",
            alert_id=order.alert_id,
            client_order_id=order.client_order_id,
            broker_order_id=broker_order.broker_order_id,
            symbol=order.symbol,
            side=order.side.value,
            qty=order.qty,
            order_type=order.order_type.value,
            limit_price=order.limit_price,
            broker_status=broker_order.status.value,
        )
        updated = self.ledger.apply_broker_order(order, broker_order)
        self._cancel_if_killed_meanwhile(updated)
        return Outcome(200, "accepted", order.alert_id or "", {"order": _order_summary(updated)})

    def _cancel_if_killed_meanwhile(self, order: OrderRecord) -> None:
        """Close the race where the kill switch was thrown while this order was in flight."""
        if not (self.kill.is_engaged() and order.status.is_working and order.broker_order_id):
            return
        try:
            self.broker.cancel_order(order.broker_order_id)
            log_event(
                "kill_switch_late_cancel",
                level=logging.WARNING,
                client_order_id=order.client_order_id,
            )
        except BrokerError as exc:
            # Leave it: the supervisor retries cancel-all while the switch is engaged.
            self.kill.mark_cancel_pending()
            log_event("kill_switch_late_cancel_failed", level=logging.ERROR, error=str(exc))

    def _on_rejected(self, order: OrderRecord, reason: str) -> Outcome:
        now = self.clock()
        with self.store.db.transaction():
            self.store.update_order(
                order.client_order_id, status=OrderStatus.REJECTED, now=now, reject_reason=reason
            )
            if order.alert_id:
                self.store.set_alert_state(
                    order.alert_id, AlertState.REJECTED_BROKER, now, reason=reason
                )
        log_event(
            "broker_rejected",
            level=logging.WARNING,
            alert_id=order.alert_id,
            client_order_id=order.client_order_id,
            reason=reason,
        )
        return Outcome(200, "rejected_by_broker", order.alert_id or "", {"reason": reason})

    def _on_auth_failure(self, order: OrderRecord, exc: BrokerAuthError) -> Outcome:
        now = self.clock()
        with self.store.db.transaction():
            self.store.update_order(
                order.client_order_id,
                status=OrderStatus.FAILED,
                now=now,
                reject_reason="broker_auth_error",
            )
            if order.alert_id:
                self.store.set_alert_state(
                    order.alert_id, AlertState.FAILED, now, reason="broker_auth_error"
                )
        log_event(
            "broker_auth_error",
            level=logging.CRITICAL,
            alert_id=order.alert_id,
            client_order_id=order.client_order_id,
            error=str(exc),
        )
        return Outcome(502, "broker_error", order.alert_id or "", {"retryable": False})

    def _on_unknown(self, order: OrderRecord, error: BrokerError | None) -> Outcome:
        now = self.clock()
        with self.store.db.transaction():
            self.store.update_order(
                order.client_order_id, status=OrderStatus.SUBMIT_UNKNOWN, now=now
            )
            if order.alert_id:
                self.store.set_alert_state(
                    order.alert_id, AlertState.SUBMIT_UNKNOWN, now, reason="broker_unavailable"
                )
        log_event(
            "submit_unknown",
            level=logging.ERROR,
            alert_id=order.alert_id,
            client_order_id=order.client_order_id,
            error=str(error) if error else None,
            is_connection_error=isinstance(error, BrokerConnectionError),
        )
        return Outcome(503, "broker_unavailable", order.alert_id or "", {"retryable": True})
