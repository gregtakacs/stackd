"""M1: the job model — a small sqlite queue + a worker thread, deliberately self-contained.

Why NOT stackd/store.py's ledger: that is the single most safety-critical file in the
daemon (the cost ledger), it assumes one connection + one external lock, and mounting a
second, GPU-bound subsystem into it raises the blast radius of any toolbox bug from
"renders fail" to "savings numbers are wrong." A toolbox job is meaningless to the ledger.
So this owns its own DB file, its own lock, and its own connection, and works identically
under `stackd serve` and the standalone spike (neither needs the other's schema).

Lifecycle: enqueue(state=queued) -> worker sets running (records the ComfyUI prompt_id so
cancel can interrupt it) -> done(artifact) | error | cancelled. `submit` and `cancel` are
INJECTED so the suite drives the whole state machine with fakes and no GPU, no httpx, no
PIL — and so the real ComfyUI path (comfy_render, below) is one swappable seam.

Persistence is for FINISHED jobs (a page reopened after a restart still sees its result).
The in-flight source+mask blobs are held in memory only; a restart mid-render loses that
job, and reconcile_orphans() marks such rows error with a "resubmit" note rather than
lying that they are still queued. That is the honest failure, not a stall.
"""

from __future__ import annotations

import base64
import inspect as _inspect
import json
import queue
import sqlite3
import threading
import time
import uuid

VALID_STATES = ("queued", "running", "done", "error", "cancelled")


class NoEngine(RuntimeError):
    """No serveable image engine right now — say so, don't silently stall the job."""


class UserNotRegistered(RuntimeError):
    """The caller's email has no Open WebUI key on record, so there is no owner to save
    the artifact under. Refuse rather than save as some other user — the whole point of
    the per-user key (imagegen/openwebui_client) is that a file belongs to who made it."""


class Cancelled(RuntimeError):
    """Raised by an injected submit when its render was interrupted mid-flight."""


def _now() -> float:
    return time.time()


def _accepts_progress(fn) -> bool:
    """True if the injected render seam takes an on_progress keyword.

    Inspected rather than assumed: the queue's contract is (job, source, mask,
    on_prompt_id), smoke_toolbox drives it with plain fakes, and passing an unexpected kwarg
    to a callable that does not take **kwargs raises TypeError that the worker would report
    to the user as a failed render. A progress bar is not worth failing jobs over.
    """
    try:
        params = _inspect.signature(fn).parameters
    except (TypeError, ValueError):        # builtins / C callables: assume it does not
        return False
    p = params.get("on_progress")
    if p is None:
        return any(v.kind is _inspect.Parameter.VAR_KEYWORD for v in params.values())
    return p.kind in (_inspect.Parameter.KEYWORD_ONLY, _inspect.Parameter.POSITIONAL_OR_KEYWORD)



