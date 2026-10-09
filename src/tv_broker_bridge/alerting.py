"""Operator alerts.

The reconciler calls ``AlertSink.send`` for every mismatch. The default sink
writes an ERROR-level structured log line (event ``alert``), which any log
shipper can route to paging. Swap in your own sink (email, chat webhook, ...)
by implementing the one-method protocol.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from tv_broker_bridge.logging_utils import log_event


class AlertSink(Protocol):
    def send(self, kind: str, message: str, **details: Any) -> None: ...


class LogAlertSink:
    def send(self, kind: str, message: str, **details: Any) -> None:
        log_event("alert", level=logging.ERROR, kind=kind, message=message, **details)
