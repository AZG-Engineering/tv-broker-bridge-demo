"""Structured JSON logging.

Every decision the bridge makes is emitted as exactly one JSON object per line
with a stable ``event`` name, so the log doubles as an audit trail::

    {"ts": "...", "level": "INFO", "event": "deduped", "alert_id": "...", ...}

Fields whose names look like credentials are redacted before they reach any
handler, as a second line of defence behind "never pass secrets to the logger".
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

LOGGER_NAME = "tv_broker_bridge"
_logger = logging.getLogger(LOGGER_NAME)

_SENSITIVE = re.compile(
    r"secret|password|passwd|token|api[_-]?key|authorization|apca", re.IGNORECASE
)
_REDACTED = "***redacted***"
_RESERVED = {"fields"}  # "event" cannot clash: Python rejects the duplicate argument


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def redact(value: Any) -> Any:
    """Recursively replace values stored under credential-looking keys."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _SENSITIVE.search(str(k)) else redact(v)) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(redact(fields))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=_json_default, separators=(",", ":"))


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured decision/event record."""
    clash = _RESERVED.intersection(fields)
    if clash:
        raise ValueError(f"reserved log field(s): {sorted(clash)}")
    _logger.log(level, event, extra={"event": event, "fields": fields})


def configure_logging(level: str = "INFO", log_file: str | Path | None = None) -> None:
    """Install JSON handlers on the package logger (idempotent)."""
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        handler.close()  # don't leak the previous file handle when reconfiguring
    formatter = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3)
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        _logger.addHandler(handler)
    _logger.setLevel(level.upper())
    _logger.propagate = False