class JobStore:
    """SQLite-backed job rows. One connection, one lock, autocommit + WAL exactly like
    stackd/store.py — safe to call from handler threads and the worker concurrently."""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, timeout=10, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("PRAGMA journal_mode=WAL;")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                kind TEXT,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                working_w INTEGER,
                working_h INTEGER,
                spec_json TEXT,
                mask_json TEXT,
                prompt_id TEXT,
                engine_base TEXT,
                artifact_b64 TEXT,
                artifact_type TEXT,
                error TEXT,
                note TEXT,
                crop_json TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_jobs_email ON jobs(email, created_at);
            """
        )
        # CREATE TABLE IF NOT EXISTS does not touch a table that already exists, so a
        # column added here is invisible to every DB written by an earlier deploy until it
        # is ALTERed in. Idempotent and cheap; a pre-existing jobs.db keeps its rows and
        # gains the column NULL-filled (a job that never cropped has no provenance, which
        # is exactly what NULL should mean here).
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col in ("crop_json", "progress_json"):
            if col not in cols:
                # progress_json is IN-FLIGHT state (elapsed/stage), the one piece of live
                # UI data that must survive a page reload; crop_json is terminal provenance.
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
        self._lock = threading.Lock()

    def create(self, *, email, kind, w, h, spec, mask_info, job_id=None) -> str:
        jid = job_id or uuid.uuid4().hex[:16]
        t = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO jobs (id,email,kind,state,created_at,updated_at,working_w,"
                "working_h,spec_json,mask_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (jid, email, kind, "queued", t, t, w, h,
                 json.dumps(spec or {}), json.dumps(mask_info or {})),
            )
        return jid

    def set(self, job_id, **fields) -> None:
        allowed = {"state", "prompt_id", "engine_base", "artifact_b64",
                   "artifact_type", "error", "note", "crop_json", "progress_json"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown job field(s): {sorted(bad)}")
        if "state" in fields and fields["state"] not in VALID_STATES:
            raise ValueError(f"bad state {fields['state']!r}")
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [_now(), job_id]
        with self._lock:
            self.conn.execute(f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?", vals)

    def recent_durations(self, w, h, *, limit: int = 8) -> list:
        """Wall-clock durations of the last few COMPLETED renders at this exact working
        size. Feeds the poll route's ETA. Median rather than mean at the call site: one
        cold-start or evicted-model render is an outlier that would double the estimate."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT created_at, updated_at FROM jobs "
                "WHERE state='done' AND working_w=? AND working_h=? "
                "ORDER BY updated_at DESC LIMIT ?", (int(w), int(h), int(limit))).fetchall()
        out = []
        for r in rows:
            try:
                d = float(r[1]) - float(r[0])
            except (TypeError, ValueError):
                continue
            if 0.5 < d < 3600:                     # a junk row is not a calibration point
                out.append(d)
        return out


    def get(self, job_id) -> dict | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def for_email(self, email, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE email=? ORDER BY created_at DESC LIMIT ?",
                (email, limit)).fetchall()
        return [dict(r) for r in rows]

    def prune(self, keep_days: float = 14.0) -> dict:
        """Retention sweep — without it this DB is a slow leak with my name on it.

        Rows are cheap; artifact_b64 is a whole PNG of a finished render, and NOTHING
        ever reads a week-old row again: the job-scope token lives only in the browser
        tab that created the job, so a reopened page cannot poll it. (A reopened embed
        shows results only for its own live session; the durable copies of every
        finished image are the OWU file save and the chat post, not this table.)

        Policy after keep_days: done rows KEEP the row (provenance is ~1 KB) but lose
        their pixels; error/cancelled rows, which no UI ever shows again, go entirely.
        In-flight rows are never touched. keep_days <= 0 disables the sweep.
        """
        if keep_days is None or keep_days <= 0:
            return {"stripped": 0, "deleted": 0}
        cutoff = time.time() - float(keep_days) * 86400.0
        with self._lock:
            cur = self.conn.execute(
                "UPDATE jobs SET artifact_b64=NULL, artifact_type=NULL, "
                "note=CASE WHEN note IS NULL OR note='' THEN '[artifact pruned]' "
                "ELSE note||' [artifact pruned]' END "
                "WHERE state='done' AND artifact_b64 IS NOT NULL "
                "AND updated_at IS NOT NULL AND updated_at<?", (cutoff,))
            stripped = cur.rowcount
            cur2 = self.conn.execute(
                "DELETE FROM jobs WHERE state IN ('error','cancelled') "
                "AND updated_at IS NOT NULL AND updated_at<?", (cutoff,))
            deleted = cur2.rowcount
        if stripped or deleted:
            try:                            # give the freed pages back to the file
                self.conn.execute("VACUUM;")
            except sqlite3.OperationalError:
                pass                          # locked/inside-txn: next sweep retries
        return {"stripped": stripped, "deleted": deleted}

    def reconcile_orphans(self) -> int:
        """On startup, a queued/running row is one the dead worker will never pick up
        again. Mark them error with a resubmit note so the UI shows truth, not a spinner
        that spins forever. Returns how many were swept."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE jobs SET state='error', updated_at=?, "
                "error='the render service restarted before this job finished; please "
                "submit it again' WHERE state IN ('queued','running')", (_now(),))
        return cur.rowcount

    def pending_states(self, job_id) -> tuple:
        """(state, prompt_id, engine_base) for a job, or None if it does not exist. Used
        by JobQueue to decide how to cancel (a queued job is simply dropped; a running one
        needs the ComfyUI interrupt) without a second full-row read."""
        with self._lock:
            r = self.conn.execute(
                "SELECT state, prompt_id, engine_base FROM jobs WHERE id=?", (job_id,)).fetchone()
        return (r["state"], r["prompt_id"], r["engine_base"]) if r else None


class JobQueue:
    """The worker thread + in-flight blob store, wired to a JobStore.

    Why blobs live in memory and not the DB: a source+mask pair is a few MB (the source is
    the RAW photo bytes now — the crop-and-paste path composites against them, so the
    queue is deliberately given what the browser-canvas ceiling did NOT shrink) and is
    only ever needed for the seconds between submit and the ComfyUI upload. Persisting a
    finished job's *result* (the artifact) is worth it — that is what survives a restart
    for a page reopened later — but persisting in-flight inputs buys nothing and drags a
    GPU-bound hot path through a blob write. A restart mid-render therefore loses that
    job; JobStore.reconcile_orphans() marks it error with a resubmit note, which is the
    honest failure rather than a spinner that never stops.

    The two GPU-touching operations are INJECTED, not imported:

        render(job, source, mask, on_prompt_id) -> (artifact_b64, artifact_type)
            Builds the graph, uploads, submits to ComfyUI, waits for the output. MUST
            call on_prompt_id(prompt_id, base) as soon as the prompt is accepted, so the
            row records the id cancel needs — before the (potentially minutes-long) wait.
            Raises NoEngine / Cancelled / UserNotRegistered / TimeoutError / anything.
        cancel(prompt_id, base) -> None
            Best-effort POST /interrupt. Never raises (a failed interrupt is logged).

    Both are pure callables, so smoke_toolbox drives the ENTIRE state machine — queued ->
    running -> done | error | cancelled, the orphan sweep, and cancellation of both a
    queued and a running job — with fakes, no GPU, no httpx, no PIL.
    """

    def __init__(self, store: JobStore, *, render, cancel=None, logger=None,
                 keep_days: float = 14.0):
        self.store = store
        self._render = render
        self._cancel = cancel
        self.log = logger
        self.keep_days = keep_days              # retention: see JobStore.prune
        self._last_prune = 0.0
        self._q: "queue.Queue[str]" = queue.Queue()
        self._blobs: dict[str, tuple] = {}       # job_id -> (source_bytes, mask_bytes)
        self._blobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def enqueue(self, job_id: str, *, source: bytes, mask: bytes) -> None:
        """Stash the in-flight pair (memory only) and wake the worker. The row already
        exists in `queued`; enqueue is what makes it runnable. Keeping the fast DB write
        (in the handler thread) separate from the blob hand-off (worker-owned memory) means
        the handler never enqueues a row it did not just create."""
        with self._blobs_lock:
            self._blobs[job_id] = (source, mask)
        self._q.put(job_id)

    def start(self) -> None:
        if self._thread is not None:
            return
        swept = self.store.reconcile_orphans()
        if swept and self.log:
            self.log.info("toolbox: swept %d orphan job(s) from a prior run", swept)
        self._last_prune = time.time()
        self._prune()                        # retention at boot; then every ~6h idle
        self._thread = threading.Thread(target=self._run, name="toolbox-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._q.put(None)                        # wake a worker parked on get()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def cancel(self, job_id: str) -> str:
        """Mark a job cancelled and, if it is mid-render, interrupt ComfyUI. Returns the
        resulting state. A queued job is dropped from the queue (it is re-checked by
        _process before any GPU work, so this needs no removal race); a running one gets
        the injected cancel(). A finished job is left exactly as it is — cancelling a done
        render would lie about a result the user already has."""
        info = self.store.pending_states(job_id)
        if info is None:
            return "missing"
        state, prompt_id, engine_base = info
        if state in ("done", "error", "cancelled"):
            return state
        with self._blobs_lock:                   # drop the (now-orphaned) in-flight pair
            self._blobs.pop(job_id, None)
        self.store.set(job_id, state="cancelled", error=None)
        if state == "running" and prompt_id and self._cancel is not None:
            try:
                self._cancel(prompt_id, engine_base)
            except Exception as e:  # noqa: BLE001 — a failed interrupt must not un-cancel
                if self.log:
                    self.log.warning("toolbox: cancel %s interrupt failed: %r", job_id, e)
        return "cancelled"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._q.get(timeout=0.5)
            except queue.Empty:
                self._maybe_prune()
                continue
            if job_id is None:
                break
            try:
                self._process(job_id)
            except Exception as e:  # noqa: BLE001 — one bad job must not kill the worker
                if self.log:
                    self.log.exception("toolbox: job %s worker error", job_id)
                try:
                    self.store.set(job_id, state="error",
                                   error=f"worker error: {e.__class__.__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass
        # Anything still queued when we stop belongs to a worker going away; the next
        # start()'s reconcile_orphans() marks them error, not a forever-spinner.

    def _prune(self) -> None:
        try:
            r = self.store.prune(self.keep_days)
            if (r["stripped"] or r["deleted"]) and self.log:
                self.log.info("toolbox: retention — %d artifact(s) stripped, %d dead row(s) deleted",
                              r["stripped"], r["deleted"])
        except Exception as e:  # noqa: BLE001 — the janitor is never a dependency
            if self.log:
                self.log.warning("toolbox: retention sweep failed: %r", e)

    def _maybe_prune(self) -> None:
        # Piggyback retention on the worker's idle wake-ups (~2/s) instead of adding a
        # second thread: a six-hour cadence is more than enough for a days-scale policy.
        now = time.time()
        if now - self._last_prune < 21600:
            return
        self._last_prune = now
        self._prune()

    def _process(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job["state"] != "queued":
            return                                # cancelled/swept between enqueue and here
        with self._blobs_lock:
            blobs = self._blobs.pop(job_id, None)
        if blobs is None:
            self.store.set(job_id, state="error",
                           error="the render inputs were lost before the job started; "
                                 "submit it again")
            return
        source, mask = blobs
        job["spec"] = json.loads(job.get("spec_json") or "{}")

        def on_prompt_id(prompt_id, base):
            # Record the id the instant ComfyUI accepts the prompt, so cancel can
            # interrupt a render that is still waiting on its result.
            self.store.set(job_id, state="running", prompt_id=prompt_id, engine_base=base)

        def on_progress(fields):
            # In-flight progress for the bar, written straight to the row rather than
            # buffered: the poll route reads the row, so a page reopened mid-render shows
            # the same truth the worker saw instead of restarting from zero.
            try:
                self.store.set(job_id, progress_json=json.dumps(fields))
            except Exception:  # noqa: BLE001 — a lost progress tick must never cost a render
                pass

        try:
            # on_progress is offered only when the injected render accepts it. The seam is
            # documented as (job, source, mask, on_prompt_id); the suite's fakes and any
            # older render would TypeError on an unexpected kwarg, and that TypeError would
            # land on the user as a failed job.
            if _accepts_progress(self._render):
                art_b64, art_type = self._render(job, source, mask,
                                                 on_prompt_id=on_prompt_id,
                                                 on_progress=on_progress)
            else:
                art_b64, art_type = self._render(job, source, mask,
                                                 on_prompt_id=on_prompt_id)
        except Cancelled as e:
            self.store.set(job_id, state="cancelled", error=str(e) or None)
            return
        except (NoEngine, UserNotRegistered) as e:
            self.store.set(job_id, state="error", error=str(e))
            return
        except Exception as e:  # noqa: BLE001 — TimeoutError, httpx, graph drift…
            if self.log:
                self.log.warning("toolbox: job %s render failed: %r", job_id, e)
            # A cancel that landed mid-render tears the request out from under the wait;
            # the likeliest cause of an exception here, so report cancelled, not error.
            cur = self.store.pending_states(job_id)
            if cur and cur[0] == "cancelled":
                return
            self.store.set(job_id, state="error", error=f"{e.__class__.__name__}: {e}")
            return
        if art_b64 is None:
            self.store.set(job_id, state="error",
                           error="ComfyUI finished but returned no image")
            return
        # A cancel can land while the render is still in flight (the row was flipped to
        # cancelled out from under us). Honour it: writing done here would resurrect a job
        # the user deliberately stopped and hand back an artifact they asked us to drop.
        cur = self.store.pending_states(job_id)
        if cur and cur[0] == "cancelled":
            return
        fields = {"state": "done", "artifact_b64": art_b64, "artifact_type": art_type}
        # Crop provenance, if this render was crop-and-pasted: the artifact is a composite
        # of the model output and the user's own photo, and "done" must be able to say so
        # later rather than implying a pure model output. Absent for a full-frame render.
        if job.get("_crop_json"):
            fields["crop_json"] = job["_crop_json"]
        self.store.set(job_id, **fields)


