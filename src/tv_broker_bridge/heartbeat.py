"""Heartbeat file for out-of-process liveness checks.

The server rewrites a small JSON file every few seconds (atomically, via
temp-file + rename, so a reader never sees a half-written file). Another
process (cron, systemd, a container probe, ``tv-bridge check-heartbeat``) can
treat "file older than N seconds" as "the bridge is wedged or dead".
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def write_heartbeat(path: str | Path, now: datetime, **fields: Any) -> None:
    """Atomically write the heartbeat file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": now.isoformat(timespec="seconds"), "epoch": now.timestamp(), **fields}
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_heartbeat(path: str | Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_heartbeat(path: str | Path, max_age_s: float, now: datetime) -> tuple[bool, str]:
    """Return ``(healthy, message)``: healthy iff the file exists and is fresh enough."""
    data = read_heartbeat(path)
    if data is None:
        return False, f"no readable heartbeat at {path}"
    try:
        age = now.timestamp() - float(data["epoch"])
    except (KeyError, TypeError, ValueError):
        return False, f"heartbeat at {path} has no valid 'epoch'"
    if age > max_age_s:
        return False, f"heartbeat is stale: {age:.1f}s old (limit {max_age_s:.1f}s)"
    return True, f"heartbeat ok: {age:.1f}s old, status={data.get('status', 'unknown')}"
