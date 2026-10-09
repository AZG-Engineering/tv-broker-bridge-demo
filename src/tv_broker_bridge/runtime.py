"""Composition root: builds every component and owns the background loops."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tv_broker_bridge import __version__
from tv_broker_bridge.alerting import AlertSink, LogAlertSink
from tv_broker_bridge.broker.base import Broker
from tv_broker_bridge.clock import Clock, utc_now
from tv_broker_bridge.config import Settings, create_broker
from tv_broker_bridge.db import Database
from tv_broker_bridge.health import RuntimeStatus
from tv_broker_bridge.heartbeat import write_heartbeat
from tv_broker_bridge.kill_switch import KillSwitch
from tv_broker_bridge.ledger import OrderLedger
from tv_broker_bridge.logging_utils import log_event
from tv_broker_bridge.processor import AlertProcessor
from tv_broker_bridge.reconcile import Reconciler
from tv_broker_bridge.risk import RiskConfig, load_risk_config
from tv_broker_bridge.store import Store


class PeriodicWorker(threading.Thread):
    """Run ``fn`` every ``interval_s`` seconds until stopped; an exception never kills the loop."""

    def __init__(self, name: str, interval_s: float, fn: Callable[[], object]) -> None:
        super().__init__(name=name, daemon=True)
        self.interval_s = interval_s
        self.fn = fn
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.fn()
            except Exception as exc:  # the loop must survive anything
                log_event(
                    "loop_error",
                    level=logging.ERROR,
                    loop=self.name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            self.stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)


@dataclass
class Runtime:
    settings: Settings
    db: Database
    store: Store
    broker: Broker
    risk: RiskConfig
    clock: Clock
    status: RuntimeStatus
    kill: KillSwitch
    ledger: OrderLedger
    processor: AlertProcessor
    reconciler: Reconciler
    workers: list[PeriodicWorker] = field(default_factory=list)
    background_reconcile: bool = False

    # -- health ------------------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        now = self.clock()
        kill = self.kill.state()
        problems = self.status.problems(
            now,
            self.settings.reconcile_interval_s,
            background_reconcile=self.background_reconcile,
        )
        if kill.engaged:
            overall = "halted"
        elif problems:
            overall = "degraded"
        else:
            overall = "ok"
        return {
            "status": overall,
            "problems": problems,
            "version": __version__,
            "broker": self.broker.name,
            "kill_switch": {
                "engaged": kill.engaged,
                "engaged_at": kill.engaged_at,
                "cancel_pending": kill.cancel_pending,
            },
            "reconcile": self.status.as_dict(),
            "uptime_s": round((now - self.status.started_at).total_seconds(), 1),
        }

    # -- supervisor tick: finish a pending kill-cancel, then beat ----------------------------
    def supervise_once(self) -> None:
        try:
            self.kill.retry_pending_cancel()
        finally:
            health = self.health()
            write_heartbeat(
                self.settings.heartbeat_path,
                self.clock(),
                status=health["status"],
                problems=health["problems"],
                kill_switch=health["kill_switch"]["engaged"],
                broker=self.broker.name,
                last_reconcile_success=health["reconcile"]["last_success_at"],
                pid=os.getpid(),
                version=__version__,
            )

    # -- lifecycle ---------------------------------------------------------------------------
    def start_background(self) -> None:
        self.background_reconcile = True
        self.workers = [
            PeriodicWorker("heartbeat", self.settings.heartbeat_interval_s, self.supervise_once),
            PeriodicWorker(
                "reconcile", self.settings.reconcile_interval_s, self.reconciler.run_once
            ),
        ]
        for worker in self.workers:
            worker.start()
        log_event(
            "background_started",
            reconcile_interval_s=self.settings.reconcile_interval_s,
            heartbeat_interval_s=self.settings.heartbeat_interval_s,
            heartbeat_path=self.settings.heartbeat_path,
        )

    def stop_background(self) -> None:
        for worker in self.workers:
            worker.stop()
        self.workers = []
        self.background_reconcile = False

    def close(self) -> None:
        self.stop_background()
        self.broker.close()
        self.db.close()


def build_runtime(
    settings: Settings,
    *,
    broker: Broker | None = None,
    risk: RiskConfig | None = None,
    clock: Clock = utc_now,
    alert_sink: AlertSink | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Runtime:
    """Wire everything together. Config problems raise ``ConfigError`` here, before serving."""
    risk_config = risk or load_risk_config(settings.risk_config_path)
    active_broker = broker or create_broker(settings)
    db = Database(settings.db_path)
    store = Store(db)
    status = RuntimeStatus(clock())
    ledger = OrderLedger(store, clock)
    reconciler = Reconciler(
        store=store,
        broker=active_broker,
        ledger=ledger,
        alerts=alert_sink or LogAlertSink(),
        clock=clock,
        status=status,
        unknown_grace_s=settings.unknown_grace_s,
    )
    kill = KillSwitch(store, active_broker, clock, after_cancel=reconciler.run_once)
    processor = AlertProcessor(
        store=store,
        broker=active_broker,
        risk=risk_config,
        kill=kill,
        ledger=ledger,
        clock=clock,
        attempts=settings.submit_attempts,
        backoff_s=settings.submit_backoff_s,
        sleep=sleep,
    )
    return Runtime(
        settings=settings,
        db=db,
        store=store,
        broker=active_broker,
        risk=risk_config,
        clock=clock,
        status=status,
        kill=kill,
        ledger=ledger,
        processor=processor,
        reconciler=reconciler,
    )
