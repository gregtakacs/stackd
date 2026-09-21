"""Built-in ComfyUI scratch janitor — was the `comfyui-cleaner` Alpine container
running comfyui-cleaner.sh. Runs as one daemon thread inside `stackd serve`,
toggled live via POST /cleaner/on|off.

Two jobs, every `interval_s`:
  * sweep: delete files under <scratch>/{output,input,temp} older than
    `file_ttl_min`. Long skipped while ComfyUI's /queue has anything RUNNING:
    the image MCP's fetch happens only after the WHOLE prompt completes, and
    early-written temp artifacts (the edit_image mask PreviewImage) are minutes
    to hours older than that by then -- a TTL sweep during a long Klein masked
    edit deletes files the in-flight fetch is about to request (2026-09-20:
    sank two fully rendered ~6-min MCP edits over their debug preview, 404).
    A busy queue is never starved of cleanup for long: prompts finish, the
    next idle sweep catches up.
  * prune: drop stale ComfyUI /history entries (it builds its gallery from
    execution history, not a dir scan, so deleting files leaves dangling rows).
    Prune is safe while busy -- it only deletes entries whose files are ALL
    gone, and an in-flight prompt has no history entry yet.

Safety rules carried over verbatim from the shell script:
  * NEVER blanket-clear history ({"clear": true}) — it races the image MCP
    (submit → poll /history/<id> → download), silently timing out a live
    generation. Only delete entries that are safe: no output images at all
    (errored/cancelled) OR every output file already gone from disk.
  * NEVER touch /queue — that would kill in-flight work.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

_SUBDIRS = ("output", "input", "temp")


def sweep_files(scratch_dir: str, ttl_min: float) -> int:
    """Unlink regular files under <scratch_dir>/{output,input,temp} whose mtime is
    older than ttl_min minutes. Returns the count removed. Directories untouched."""
    cutoff = time.time() - ttl_min * 60.0
    removed = 0
    for sub in _SUBDIRS:
        base = os.path.join(scratch_dir, sub)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for name in files:
                p = os.path.join(root, name)
                try:
                    if os.path.isfile(p) and os.stat(p).st_mtime < cutoff:
                        os.unlink(p)
                        removed += 1
                except OSError:
                    pass
    return removed


def _history(endpoint: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/history")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def queue_busy(endpoint: str, timeout: float = 5.0) -> bool:
    """True if ComfyUI has at least one prompt in queue_running (GET /queue,
    strictly read-only -- the never-touch-/queue rule is about not mutating it).
    Unreadable queue -> False: a flaky peek must not permanently disable the
    janitor, and the raised TTL keeps the race window vanishingly small anyway."""
    try:
        req = urllib.request.Request(f"{endpoint.rstrip('/')}/queue")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            q = json.loads(r.read() or b"{}")
        return bool(q.get("queue_running"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _delete_history(endpoint: str, ids: list[str], timeout: float = 10.0) -> None:
    body = json.dumps({"delete": ids}).encode()
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/history", data=body, method="POST",
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout):
        pass


def prune_history(endpoint: str, scratch_dir: str) -> int:
    """Delete history entries that are safe to drop (see module docstring).
    Returns the count deleted. Best-effort — any error is swallowed by the caller."""
    hist = _history(endpoint)
    stale: list[str] = []
    for pid, entry in hist.items():
        imgs = [
            img
            for node in (entry.get("outputs") or {}).values()
            for img in (node.get("images") or [])
        ]
        if not imgs:
            stale.append(pid)  # errored / cancelled — nothing to protect
            continue
        alive = False
        for img in imgs:
            sub = img.get("type") or "output"
            rel = img.get("subfolder") or ""
            path = os.path.join(scratch_dir, sub, rel, img.get("filename", ""))
            if os.path.exists(path):
                alive = True
                break
        if not alive:
            stale.append(pid)
    if stale:
        _delete_history(endpoint, stale)
    return len(stale)


class Cleaner:
    """Owns the enable flag + counters; `run_forever` is the thread body."""

    def __init__(self, scratch_dir: str, *, file_ttl_min: int = 10,
                 interval_s: int = 90, enabled: bool = True) -> None:
        self.scratch_dir = scratch_dir
        self.file_ttl_min = file_ttl_min
        self.interval_s = interval_s
        self._enabled = threading.Event()
        if enabled:
            self._enabled.set()
        self.swept_total = 0
        self.pruned_total = 0
        self.last_run: float | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    def set_enabled(self, on: bool) -> None:
        (self._enabled.set if on else self._enabled.clear)()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_s": self.interval_s,
            "file_ttl_min": self.file_ttl_min,
            "scratch_dir": self.scratch_dir,
            "swept_total": self.swept_total,
            "pruned_total": self.pruned_total,
            "last_run": self.last_run,
        }

    def run_once(self, endpoint: str | None) -> tuple[int, int]:
        # Never sweep while a prompt is running: its early-written temp artifacts
        # (mask previews) are still owed to the fetch that happens at completion.
        busy = bool(endpoint) and queue_busy(endpoint)
        swept = 0 if busy else sweep_files(self.scratch_dir, self.file_ttl_min)
        pruned = 0
        if endpoint:
            try:
                pruned = prune_history(endpoint, self.scratch_dir)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        self.swept_total += swept
        self.pruned_total += pruned
        self.last_run = time.time()
        return swept, pruned

    def run_forever(self, get_endpoint, stop: threading.Event, on_event=None) -> None:
        """`get_endpoint()` -> the resident ComfyUI base URL (or None). `stop` ends
        the loop. `on_event(swept, pruned)` is called after a run that did work."""
        while not stop.wait(self.interval_s):
            if not self.enabled:
                continue
            try:
                swept, pruned = self.run_once(get_endpoint())
                if (swept or pruned) and on_event:
                    on_event(swept, pruned)
            except Exception:  # noqa: BLE001 — a janitor must never sink the daemon
                pass
