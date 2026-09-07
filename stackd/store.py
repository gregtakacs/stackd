"""SQLite datastore (stdlib `sqlite3`, no dependency) — one file, two jobs:

  * `user_keys`  — Open WebUI per-user API keys, self-service registered at
                   /register. Replaces a prior proxy's user_keys.json.
  * `usage` / `usage_daily` / `price_points` / `energy` — the cost-savings ledger.

Raw usage rows are kept `RETAIN_DAYS` then folded into `usage_daily`.
"""

from __future__ import annotations

import datetime
import sqlite3
import time
from dataclasses import dataclass

SCHEMA_VERSION = 7
RETAIN_DAYS = 40

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_keys (
    email TEXT PRIMARY KEY,
    api_key TEXT NOT NULL,
    label TEXT,
    registered_at REAL,
    last_validated_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_user_keys_apikey ON user_keys(api_key);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    user_email TEXT,
    requested_model TEXT,
    served_stack TEXT,           -- the stackd stack/unit (e.g. 'coding')
    served_model TEXT,           -- the checkpoint on disk (e.g. 'Qwen3.8-Flash-Next-NVFP4')
    served_profile TEXT,
    off_home INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1,
    ctx_tokens INTEGER DEFAULT 0    -- context size this request occupied (prompt + completion)
);
CREATE INDEX IF NOT EXISTS ix_usage_day ON usage(day);

CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT, user_email TEXT, requested_model TEXT, served_profile TEXT,
    served_stack TEXT NOT NULL DEFAULT '?',
    served_model TEXT NOT NULL DEFAULT '?',
    reqs INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    -- context size (over rows with ctx_tokens>0): peak = ctx_tokens_max;
    -- token-weighted mean = ctx_tokens_sq_sum / ctx_tokens_sum (weights each
    -- request by its own size, so a flood of tiny agent calls can't drag it
    -- down); plain arithmetic mean = ctx_tokens_sum / ctx_n is still available.
    -- (tok/s used to fold here too — v6 moved it to engine_daily, the /metrics sampler.)
    ctx_tokens_sum INTEGER DEFAULT 0,     -- Σ ctx_tokens
    ctx_tokens_sq_sum INTEGER DEFAULT 0,  -- Σ ctx_tokens² (for the token-weighted mean)
    ctx_tokens_max INTEGER DEFAULT 0,
    ctx_n INTEGER DEFAULT 0,              -- count of those rows -> arith. mean = sum / ctx_n
    PRIMARY KEY (day, user_email, requested_model, served_profile, served_stack, served_model)
);

-- engine-measured throughput, sampled from each running engine's own /metrics
-- counters (see telemetry.engine_counters) and folded per day. Keyed by the
-- running stack only — the sampler has no user / requested_model attribution;
-- savings_facts pro-rates these onto the priced rows by token share. tps =
-- decode_tok / decode_ms * 1000 (prefill likewise). Persisted like usage_daily
-- (never pruned) — one row per stack/model/profile/day.
CREATE TABLE IF NOT EXISTS engine_daily (
    day TEXT NOT NULL,
    served_stack TEXT NOT NULL DEFAULT '?',
    served_model TEXT NOT NULL DEFAULT '?',
    served_profile TEXT NOT NULL DEFAULT '?',
    decode_tok INTEGER DEFAULT 0,       -- Σ generation tokens over sampled windows
    decode_ms INTEGER DEFAULT 0,        -- Σ generation time (engine counter, or
                                        --   wall-clock while a request was in flight)
    prefill_tok INTEGER DEFAULT 0,      -- Σ prompt tokens actually prefilled
    prefill_ms INTEGER DEFAULT 0,       -- Σ prefill time (engine counter)
    ctx_tokens_max INTEGER DEFAULT 0,   -- peak KV fill (tokens) seen that day
    PRIMARY KEY (day, served_stack, served_model, served_profile)
);

CREATE TABLE IF NOT EXISTS price_points (
    ref TEXT, effective_from TEXT,
    input_mtok REAL, output_mtok REAL, cache_read_mtok REAL,
    PRIMARY KEY (ref, effective_from)
);

