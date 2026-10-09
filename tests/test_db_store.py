"""Transactions and the repository layer: the atomicity the pipeline relies on."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tv_broker_bridge.db import Database
from tv_broker_bridge.models import OrderRecord, OrderStatus, OrderType, Side
from tv_broker_bridge.store import AlertState, Store, iso

NOW = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
D = Decimal


def add_alert(store: Store, alert_id: str = "a1") -> None:
    store.insert_alert(
        alert_id=alert_id,
        state=AlertState.ACCEPTED,
        payload_hash="h",
        symbol="AAPL",
        side=Side.BUY,
        qty=D(1),
        order_type=OrderType.MARKET,
        limit_price=None,
        reason=None,
        now=NOW,
    )


def make_order(
    alert_id: str | None, cid: str = "c1", status: OrderStatus = OrderStatus.ACCEPTED
) -> OrderRecord:
    return OrderRecord(
        client_order_id=cid,
        alert_id=alert_id,
        symbol="AAPL",
        side=Side.BUY,
        qty=D(10),
        order_type=OrderType.MARKET,
        status=status,
        created_at=iso(NOW),
        updated_at=iso(NOW),
    )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


class TestTransactions:
    def test_commit_makes_all_writes_visible(self, db: Database) -> None:
        store = Store(db)
        with db.transaction():
            add_alert(store)
            store.insert_order(make_order("a1"))
        assert store.get_alert("a1") is not None
        assert store.get_order("c1") is not None

    def test_an_exception_rolls_back_every_write_in_the_transaction(self, db: Database) -> None:
        store = Store(db)

        def crash_midway() -> None:
            with db.transaction():
                add_alert(store)  # claim the alert_id...
                store.insert_order(make_order("a1"))  # ...and reserve the order...
                raise RuntimeError("crash before commit")  # ...then die

        with pytest.raises(RuntimeError):
            crash_midway()
        # Nothing survives: no half-claimed alert that would wrongly dedupe a retry.
        assert store.get_alert("a1") is None
        assert store.get_order("c1") is None
        add_alert(store)  # and the database is still usable

    def test_nested_transactions_commit_only_at_the_outermost_level(self, db: Database) -> None:
        store = Store(db)
        with db.transaction():
            with db.transaction():
                add_alert(store)
            # inner block finished, but nothing is committed yet: a second connection can't see it
            other = Database(db.path)
            try:
                assert Store(other).get_alert("a1") is None
            finally:
                other.close()
        after = Database(db.path)
        try:
            assert Store(after).get_alert("a1") is not None  # committed once the outer block exits
        finally:
            after.close()

    def test_failure_in_a_nested_block_rolls_back_the_whole_thing(self, db: Database) -> None:
        store = Store(db)

        def fail_in_inner_block() -> None:
            with db.transaction():
                add_alert(store, "outer")
                with db.transaction():
                    add_alert(store, "inner")
                    raise ValueError("inner")

        with pytest.raises(ValueError, match="inner"):
            fail_in_inner_block()
        assert store.get_alert("outer") is None
        assert store.get_alert("inner") is None

    def test_file_databases_use_wal_for_cross_process_readers(self, db: Database) -> None:
        row = db.query_one("PRAGMA journal_mode")
        assert row is not None
        assert row[0] == "wal"

    def test_the_schema_can_be_opened_repeatedly(self, tmp_path: Path) -> None:
        for _ in range(3):
            Database(tmp_path / "again.db").close()


class TestAlertIdUniquenessAcrossConnections:
    """The CLI and the server are separate connections (processes) on one file."""

    def test_the_second_claim_of_an_alert_id_fails_at_the_database(self, tmp_path: Path) -> None:
        first, second = Database(tmp_path / "x.db"), Database(tmp_path / "x.db")
        try:
            add_alert(Store(first))
            with pytest.raises(sqlite3.IntegrityError):
                add_alert(Store(second))
        finally:
            first.close()
            second.close()

    def test_begin_immediate_serialises_writers_across_connections(self, tmp_path: Path) -> None:
        path = tmp_path / "y.db"
        first, second = Database(path), Database(path)
        entered_second = threading.Event()
        order: list[str] = []

        def second_writer() -> None:
            with second.transaction():  # blocks (busy timeout) until `first` commits
                order.append("second")
                entered_second.set()

        try:
            with first.transaction():
                thread = threading.Thread(target=second_writer)
                thread.start()
                assert not entered_second.wait(timeout=0.3)  # still blocked while we hold the lock
                order.append("first")
            thread.join(timeout=5)
            assert order == ["first", "second"]
        finally:
            first.close()
            second.close()

    def test_foreign_keys_are_enforced(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            Store(db).insert_order(make_order("no-such-alert"))


class TestStoreQueries:
    def test_lookups_by_each_key(self, db: Database) -> None:
        store = Store(db)
        add_alert(store)
        order = make_order("a1")
        store.insert_order(order)
        store.update_order("c1", status=OrderStatus.ACCEPTED, now=NOW, broker_order_id="b-9")
        assert store.get_order_by_alert("a1") is not None
        assert store.get_order_by_broker_id("b-9") is not None
        assert store.get_order_by_broker_id("nope") is None
        assert store.get_order("nope") is None
        assert store.get_alert("nope") is None

    def test_update_leaves_unspecified_fields_alone(self, db: Database) -> None:
        store = Store(db)
        add_alert(store)
        store.insert_order(make_order("a1"))
        store.update_order("c1", status=OrderStatus.PARTIALLY_FILLED, now=NOW, filled_qty=D(4))
        store.update_order("c1", status=OrderStatus.FILLED, now=NOW, avg_fill_price=D("101.5"))
        order = store.get_order("c1")
        assert order is not None
        assert order.filled_qty == D(4)  # untouched by the second update
        assert order.avg_fill_price == D("101.5")

    def test_list_orders_filters_orders_and_limits(self, db: Database) -> None:
        store = Store(db)
        for i, status in enumerate([OrderStatus.ACCEPTED, OrderStatus.FILLED, OrderStatus.FILLED]):
            add_alert(store, f"a{i}")
            store.insert_order(make_order(f"a{i}", f"c{i}", status))
        assert len(store.list_orders()) == 3
        assert len(store.list_orders([OrderStatus.FILLED])) == 2
        assert store.list_orders([]) == []
        assert len(store.list_orders(limit=1)) == 1

    def test_working_exposure_is_signed_and_excludes_the_given_alert(self, db: Database) -> None:
        store = Store(db)
        for alert_id, side, qty in [
            ("a1", Side.BUY, 10),
            ("a2", Side.SELL, 3),
            ("a3", Side.BUY, 100),
        ]:
            add_alert(store, alert_id)
            order = make_order(alert_id, f"c-{alert_id}", OrderStatus.ACCEPTED)
            store.insert_order(replace(order, side=side, qty=D(qty)))
        store.update_order("c-a3", status=OrderStatus.FILLED, now=NOW, filled_qty=D(100))
        assert store.working_exposure("AAPL") == D(7)  # +10 -3; a3 is finished
        assert store.working_exposure("AAPL", exclude_alert_id="a1") == D(-3)
        assert store.working_exposure("MSFT") == D(0)

    def test_positions_roundtrip_with_exact_decimals(self, db: Database) -> None:
        store = Store(db)
        assert store.get_position("AAPL") == D(0)
        store.set_position("AAPL", D("10.5"), NOW)
        assert store.add_to_position("AAPL", D("-0.25"), NOW) == D("10.25")
        assert store.list_positions() == {"AAPL": D("10.25")}

    def test_kv_roundtrip(self, db: Database) -> None:
        store = Store(db)
        assert store.get_kv("k") is None
        store.set_kv("k", "v1")
        store.set_kv("k", "v2")
        assert store.get_kv("k") == "v2"
