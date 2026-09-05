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

SCHEMA_VERSION = 1
RETAIN_DAYS = 7

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
    served_stack TEXT,
    served_profile TEXT,
    off_home INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_usage_day ON usage(day);

CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT, user_email TEXT, requested_model TEXT, served_profile TEXT,
    reqs INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    PRIMARY KEY (day, user_email, requested_model, served_profile)
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
        if s.get_meta("schema_version") is None:
            s.set_meta("schema_version", str(SCHEMA_VERSION))
        return s

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
                    "INSERT INTO usage_daily(day,user_email,requested_model,served_profile,reqs,"
                    "prompt_tokens,completion_tokens,cached_tokens) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(day,user_email,requested_model,served_profile) DO UPDATE SET "
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
                     prompt_tokens: int = 0, completion_tokens: int = 0,
                     cached_tokens: int = 0, off_home: bool = False, ok: bool = True,
                     now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.conn.execute(
            "INSERT INTO usage(ts,day,user_email,requested_model,served_stack,served_profile,"
            "off_home,prompt_tokens,completion_tokens,cached_tokens,ok) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, _utc_day(now), (user_email or "direct"), requested_model, served_stack,
             served_profile, int(off_home), prompt_tokens, completion_tokens, cached_tokens, int(ok)),
        )

    def rollup(self, *, retain_days: int = RETAIN_DAYS, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = _utc_day(now - retain_days * 86400)
        rows = self.conn.execute(
            "SELECT day, COALESCE(user_email,'direct') u, COALESCE(requested_model,'?') m, "
            "COALESCE(served_profile,'?') p, COUNT(*) reqs, SUM(prompt_tokens) pt, "
            "SUM(completion_tokens) ct, SUM(cached_tokens) cc "
            "FROM usage WHERE day < ? GROUP BY day,u,m,p", (cutoff,)
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "INSERT INTO usage_daily(day,user_email,requested_model,served_profile,reqs,"
                "prompt_tokens,completion_tokens,cached_tokens) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(day,user_email,requested_model,served_profile) DO UPDATE SET "
                "reqs=reqs+excluded.reqs, prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
                "completion_tokens=completion_tokens+excluded.completion_tokens, "
                "cached_tokens=cached_tokens+excluded.cached_tokens",
                (r["day"], r["u"], r["m"], r["p"], r["reqs"], r["pt"], r["ct"], r["cc"]),
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
            "COUNT(*) reqs, SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, "
            "SUM(cached_tokens) cached_tokens FROM usage" + where +
            " GROUP BY day,user_email,requested_model,served_profile", args
        ).fetchall()
        folded = self.conn.execute(
            "SELECT day,user_email,requested_model,served_profile,reqs,prompt_tokens,"
            "completion_tokens,cached_tokens FROM usage_daily" + where, args
        ).fetchall()
        return [dict(r) for r in list(raw) + list(folded)]

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
