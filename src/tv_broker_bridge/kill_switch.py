"""Kill switch.

Engaging it (a) stops the webhook from accepting alerts and (b) cancels every
working order at the broker. State is persisted in SQLite, so:

* it survives a restart (a crashed-and-restarted bridge does not quietly resume);
* the CLI and the HTTP endpoint share it, even from different processes;
* if the broker is unreachable when the switch is thrown, ``cancel_pending``
  stays set and the supervisor retries the cancel until it succeeds.

Releasing is deliberately CLI-only: re-arming should need a human on the host.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.clock import Clock
from tv_broker_bridge.errors import BrokerError
from tv_broker_bridge.logging_utils import log_event
from tv_broker_bridge.store import Store, iso

KV_KEY = "kill_switch"


@dataclass
class KillState:
    engaged: bool = False
    engaged_at: str | None = None
    reason: str | None = None
    source: str | None = None
    cancel_pending: bool = False
    last_cancel_error: str | None = None


@dataclass(frozen=True)
class KillOutcome:
    engaged: bool
    cancel_complete: bool
    canceled_order_ids: list[str] = field(default_factory=list)
    error: str | None = None


class KillSwitch:
    def __init__(
        self,
        store: Store,
        broker: Broker | None,
        clock: Clock,
        after_cancel: Callable[[], object] | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.clock = clock
        self.after_cancel = after_cancel  # e.g. a reconcile pass to sync local order state

    # -- state -------------------------------------------------------------------------------
    def state(self) -> KillState:
        raw = self.store.get_kv(KV_KEY)
        if raw is None:
            return KillState()
        return KillState(**json.loads(raw))

    def _save(self, state: KillState) -> None:
        self.store.set_kv(KV_KEY, json.dumps(asdict(state)))

    def is_engaged(self) -> bool:
        return self.state().engaged

    # -- actions -----------------------------------------------------------------------------
    def engage(self, reason: str, source: str, *, cancel: bool = True) -> KillOutcome:
        """Stop accepting alerts, then cancel all working orders (if ``cancel``)."""
        with self.store.db.transaction():
            state = self.state()
            already = state.engaged
            if not already:
                state.engaged = True
                state.engaged_at = iso(self.clock())
                state.reason = reason
                state.source = source
            state.cancel_pending = True
            self._save(state)
        log_event(
            "kill_switch_engaged",
            level=logging.CRITICAL,
            reason=state.reason,
            source=source,
            already_engaged=already,
        )
        if not cancel or self.broker is None:
            return KillOutcome(engaged=True, cancel_complete=False)
        return self.cancel_working_orders()

    def release(self, source: str) -> bool:
        """Clear the switch. Returns False if it was not engaged."""
        with self.store.db.transaction():
            state = self.state()
            if not state.engaged:
                return False
            self._save(KillState())
        log_event("kill_switch_released", level=logging.WARNING, source=source)
        return True

    def cancel_working_orders(self) -> KillOutcome:
        """Cancel everything at the broker; on failure leave ``cancel_pending`` set for retry."""
        if self.broker is None:
            return KillOutcome(engaged=True, cancel_complete=False, error="no broker available")
        try:
            canceled = self.broker.cancel_all_orders()
        except BrokerError as exc:
            state = self.state()
            state.last_cancel_error = str(exc)
            self._save(state)
            log_event("kill_switch_cancel_failed", level=logging.ERROR, error=str(exc))
            return KillOutcome(engaged=True, cancel_complete=False, error=str(exc))

        state = self.state()
        state.cancel_pending = False
        state.last_cancel_error = None
        self._save(state)
        log_event("kill_switch_orders_canceled", level=logging.WARNING, count=len(canceled))
        if self.after_cancel is not None:
            try:
                self.after_cancel()
            except Exception as exc:  # best-effort sync; the reconciler retries
                log_event("kill_switch_sync_failed", level=logging.ERROR, error=str(exc))
        return KillOutcome(engaged=True, cancel_complete=True, canceled_order_ids=canceled)

    def mark_cancel_pending(self) -> None:
        """Ask the supervisor to (re)run cancel-all, e.g. after a late order slipped through."""
        with self.store.db.transaction():
            state = self.state()
            if state.engaged:
                state.cancel_pending = True
                self._save(state)

    def retry_pending_cancel(self) -> KillOutcome | None:
        """Called by the supervisor loop: finish a cancel that did not complete earlier."""
        state = self.state()
        if state.engaged and state.cancel_pending:
            return self.cancel_working_orders()
        return None
