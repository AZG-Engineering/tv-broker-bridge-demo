"""Repository layer: typed reads and writes over :class:`Database`."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from tv_broker_bridge.db import Database
from tv_broker_bridge.models import OrderRecord, OrderStatus, OrderType, Side


class AlertState(StrEnum):
    """Where an alert_id stands. Anything except SUBMIT_UNKNOWN is final for dedupe."""

    ACCEPTED = "accepted"  # passed risk; an order row exists and tracks the rest
    REJECTED_RISK = "rejected_risk"
    REJECTED_BROKER = "rejected_broker"
    SUBMIT_UNKNOWN = "submit_unknown"  # delivery outcome unknown; same alert_id may retry
    FAILED = "failed"  # delivery abandoned; nothing reached the broker


@dataclass(frozen=True, slots=True)
class AlertRow:
    alert_id: str
    state: AlertState
    payload_hash: str
    symbol: str
    side: Side
    qty: Decimal
    order_type: OrderType
    limit_price: Decimal | None
    reason: str | None
    received_at: str
    updated_at: str


def iso(dt: datetime) -> str:
    """Canonical timestamp: UTC, fixed width, so string order equals time order."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _order_from_row(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        client_order_id=row["client_order_id"],
        alert_id=row["alert_id"],
        broker_order_id=row["broker_order_id"],
        symbol=row["symbol"],
        side=Side(row["side"]),
        qty=Decimal(row["qty"]),
        order_type=OrderType(row["order_type"]),
        limit_price=_dec(row["limit_price"]),
        status=OrderStatus(row["status"]),
        filled_qty=Decimal(row["filled_qty"]),
        avg_fill_price=_dec(row["avg_fill_price"]),
        reject_reason=row["reject_reason"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _alert_from_row(row: sqlite3.Row) -> AlertRow:
    return AlertRow(
        alert_id=row["alert_id"],
        state=AlertState(row["state"]),
        payload_hash=row["payload_hash"],
        symbol=row["symbol"],
        side=Side(row["side"]),
        qty=Decimal(row["qty"]),
        order_type=OrderType(row["order_type"]),
        limit_price=_dec(row["limit_price"]),
        reason=row["reason"],
        received_at=row["received_at"],
        updated_at=row["updated_at"],
    )


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._changes = 0
        self._changes_lock = threading.Lock()

    def change_token(self) -> int:
        """Counts every write to orders or positions made through this Store.

        The reconciler reads it before observing local state and again, inside the apply
        transaction, before writing: if it moved, something else wrote in between and the
        pass's picture of local state is stale.
        """
        with self._changes_lock:
            return self._changes

    def _bump(self) -> None:
        with self._changes_lock:
            self._changes += 1

    # -- alerts ------------------------------------------------------------------------------
    def get_alert(self, alert_id: str) -> AlertRow | None:
        row = self.db.query_one("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,))
        return None if row is None else _alert_from_row(row)

    def insert_alert(
        self,
        *,
        alert_id: str,
        state: AlertState,
        payload_hash: str,
        symbol: str,
        side: Side,
        qty: Decimal,
        order_type: OrderType,
        limit_price: Decimal | None,
        reason: str | None,
        now: datetime,
    ) -> None:
        """Insert a new alert. Raises ``sqlite3.IntegrityError`` if alert_id already exists."""
        self.db.execute(
            "INSERT INTO alerts (alert_id, state, payload_hash, symbol, side, qty, order_type,"
            " limit_price, reason, received_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                alert_id,
                state.value,
                payload_hash,
                symbol,
                side.value,
                str(qty),
                order_type.value,
                None if limit_price is None else str(limit_price),
                reason,
                iso(now),
                iso(now),
            ),
        )

    def set_alert_state(
        self, alert_id: str, state: AlertState, now: datetime, reason: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE alerts SET state = ?, reason = COALESCE(?, reason), updated_at = ?"
            " WHERE alert_id = ?",
            (state.value, reason, iso(now), alert_id),
        )

    # -- orders ------------------------------------------------------------------------------
    def insert_order(self, rec: OrderRecord) -> None:
        self._bump()
        self.db.execute(
            "INSERT INTO orders (client_order_id, alert_id, broker_order_id, symbol, side, qty,"
            " order_type, limit_price, status, filled_qty, avg_fill_price, reject_reason, source,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.client_order_id,
                rec.alert_id,
                rec.broker_order_id,
                rec.symbol,
                rec.side.value,
                str(rec.qty),
                rec.order_type.value,
                None if rec.limit_price is None else str(rec.limit_price),
                rec.status.value,
                str(rec.filled_qty),
                None if rec.avg_fill_price is None else str(rec.avg_fill_price),
                rec.reject_reason,
                rec.source,
                rec.created_at,
                rec.updated_at,
            ),
        )

    def get_order(self, client_order_id: str) -> OrderRecord | None:
        row = self.db.query_one(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        )
        return None if row is None else _order_from_row(row)

    def get_order_by_alert(self, alert_id: str) -> OrderRecord | None:
        row = self.db.query_one("SELECT * FROM orders WHERE alert_id = ?", (alert_id,))
        return None if row is None else _order_from_row(row)

    def get_order_by_broker_id(self, broker_order_id: str) -> OrderRecord | None:
        row = self.db.query_one(
            "SELECT * FROM orders WHERE broker_order_id = ?", (broker_order_id,)
        )
        return None if row is None else _order_from_row(row)

    def update_order(
        self,
        client_order_id: str,
        *,
        status: OrderStatus,
        now: datetime,
        broker_order_id: str | None = None,
        filled_qty: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        reject_reason: str | None = None,
    ) -> None:
        """Update mutable order fields; ``None`` leaves the stored value unchanged."""
        self._bump()
        self.db.execute(
            "UPDATE orders SET status = ?,"
            " broker_order_id = COALESCE(?, broker_order_id),"
            " filled_qty = COALESCE(?, filled_qty),"
            " avg_fill_price = COALESCE(?, avg_fill_price),"
            " reject_reason = COALESCE(?, reject_reason),"
            " updated_at = ? WHERE client_order_id = ?",
            (
                status.value,
                broker_order_id,
                None if filled_qty is None else str(filled_qty),
                None if avg_fill_price is None else str(avg_fill_price),
                reject_reason,
                iso(now),
                client_order_id,
            ),
        )

    def list_orders(
        self, statuses: Iterable[OrderStatus] | None = None, limit: int | None = None
    ) -> list[OrderRecord]:
        sql = "SELECT * FROM orders"
        params: list[object] = []
        if statuses is not None:
            values = [s.value for s in statuses]
            if not values:
                return []
            sql += f" WHERE status IN ({','.join('?' * len(values))})"
            params.extend(values)
        sql += " ORDER BY created_at DESC, client_order_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_order_from_row(r) for r in self.db.query_all(sql, params)]

    def count_orders_created_between(
        self, start: datetime, end: datetime, exclude_alert_id: str | None = None
    ) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM orders WHERE source = 'bridge'"
            " AND created_at >= ? AND created_at < ?"
            " AND (alert_id IS NULL OR alert_id != ?)",
            (iso(start), iso(end), exclude_alert_id or ""),
        )
        return int(row["n"]) if row else 0

    def working_exposure(self, symbol: str, exclude_alert_id: str | None = None) -> Decimal:
        """Signed quantity still to be filled by working orders on ``symbol``."""
        working = [s for s in OrderStatus if s.is_working]
        rows = self.db.query_all(
            "SELECT * FROM orders WHERE symbol = ? AND status IN "  # noqa: S608 - placeholders only
            f"({','.join('?' * len(working))}) AND (alert_id IS NULL OR alert_id != ?)",
            (symbol, *[s.value for s in working], exclude_alert_id or ""),
        )
        return sum((_order_from_row(r).signed_remaining for r in rows), Decimal(0))

    # -- positions ---------------------------------------------------------------------------
    def get_position(self, symbol: str) -> Decimal:
        row = self.db.query_one("SELECT qty FROM positions WHERE symbol = ?", (symbol,))
        return Decimal(row["qty"]) if row else Decimal(0)

    def set_position(self, symbol: str, qty: Decimal, now: datetime) -> None:
        self._bump()
        self.db.execute(
            "INSERT INTO positions (symbol, qty, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(symbol) DO UPDATE SET"
            " qty = excluded.qty, updated_at = excluded.updated_at",
            (symbol, str(qty), iso(now)),
        )

    def add_to_position(self, symbol: str, delta: Decimal, now: datetime) -> Decimal:
        with self.db.transaction():
            new_qty = self.get_position(symbol) + delta
            self.set_position(symbol, new_qty, now)
        return new_qty

    def list_positions(self) -> dict[str, Decimal]:
        rows = self.db.query_all("SELECT symbol, qty FROM positions ORDER BY symbol")
        return {r["symbol"]: Decimal(r["qty"]) for r in rows}

    # -- key/value (kill switch state lives here so it survives restarts) ---------------------
    def get_kv(self, key: str) -> str | None:
        row = self.db.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    def set_kv(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
