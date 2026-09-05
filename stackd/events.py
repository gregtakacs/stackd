"""A tiny in-memory ring buffer of scheduler events for the web UI.

`serve.py` already `print()`s every reconcile action (activate / evict / crash-
restart / idle-evict / image swap / config reload). Those same call sites also
`record()` here so `GET /events` can show "what the daemon just did" without a
log scrape. Process-local, bounded, lost on restart — that's fine, it's a
live feed, not an audit log.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_MAX = 1000
_buf: deque[dict] = deque(maxlen=_MAX)
_lock = threading.Lock()
_seq = 0


def record(kind: str, stack: str = "", detail: str = "", source: str = "") -> None:
    """Append one event. `kind` is the reconcile action (spawn/stop/restart/
    idle-evict/image-swap/reload/...); `source` is where it came from
    (control/reload/tick/boot/image)."""
    global _seq
    with _lock:
        _seq += 1
        _buf.append({
            "seq": _seq,
            "ts": time.time(),
            "kind": kind,
            "stack": stack or "",
            "detail": detail or "",
            "source": source or "",
        })


def record_evt(evt, source: str = "") -> None:
    """Record a reconciler `_Evt` (has .action / .stack / .detail)."""
    record(getattr(evt, "action", "event"), getattr(evt, "stack", ""),
           getattr(evt, "detail", ""), source)


def snapshot(since_seq: int | None = None) -> dict:
    """All buffered events (oldest first), or just those after `since_seq`."""
    with _lock:
        rows = list(_buf)
        last = _seq
    if since_seq is not None:
        rows = [r for r in rows if r["seq"] > since_seq]
    return {"events": rows, "last_seq": last, "now": time.time()}
