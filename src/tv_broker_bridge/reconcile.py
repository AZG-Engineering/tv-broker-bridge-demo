"""Reconciliation: compare local order/position state with the broker's.

**The broker is the source of truth.** On every pass we fetch the broker's
orders and positions, diff them against the local database, correct the local
side, and log + alert on anything that should not have differed.

Two kinds of difference are distinguished:

* *progress*: a working local order that the broker now reports as more filled,
  or finished. That is how asynchronous fills are normally learned, so it is
  applied and logged (``filled``) but is not a mismatch.
* *mismatch*: everything else. Examples: the broker holds an order we never
  recorded; a local order the broker has no trace of; a response lost after the
  broker accepted an order; a position that differs after progress is applied
  (e.g. a manual trade in the broker's UI); a fill count that went backwards.

To avoid false alarms from the broker changing between our "orders" and
"positions" requests, any pass that finds a mismatch is re-observed against a
fresh snapshot, and only mismatches present in the second observation count.
Races with the live submit path are handled two ways. An order whose submit is still in
flight (``PENDING_SUBMIT`` and young) is left alone, and so is its symbol's position, because
the fill exists at the broker before the submit path has booked it. And a change token
discards the whole pass if any order or position was written locally while it was observing:
the next pass, a few seconds later, sees a settled picture.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from tv_broker_bridge.alerting import AlertSink
from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.clock import Clock
from tv_broker_bridge.errors import BrokerError, OrderNotFoundError
from tv_broker_bridge.health import RuntimeStatus
from tv_broker_bridge.ledger import OrderLedger
from tv_broker_bridge.logging_utils import log_event
from tv_broker_bridge.models import BrokerOrder, OrderRecord, OrderStatus
from tv_broker_bridge.store import AlertState, Store, iso, parse_iso

_T = TypeVar("_T")


def _need(value: _T | None) -> _T:
    """Narrow an Optional that is non-None by construction for this finding kind."""
    if value is None:
        raise RuntimeError("reconcile finding is missing a field its kind requires")
    return value


#: A PENDING_SUBMIT row younger than this belongs to a request still in flight.
INFLIGHT_GRACE_S = 30.0

# Mismatch kinds (stable identifiers used in logs and alerts)
UNKNOWN_BROKER_ORDER = "unknown_broker_order"
MISSING_AT_BROKER = "missing_at_broker"
LOST_RESPONSE = "lost_response"
UNSENT = "unsent"
FILL_REGRESSION = "fill_regression"
TERMINAL_CONFLICT = "terminal_state_conflict"
POSITION_MISMATCH = "position_mismatch"
_PROGRESS = "progress"


@dataclass(frozen=True)
class Mismatch:
    kind: str
    detail: str
    symbol: str | None = None
    client_order_id: str | None = None


@dataclass(frozen=True)
class ReconcileReport:
    ok: bool  # False => the pass could not complete (e.g. broker unreachable)
    mismatches: tuple[Mismatch, ...] = ()
    updates: int = 0
    orders_seen: int = 0
    error: str | None = None
    skipped: bool = False  # local state changed mid-pass; nothing was applied

    @property
    def clean(self) -> bool:
        return self.ok and not self.mismatches


@dataclass(frozen=True)
class _Finding:
    kind: str
    detail: str = ""
    rec: OrderRecord | None = None
    bo: BrokerOrder | None = None
    symbol: str | None = None
    broker_qty: Decimal | None = None  # position_mismatch: the broker's quantity

    @property
    def alerting(self) -> bool:
        return self.kind != _PROGRESS


@dataclass
class _Snapshot:
    orders: list[BrokerOrder]
    positions: dict[str, Decimal]
    lookups: dict[str, BrokerOrder | None] = field(default_factory=dict)


class Reconciler:
    def __init__(
        self,
        *,
        store: Store,
        broker: Broker,
        ledger: OrderLedger,
        alerts: AlertSink,
        clock: Clock,
        status: RuntimeStatus,
        unknown_grace_s: float = 120.0,
    ) -> None:
        self.store = store
        self.broker = broker
        self.ledger = ledger
        self.alerts = alerts
        self.clock = clock
        self.status = status
        self.unknown_grace_s = unknown_grace_s
        self._lock = threading.Lock()  # one pass at a time (supervisor vs. kill switch)

    # -- public ------------------------------------------------------------------------------
    def run_once(self) -> ReconcileReport:
        with self._lock:
            return self._run_once()

    # -- internals ---------------------------------------------------------------------------
    def _run_once(self) -> ReconcileReport:
        try:
            token, snapshot, findings = self._observe()
            if any(f.alerting for f in findings):
                token, snapshot, findings = self._observe()  # confirm against a fresh snapshot
        except BrokerError as exc:
            failures = self.status.record_failure(self.clock(), str(exc))
            log_event(
                "reconcile_failed",
                level=logging.ERROR,
                error=str(exc),
                error_type=type(exc).__name__,
                consecutive_failures=failures,
            )
            return ReconcileReport(ok=False, error=str(exc))

        applied = self._apply(findings, token)
        if applied is None:
            self.status.record_success(self.clock(), 0)
            log_event("reconcile_skipped", reason="local state changed during the pass")
            return ReconcileReport(ok=True, orders_seen=len(snapshot.orders), skipped=True)
        mismatches, updates = applied
        previous_failures = self.status.record_success(self.clock(), len(mismatches))
        if previous_failures:
            log_event("broker_reconnected", level=logging.WARNING, failed_passes=previous_failures)

        for m in mismatches:
            log_event(
                "reconcile_mismatch",
                level=logging.ERROR,
                kind=m.kind,
                detail=m.detail,
                symbol=m.symbol,
                client_order_id=m.client_order_id,
                resolution="local state replaced by broker state",
            )
            self.alerts.send(m.kind, m.detail, symbol=m.symbol, client_order_id=m.client_order_id)
        log_event(
            "reconciled",
            level=logging.WARNING if mismatches else logging.INFO,
            mismatches=len(mismatches),
            updates=updates,
            orders_seen=len(snapshot.orders),
            positions_seen=len(snapshot.positions),
            clean=not mismatches,
        )
        return ReconcileReport(
            ok=True,
            mismatches=tuple(mismatches),
            updates=updates,
            orders_seen=len(snapshot.orders),
        )

    def _observe(self) -> tuple[int, _Snapshot, list[_Finding]]:
        token = self.store.change_token()  # taken BEFORE reading local state
        local_orders = self.store.list_orders()
        local_positions = self.store.list_positions()
        snapshot = self._snapshot([r for r in local_orders if r.status.is_working])
        findings = self._diff(snapshot, local_orders, local_positions, self.clock())
        return token, snapshot, findings

    def _snapshot(self, local_working: list[OrderRecord]) -> _Snapshot:
        orders = self.broker.list_orders()
        positions = {p.symbol: p.qty for p in self.broker.list_positions()}
        listed_client = {o.client_order_id for o in orders}
        listed_broker = {o.broker_order_id for o in orders}
        snap = _Snapshot(orders=orders, positions=positions)
        # The order list is bounded (most recent N), so look up working orders it omitted.
        for rec in local_working:
            if rec.client_order_id in listed_client or rec.broker_order_id in listed_broker:
                continue
            try:
                if rec.broker_order_id:
                    snap.lookups[rec.client_order_id] = self.broker.get_order(rec.broker_order_id)
                else:
                    snap.lookups[rec.client_order_id] = self.broker.get_order_by_client_id(
                        rec.client_order_id
                    )
            except OrderNotFoundError:
                snap.lookups[rec.client_order_id] = None
        return snap

    # -- diff (pure: no writes) --------------------------------------------------------------
    def _diff(
        self,
        snap: _Snapshot,
        local_orders: list[OrderRecord],
        local_positions: dict[str, Decimal],
        now: datetime,
    ) -> list[_Finding]:
        by_client = {r.client_order_id: r for r in local_orders}
        by_broker = {r.broker_order_id: r for r in local_orders if r.broker_order_id}
        findings: list[_Finding] = []
        expected_delta: dict[str, Decimal] = defaultdict(Decimal)
        matched: set[str] = set()

        for bo in snap.orders:
            rec = by_client.get(bo.client_order_id) or by_broker.get(bo.broker_order_id)
            if rec is None:
                if bo.status.is_working:  # historical closed orders are not our concern
                    findings.append(
                        _Finding(
                            UNKNOWN_BROKER_ORDER,
                            f"broker has working order {bo.broker_order_id} "
                            f"({bo.side.value} {bo.qty} {bo.symbol}) "
                            "that the bridge never recorded",
                            bo=bo,
                            symbol=bo.symbol,
                        )
                    )
                continue
            matched.add(rec.client_order_id)
            findings.extend(self._compare(rec, bo, now, expected_delta))

        for rec in local_orders:
            if not rec.status.is_working or rec.client_order_id in matched:
                continue
            found = snap.lookups.get(rec.client_order_id)
            if found is not None:
                findings.extend(self._compare(rec, found, now, expected_delta))
            else:
                findings.extend(self._absent(rec, now))

        inflight_symbols = {
            r.symbol
            for r in local_orders
            if r.status is OrderStatus.PENDING_SUBMIT
            and (now - parse_iso(r.created_at)).total_seconds() < INFLIGHT_GRACE_S
        }
        expected = dict(local_positions)
        for symbol, delta in expected_delta.items():
            expected[symbol] = expected.get(symbol, Decimal(0)) + delta
        for symbol in sorted(set(expected) | set(snap.positions)):
            if symbol in inflight_symbols:
                continue  # a fill may exist at the broker that the submit path hasn't booked
            local_qty = expected.get(symbol, Decimal(0))
            broker_qty = snap.positions.get(symbol, Decimal(0))
            if local_qty != broker_qty:
                findings.append(
                    _Finding(
                        POSITION_MISMATCH,
                        f"{symbol}: local position {local_qty} != broker position {broker_qty}",
                        symbol=symbol,
                        broker_qty=broker_qty,
                    )
                )
        return findings

    def _compare(
        self,
        rec: OrderRecord,
        bo: BrokerOrder,
        now: datetime,
        expected_delta: dict[str, Decimal],
    ) -> list[_Finding]:
        age_s = (now - parse_iso(rec.created_at)).total_seconds()
        if rec.status is OrderStatus.PENDING_SUBMIT and age_s < INFLIGHT_GRACE_S:
            return []  # the submit path is still running and will record this itself

        delta = bo.filled_qty - rec.filled_qty
        if rec.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMIT_UNKNOWN):
            expected_delta[rec.symbol] += rec.side.sign * max(delta, Decimal(0))
            return [
                _Finding(
                    LOST_RESPONSE,
                    f"order {rec.client_order_id} was {rec.status.value} locally but exists at the "
                    f"broker as {bo.status.value} (filled {bo.filled_qty}/{bo.qty}); "
                    "the submit response was lost",
                    rec=rec,
                    bo=bo,
                    symbol=rec.symbol,
                )
            ]
        if rec.status.is_working:
            if delta < 0:
                return [
                    _Finding(
                        FILL_REGRESSION,
                        f"order {rec.client_order_id}: local filled {rec.filled_qty} > broker "
                        f"filled {bo.filled_qty}",
                        rec=rec,
                        bo=bo,
                        symbol=rec.symbol,
                    )
                ]
            if bo.status != rec.status or delta != 0:
                expected_delta[rec.symbol] += rec.side.sign * delta
                return [_Finding(_PROGRESS, rec=rec, bo=bo, symbol=rec.symbol)]
            return []
        # Local order is terminal: the broker must agree.
        if bo.status != rec.status or bo.filled_qty != rec.filled_qty:
            return [
                _Finding(
                    TERMINAL_CONFLICT,
                    f"order {rec.client_order_id}: local {rec.status.value} "
                    f"(filled {rec.filled_qty}) vs broker {bo.status.value} "
                    f"(filled {bo.filled_qty})",
                    rec=rec,
                    bo=bo,
                    symbol=rec.symbol,
                )
            ]
        return []

    def _absent(self, rec: OrderRecord, now: datetime) -> list[_Finding]:
        """A working local order the broker does not have."""
        age_s = (now - parse_iso(rec.created_at)).total_seconds()
        if rec.status in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMIT_UNKNOWN):
            if rec.status is OrderStatus.PENDING_SUBMIT and age_s < INFLIGHT_GRACE_S:
                return []
            if age_s < self.unknown_grace_s:
                return []  # still inside the window where the same alert_id may retry
            return [
                _Finding(
                    UNSENT,
                    f"order {rec.client_order_id} never reached the broker (unconfirmed for "
                    f"{age_s:.0f}s); marked failed and NOT re-sent automatically",
                    rec=rec,
                    symbol=rec.symbol,
                )
            ]
        return [
            _Finding(
                MISSING_AT_BROKER,
                f"order {rec.client_order_id} ({rec.status.value}) has no trace at the broker",
                rec=rec,
                symbol=rec.symbol,
            )
        ]

    # -- apply (writes) ----------------------------------------------------------------------
    def _apply(self, findings: list[_Finding], token: int) -> tuple[list[Mismatch], int] | None:
        """Write the findings; ``None`` means local state moved since ``token`` and nothing was
        written."""
        mismatches: list[Mismatch] = []
        updates = 0
        now = self.clock()
        with self.store.db.transaction():
            if self.store.change_token() != token:
                return None
            for f in findings:
                if f.kind == POSITION_MISMATCH:
                    symbol, broker_qty = _need(f.symbol), _need(f.broker_qty)
                    self.store.set_position(symbol, broker_qty, now)
                elif f.kind == _PROGRESS:
                    self.ledger.apply_broker_order(_need(f.rec), _need(f.bo))
                    updates += 1
                    continue
                elif f.kind == LOST_RESPONSE:
                    self.ledger.apply_broker_order(_need(f.rec), _need(f.bo))
                elif f.kind in (FILL_REGRESSION, TERMINAL_CONFLICT):
                    self.ledger.apply_broker_order(_need(f.rec), _need(f.bo), adjust_position=False)
                elif f.kind == UNKNOWN_BROKER_ORDER:
                    self._adopt(_need(f.bo), now)
                elif f.kind == MISSING_AT_BROKER:
                    self.store.update_order(
                        _need(f.rec).client_order_id, status=OrderStatus.MISSING, now=now
                    )
                elif f.kind == UNSENT:
                    rec = _need(f.rec)
                    self.store.update_order(
                        rec.client_order_id,
                        status=OrderStatus.FAILED,
                        now=now,
                        reject_reason="never_reached_broker",
                    )
                    if rec.alert_id:
                        self.store.set_alert_state(
                            rec.alert_id, AlertState.FAILED, now, reason="never_reached_broker"
                        )
                mismatches.append(
                    Mismatch(
                        f.kind,
                        f.detail,
                        symbol=f.symbol,
                        client_order_id=f.rec.client_order_id if f.rec else None,
                    )
                )
        return mismatches, updates

    def _adopt(self, bo: BrokerOrder, now: datetime) -> None:
        """Record a broker order we did not create so its exposure is visible to the risk gate."""
        self.store.insert_order(
            OrderRecord(
                client_order_id=bo.client_order_id or f"ext-{bo.broker_order_id}",
                alert_id=None,
                broker_order_id=bo.broker_order_id,
                symbol=bo.symbol,
                side=bo.side,
                qty=bo.qty,
                order_type=bo.order_type,
                limit_price=bo.limit_price,
                status=bo.status,
                # Baseline = what the broker already filled; the position diff sets the position.
                filled_qty=bo.filled_qty,
                avg_fill_price=bo.avg_fill_price,
                source="external",
                created_at=iso(now),
                updated_at=iso(now),
            )
        )
