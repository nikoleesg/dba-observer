"""One JSON line per adapter call, to stdout.

Records the tool, its validated arguments (identifiers and limits only; no
secret ever reaches an argument), the outcome and the duration. Never the
result, which may hold metadata the caller is entitled to but a log is not.
"""

from __future__ import annotations

import json
import sys
import threading
import time

_lock = threading.Lock()
_state = {"writes": 0, "last_write_ok": None, "last_write_at": None}


def record(event: dict) -> None:
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "audit", **event}, default=str)
    with _lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            _state["writes"] += 1
            _state["last_write_ok"] = True
        except Exception:  # noqa: BLE001
            _state["last_write_ok"] = False
        _state["last_write_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def status() -> dict:
    with _lock:
        return {"sink": "container stdout, JSON lines", **_state}
