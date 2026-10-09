"""Runtime health bookkeeping shared by the reconciler, supervisor and /health."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEGRADED_AFTER_FAILURES = 3


@dataclass
class ReconcileStatus:
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    last_mismatch_count: int = 0
    total_mismatches: int = 0
    passes: int = 0


class RuntimeStatus:
    """Thread-safe holder for reconciliation health."""

    def __init__(self, started_at: datetime) -> None:
        self._lock = threading.Lock()
        self.started_at = started_at
        self._rec = ReconcileStatus()

    def record_success(self, now: datetime, mismatches: int) -> int:
        """Record a completed pass; returns how many failures preceded it."""
        with self._lock:
            previous_failures = self._rec.consecutive_failures
            self._rec.last_attempt_at = now
            self._rec.last_success_at = now
            self._rec.last_error = None
            self._rec.consecutive_failures = 0
            self._rec.last_mismatch_count = mismatches
            self._rec.total_mismatches += mismatches
            self._rec.passes += 1
            return previous_failures

    def record_failure(self, now: datetime, error: str) -> int:
        with self._lock:
            self._rec.last_attempt_at = now
            self._rec.last_error = error
            self._rec.consecutive_failures += 1
            return self._rec.consecutive_failures

    def reconcile(self) -> ReconcileStatus:
        with self._lock:
            return ReconcileStatus(**vars(self._rec))

    def problems(
        self, now: datetime, reconcile_interval_s: float, *, background_reconcile: bool
    ) -> list[str]:
        """Reasons the bridge should be considered degraded (empty list = healthy)."""
        rec = self.reconcile()
        problems: list[str] = []
        if rec.consecutive_failures >= DEGRADED_AFTER_FAILURES:
            problems.append("reconcile_failing")
        if rec.last_mismatch_count > 0:
            problems.append("reconcile_mismatch")
        if background_reconcile:
            reference = rec.last_success_at or self.started_at
            if (now - reference).total_seconds() > 3 * reconcile_interval_s + 5:
                problems.append("reconcile_stale")
        return problems

    def as_dict(self) -> dict[str, Any]:
        rec = self.reconcile()
        return {
            "last_attempt_at": rec.last_attempt_at.isoformat() if rec.last_attempt_at else None,
            "last_success_at": rec.last_success_at.isoformat() if rec.last_success_at else None,
            "last_error": rec.last_error,
            "consecutive_failures": rec.consecutive_failures,
            "last_mismatch_count": rec.last_mismatch_count,
            "total_mismatches": rec.total_mismatches,
            "passes": rec.passes,
        }