CREATE TABLE IF NOT EXISTS energy (
    day TEXT PRIMARY KEY, gpu_wh REAL DEFAULT 0, host_wh REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _utc_day(ts: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(
        ts if ts is not None else time.time(), datetime.timezone.utc
    ).strftime("%Y-%m-%d")


@dataclass
class Store:
    conn: sqlite3.Connection

    @classmethod
    def open(cls, path: str) -> "Store":
        # check_same_thread=False: the HTTP handler threads + the tick thread all
        # touch the store; every caller in serve.py holds the single server lock.
        conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        conn.executescript(_SCHEMA)
        s = cls(conn)
        s._migrate()
        return s

    def _migrate(self) -> None:
        """Idempotent schema catch-ups for DBs created before SCHEMA_VERSION.
        Keyed off the actual table shape, not the stored version, so a DB that
        predates the version stamp is handled too.

        The ledger distinguishes two things that used to be conflated:
          * served_stack  — the stackd unit / yaml stack (e.g. 'coding')
          * served_model  — the checkpoint on disk (e.g. 'Qwen3.8-Flash-Next-NVFP4')
        v2 stored the stack in a column it called `served_model`; v3 renames that
        to `served_stack` and adds a real `served_model` (seeded '?', backfilled
        out of band). `usage` (raw) gains a `served_model` column too.

        v4 added per-request `prefill_ms`/`decode_ms` for a token-weighted tok/s
        fold; v6 drops them again — throughput moved to `engine_daily` (the
        /metrics sampler) and the wall-clock split was too noisy to keep."""
        ucols = {r["name"] for r in self.conn.execute("PRAGMA table_info(usage)")}
        if "served_model" not in ucols:
            self.conn.execute("ALTER TABLE usage ADD COLUMN served_model TEXT")
        # v4: per-request context size. Plain ADD COLUMN — cheap, back-fills 0.
        if "ctx_tokens" not in ucols:
            self.conn.execute("ALTER TABLE usage ADD COLUMN ctx_tokens INTEGER DEFAULT 0")

        dcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(usage_daily)")}
        if "served_stack" not in dcols:
            # v1 (neither col) or v2 (a `served_model` that actually holds the
            # stack id) -> v3. Copy that column into served_stack; new
            # served_model starts as '?'.
            stack_src = "served_model" if "served_model" in dcols else "'?'"
            self.conn.executescript(f"""
                BEGIN;
                CREATE TABLE usage_daily_v3 (
                    day TEXT, user_email TEXT, requested_model TEXT, served_profile TEXT,
                    served_stack TEXT NOT NULL DEFAULT '?',
                    served_model TEXT NOT NULL DEFAULT '?',
                    reqs INTEGER DEFAULT 0,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    cached_tokens INTEGER DEFAULT 0,
                    PRIMARY KEY (day, user_email, requested_model, served_profile,
                                 served_stack, served_model)
                );
                INSERT INTO usage_daily_v3
                    (day,user_email,requested_model,served_profile,served_stack,served_model,
                     reqs,prompt_tokens,completion_tokens,cached_tokens)
                SELECT day,user_email,requested_model,served_profile,{stack_src},'?',
                       reqs,prompt_tokens,completion_tokens,cached_tokens FROM usage_daily;
                DROP TABLE usage_daily;
                ALTER TABLE usage_daily_v3 RENAME TO usage_daily;
                COMMIT;
            """)
            dcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(usage_daily)")}
        # v4: matching context running-sums on the folded table.
        # v7: + ctx_tokens_sq_sum (Σ ctx²) for the token-weighted mean. Plain
        # ADD COLUMN -> already-folded rows back-fill 0, so days older than
        # RETAIN_DAYS show no weighted mean until new traffic; raw `usage` (the
        # last 40 days, i.e. every default explorer range) is recomputed exactly.
        for col in ("ctx_tokens_sum", "ctx_tokens_sq_sum", "ctx_tokens_max", "ctx_n"):
            if col not in dcols:
                self.conn.execute(f"ALTER TABLE usage_daily ADD COLUMN {col} INTEGER DEFAULT 0")

        # v6: drop the dead per-request throughput fold (-> engine_daily). Needs
        # SQLite >= 3.35 DROP COLUMN; guarded by table_info so it's a one-time
        # no-op, and best-effort — an ancient SQLite just keeps the empty columns.
        drops = ([("usage", c) for c in ("prefill_ms", "decode_ms") if c in ucols]
                 + [("usage_daily", c) for c in
                    ("prefill_ms", "prefill_tok", "decode_ms", "decode_tok") if c in dcols])
        for tbl, col in drops:
            try:
                self.conn.execute(f"ALTER TABLE {tbl} DROP COLUMN {col}")
            except sqlite3.OperationalError:
                pass

        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.conn.close()

    # -- meta -------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- user keys -----------------------------------------------------------------
    def register_key(self, email: str, api_key: str, label: str | None = None,
                     *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        email = email.strip().lower()
        # a key can only belong to one email — drop stale rows that held it
        self.conn.execute("DELETE FROM user_keys WHERE api_key=? AND email<>?", (api_key, email))
        existing = self.conn.execute(
            "SELECT registered_at FROM user_keys WHERE email=?", (email,)
        ).fetchone()
        reg = existing["registered_at"] if existing else now
        self.conn.execute(
            "INSERT INTO user_keys(email,api_key,label,registered_at,last_validated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
            "api_key=excluded.api_key, label=COALESCE(excluded.label,user_keys.label), "
            "last_validated_at=excluded.last_validated_at",
            (email, api_key, label, reg, now),
        )

    def resolve_key(self, email: str) -> str | None:
        r = self.conn.execute(
            "SELECT api_key FROM user_keys WHERE email=?", (email.strip().lower(),)
        ).fetchone()
        return r["api_key"] if r else None

    def email_for_key(self, api_key: str) -> str | None:
        r = self.conn.execute(
            "SELECT email FROM user_keys WHERE api_key=?", (api_key,)
        ).fetchone()
        return r["email"] if r else None

    def list_users(self) -> list[dict]:
        return [
            {"email": r["email"], "label": r["label"],
             "registered_at": r["registered_at"], "last_validated_at": r["last_validated_at"],
             "key_prefix": (r["api_key"] or "")[:8]}
            for r in self.conn.execute(
                "SELECT * FROM user_keys ORDER BY registered_at").fetchall()
        ]

    def delete_user(self, email: str) -> bool:
        cur = self.conn.execute("DELETE FROM user_keys WHERE email=?", (email.strip().lower(),))
        return cur.rowcount > 0

    def touch_validated(self, email: str, *, now: float | None = None) -> None:
        self.conn.execute(
            "UPDATE user_keys SET last_validated_at=? WHERE email=?",
            (time.time() if now is None else now, email.strip().lower()),
        )

    def import_user_keys_json(self, data: dict) -> int:
        """data = {email: {api_key, registered_at?, ...}} (prior-proxy export format)."""
        n = 0
        for email, rec in (data or {}).items():
            key = rec.get("api_key") if isinstance(rec, dict) else rec
            if not key:
                continue
            self.register_key(email, key, now=(rec.get("registered_at") if isinstance(rec, dict) else None))
            n += 1
        return n

    def import_proxy_db(self, path: str) -> dict:
        """Backfill history from a llama-priority-proxy `usage.sqlite`: its
        `requests` + `daily` fold into our `usage_daily` (scenario -> profile,
        requested_label -> requested_model, `shared`/blank user -> `direct`);
        `energy` is copied for days we don't already have (no double-count on the
        hand-over day); `price_points` are added without clobbering ours
        (`manual` wins over `openrouter` within the source data)."""
        src = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        src.row_factory = sqlite3.Row
        out = {"usage_daily_rows": 0, "reqs": 0, "energy_days": 0, "price_points": 0}

        def _key(day, user, label, scen):
            u = (user or "").strip().lower()
            return (day, ("direct" if u in ("", "shared") else u),
                    (label or "?"), (scen or "?"))

        try:
            agg: dict[tuple, list[int]] = {}
            for r in src.execute(
                "SELECT day, user, requested_label, scenario, COUNT(*) reqs, "
                "SUM(prompt_tokens) pt, SUM(completion_tokens) ct, SUM(cached_tokens) cc "
                "FROM requests GROUP BY day, user, requested_label, scenario"):
                a = agg.setdefault(_key(r["day"], r["user"], r["requested_label"], r["scenario"]),
                                   [0, 0, 0, 0])
                a[0] += r["reqs"]; a[1] += r["pt"] or 0; a[2] += r["ct"] or 0; a[3] += r["cc"] or 0
            try:
                for r in src.execute(
                    "SELECT day, user, requested_label, scenario, req_count reqs, "
                    "prompt_tokens pt, completion_tokens ct, cached_tokens cc FROM daily"):
                    a = agg.setdefault(_key(r["day"], r["user"], r["requested_label"], r["scenario"]),
                                       [0, 0, 0, 0])
                    a[0] += r["reqs"] or 0; a[1] += r["pt"] or 0
                    a[2] += r["ct"] or 0; a[3] += r["cc"] or 0
            except sqlite3.OperationalError:
                pass
            for (day, u, m, p), (reqs, pt, ct, cc) in agg.items():
                self.conn.execute(
                    "INSERT INTO usage_daily(day,user_email,requested_model,served_profile,"
                    "served_stack,served_model,reqs,prompt_tokens,completion_tokens,cached_tokens) "
                    "VALUES(?,?,?,?,'?','?',?,?,?,?) "
                    "ON CONFLICT(day,user_email,requested_model,served_profile,served_stack,served_model) "
                    "DO UPDATE SET "
                    "reqs=reqs+excluded.reqs, prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
                    "completion_tokens=completion_tokens+excluded.completion_tokens, "
                    "cached_tokens=cached_tokens+excluded.cached_tokens",
                    (day, u, m, p, reqs, pt, ct, cc))
                out["usage_daily_rows"] += 1
                out["reqs"] += reqs

            have = {r[0] for r in self.conn.execute("SELECT day FROM energy")}
            for r in src.execute("SELECT day, gpu_wh, host_wh FROM energy"):
                if r["day"] in have:
                    continue
                self.conn.execute("INSERT OR IGNORE INTO energy(day,gpu_wh,host_wh) VALUES(?,?,?)",
                                  (r["day"], r["gpu_wh"] or 0, r["host_wh"] or 0))
                out["energy_days"] += 1

            try:
                pick: dict[tuple, sqlite3.Row] = {}
                for r in src.execute("SELECT model,effective_from,input_mtok,output_mtok,"
                                     "cache_read_mtok,source FROM price_points"):
                    k = (r["model"], r["effective_from"])
                    if k not in pick or r["source"] == "manual":
                        pick[k] = r
                for (ref, eff), r in pick.items():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO price_points(ref,effective_from,input_mtok,"
                        "output_mtok,cache_read_mtok) VALUES(?,?,?,?,?)",
                        (ref, eff, r["input_mtok"], r["output_mtok"], r["cache_read_mtok"]))
                    out["price_points"] += 1
            except sqlite3.OperationalError:
                pass
            self.conn.commit()
        finally:
            src.close()
        return out

    # -- usage ledger ------------------------------------------------------------------
    def record_usage(self, *, user_email: str | None, requested_model: str | None,
                     served_stack: str | None, served_profile: str | None,
                     served_model: str | None = None,
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     cached_tokens: int = 0, off_home: bool = False, ok: bool = True,
                     ctx_tokens: int | None = None,
                     now: float | None = None) -> None:
        # No per-request timing: tok/s comes from engine_daily (the /metrics
        # sampler). The vestigial usage.{prefill_ms,decode_ms} columns are left
        # DEFAULT 0 — never written, never read.
        now = time.time() if now is None else now
        if ctx_tokens is None:
            ctx_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
        self.conn.execute(
            "INSERT INTO usage(ts,day,user_email,requested_model,served_stack,served_model,"
            "served_profile,off_home,prompt_tokens,completion_tokens,cached_tokens,ok,ctx_tokens) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, _utc_day(now), (user_email or "direct"), requested_model, served_stack,
             served_model, served_profile, int(off_home),
             prompt_tokens, completion_tokens, cached_tokens, int(ok), int(ctx_tokens or 0)),
        )

    def rollup(self, *, retain_days: int = RETAIN_DAYS, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = _utc_day(now - retain_days * 86400)
        rows = self.conn.execute(
            "SELECT day, COALESCE(user_email,'direct') u, COALESCE(requested_model,'?') m, "
            "COALESCE(served_profile,'?') p, COALESCE(served_stack,'?') sk, "
            "COALESCE(served_model,'?') sm, "
            "COUNT(*) reqs, SUM(prompt_tokens) pt, "
            "SUM(completion_tokens) ct, SUM(cached_tokens) cc, "
            "SUM(CASE WHEN ctx_tokens>0 THEN ctx_tokens ELSE 0 END) cxs, "
            "SUM(CASE WHEN ctx_tokens>0 THEN ctx_tokens*ctx_tokens ELSE 0 END) cxsq, "
            "MAX(ctx_tokens) cxm, "
            "SUM(CASE WHEN ctx_tokens>0 THEN 1 ELSE 0 END) cxn "
            "FROM usage WHERE day < ? GROUP BY day,u,m,p,sk,sm", (cutoff,)
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "INSERT INTO usage_daily(day,user_email,requested_model,served_profile,"
                "served_stack,served_model,reqs,prompt_tokens,completion_tokens,cached_tokens,"
                "ctx_tokens_sum,ctx_tokens_sq_sum,ctx_tokens_max,ctx_n) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(day,user_email,requested_model,served_profile,served_stack,served_model) "
                "DO UPDATE SET "
                "reqs=reqs+excluded.reqs, prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
                "completion_tokens=completion_tokens+excluded.completion_tokens, "
                "cached_tokens=cached_tokens+excluded.cached_tokens, "
                "ctx_tokens_sum=ctx_tokens_sum+excluded.ctx_tokens_sum, "
                "ctx_tokens_sq_sum=ctx_tokens_sq_sum+excluded.ctx_tokens_sq_sum, "
                "ctx_tokens_max=MAX(ctx_tokens_max, excluded.ctx_tokens_max), "
                "ctx_n=ctx_n+excluded.ctx_n",
                (r["day"], r["u"], r["m"], r["p"], r["sk"], r["sm"],
                 r["reqs"], r["pt"], r["ct"], r["cc"],
                 r["cxs"] or 0, r["cxsq"] or 0, r["cxm"] or 0, r["cxn"] or 0),
            )
        cur = self.conn.execute("DELETE FROM usage WHERE day < ?", (cutoff,))
        return cur.rowcount

    def usage_rows(self, start_day: str | None = None, end_day: str | None = None) -> list[dict]:
        """Unified view over raw `usage` + folded `usage_daily`, as day-grain rows."""
        w, args = [], []
        if start_day:
            w.append("day >= ?"); args.append(start_day)
        if end_day:
            w.append("day <= ?"); args.append(end_day)
        where = (" WHERE " + " AND ".join(w)) if w else ""
        raw = self.conn.execute(
            "SELECT day, COALESCE(user_email,'direct') user_email, "
            "COALESCE(requested_model,'?') requested_model, COALESCE(served_profile,'?') served_profile, "
            "COALESCE(served_stack,'?') served_stack, COALESCE(served_model,'?') served_model, "
            "COUNT(*) reqs, SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, "
            "SUM(cached_tokens) cached_tokens, "
            "SUM(CASE WHEN ctx_tokens>0 THEN ctx_tokens ELSE 0 END) ctx_tokens_sum, "
            "SUM(CASE WHEN ctx_tokens>0 THEN ctx_tokens*ctx_tokens ELSE 0 END) ctx_tokens_sq_sum, "
            "MAX(ctx_tokens) ctx_tokens_max, "
            "SUM(CASE WHEN ctx_tokens>0 THEN 1 ELSE 0 END) ctx_n FROM usage" + where +
            " GROUP BY day,user_email,requested_model,served_profile,served_stack,served_model", args
        ).fetchall()
        folded = self.conn.execute(
            "SELECT day,user_email,requested_model,served_profile,served_stack,served_model,reqs,"
            "prompt_tokens,completion_tokens,cached_tokens,ctx_tokens_sum,ctx_tokens_max,ctx_n,"
            # rows folded before v7 have no Σctx² -> fall back to n·mean²
            # (= ctx_tokens_sum²/ctx_n), i.e. assume weighted == arithmetic for
            # that row rather than let a 0 pull the pooled weighted mean down.
            "CASE WHEN ctx_tokens_sq_sum>0 THEN ctx_tokens_sq_sum "
            "     WHEN ctx_n>0 THEN ctx_tokens_sum*ctx_tokens_sum/ctx_n "
            "     ELSE 0 END ctx_tokens_sq_sum "
            "FROM usage_daily" + where, args
        ).fetchall()
        return [dict(r) for r in list(raw) + list(folded)]

    # -- engine throughput ----------------------------------------------------------
    def add_engine_sample(self, *, served_stack: str, served_model: str | None,
                          served_profile: str | None,
                          decode_tok: int = 0, decode_ms: int = 0,
                          prefill_tok: int = 0, prefill_ms: int = 0,
                          ctx_tokens_max: int = 0, now: float | None = None) -> None:
        """Fold one accumulated engine-throughput window into the per-day roll.
        Additive on the token/ms running sums; MAX on the context peak."""
        day = _utc_day(time.time() if now is None else now)
        self.conn.execute(
            "INSERT INTO engine_daily(day,served_stack,served_model,served_profile,"
            "decode_tok,decode_ms,prefill_tok,prefill_ms,ctx_tokens_max) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day,served_stack,served_model,served_profile) DO UPDATE SET "
            "decode_tok=decode_tok+excluded.decode_tok, decode_ms=decode_ms+excluded.decode_ms, "
            "prefill_tok=prefill_tok+excluded.prefill_tok, prefill_ms=prefill_ms+excluded.prefill_ms, "
            "ctx_tokens_max=MAX(ctx_tokens_max, excluded.ctx_tokens_max)",
            (day, served_stack, served_model or "?", served_profile or "?",
             int(decode_tok or 0), int(decode_ms or 0),
             int(prefill_tok or 0), int(prefill_ms or 0), int(ctx_tokens_max or 0)),
        )

    def engine_rows(self, start_day: str | None = None,
                    end_day: str | None = None) -> list[dict]:
        w, args = [], []
        if start_day:
            w.append("day >= ?"); args.append(start_day)
        if end_day:
            w.append("day <= ?"); args.append(end_day)
        where = (" WHERE " + " AND ".join(w)) if w else ""
        return [dict(r) for r in self.conn.execute(
            "SELECT day,served_stack,served_model,served_profile,decode_tok,decode_ms,"
            "prefill_tok,prefill_ms,ctx_tokens_max FROM engine_daily" + where, args
        ).fetchall()]

    # -- prices --------------------------------------------------------------------
    def set_price(self, ref: str, effective_from: str, *, input_mtok: float,
                  output_mtok: float, cache_read_mtok: float = 0.0) -> None:
        self.conn.execute(
            "INSERT INTO price_points(ref,effective_from,input_mtok,output_mtok,cache_read_mtok) "
            "VALUES(?,?,?,?,?) ON CONFLICT(ref,effective_from) DO UPDATE SET "
            "input_mtok=excluded.input_mtok, output_mtok=excluded.output_mtok, "
            "cache_read_mtok=excluded.cache_read_mtok",
            (ref, effective_from, input_mtok, output_mtok, cache_read_mtok),
        )

    def price_on(self, ref: str, day: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM price_points WHERE ref=? AND effective_from <= ? "
            "ORDER BY effective_from DESC LIMIT 1", (ref, day)
        ).fetchone()
        if r is None:
            r = self.conn.execute(
                "SELECT * FROM price_points WHERE ref=? ORDER BY effective_from ASC LIMIT 1", (ref,)
            ).fetchone()
        return dict(r) if r else None

    # -- energy -------------------------------------------------------------------
    def add_energy(self, day: str, *, gpu_wh: float = 0.0, host_wh: float = 0.0) -> None:
        self.conn.execute(
            "INSERT INTO energy(day,gpu_wh,host_wh) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET gpu_wh=gpu_wh+excluded.gpu_wh, "
            "host_wh=host_wh+excluded.host_wh", (day, gpu_wh, host_wh),
        )

    def energy_between(self, start_day: str, end_day: str) -> dict:
        r = self.conn.execute(
            "SELECT COALESCE(SUM(gpu_wh),0) g, COALESCE(SUM(host_wh),0) h "
            "FROM energy WHERE day BETWEEN ? AND ?", (start_day, end_day)
        ).fetchone()
        return {"gpu_kwh": r["g"] / 1000.0, "host_kwh": r["h"] / 1000.0}

    def energy_rows(self, start_day: str, end_day: str) -> list[dict]:
        """Per-day energy in the range (for the dashboard's history chart)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT day, gpu_wh, host_wh FROM energy WHERE day BETWEEN ? AND ? "
            "ORDER BY day", (start_day, end_day)
        ).fetchall()]
