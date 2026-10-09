"""OrderLedger: the one place broker reports turn into local state.

Fills discovered by the submit path, the reconciler and the kill switch all go
through ``apply_broker_order``, so the position arithmetic lives in exactly one
function. It is idempotent: applying the same broker report twice changes
nothing the second time, because fills are applied as a delta against the
quantity already recorded.
"""

from __future__ import annotations

import logging

from tv_broker_bridge.clock import Clock
from tv_broker_bridge.logging_utils import log_event
from tv_broker_bridge.models import BrokerOrder, OrderRecord, OrderStatus
from tv_broker_bridge.store import AlertState, Store


class OrderLedger:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def apply_broker_order(
        self, rec: OrderRecord, bo: BrokerOrder, *, adjust_position: bool = True
    ) -> OrderRecord:
        """Copy the broker's view of ``bo`` onto ``rec`` and book any new fills.

        ``adjust_position=False`` records the broker's filled quantity without touching the
        position; the reconciler uses it when it is about to set the position from the
        broker's own position report anyway.
        """
        now = self.clock()
        with self.store.db.transaction():
            current = self.store.get_order(rec.client_order_id) or rec
            delta = bo.filled_qty - current.filled_qty
            self.store.update_order(
                current.client_order_id,
                status=bo.status,
                now=now,
                broker_order_id=bo.broker_order_id,
                filled_qty=bo.filled_qty,
                avg_fill_price=bo.avg_fill_price,
                reject_reason=bo.reject_reason,
            )
            if delta > 0 and adjust_position:
                self.store.add_to_position(current.symbol, current.side.sign * delta, now)
            if current.alert_id and current.status is OrderStatus.SUBMIT_UNKNOWN:
                self.store.set_alert_state(
                    current.alert_id,
                    AlertState.REJECTED_BROKER
                    if bo.status is OrderStatus.REJECTED
                    else AlertState.ACCEPTED,
                    now,
                )
            updated = self.store.get_order(current.client_order_id)
        assert updated is not None  # noqa: S101 - we just wrote it

        if delta > 0:
            log_event(
                "filled",
                alert_id=updated.alert_id,
                client_order_id=updated.client_order_id,
                broker_order_id=updated.broker_order_id,
                symbol=updated.symbol,
                side=updated.side.value,
                fill_qty=delta,
                filled_qty=updated.filled_qty,
                remaining_qty=updated.remaining_qty,
                avg_fill_price=updated.avg_fill_price,
                partial=updated.status is OrderStatus.PARTIALLY_FILLED,
                status=updated.status.value,
            )
        elif delta < 0:
            log_event(
                "fill_regression",
                level=logging.ERROR,
                client_order_id=updated.client_order_id,
                recorded=current.filled_qty,
                broker=bo.filled_qty,
            )
        if updated.status is OrderStatus.CANCELED and current.status is not OrderStatus.CANCELED:
            log_event(
                "order_canceled",
                alert_id=updated.alert_id,
                client_order_id=updated.client_order_id,
                filled_qty=updated.filled_qty,
            )
        return updated
