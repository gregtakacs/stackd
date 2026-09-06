"""P5 — SQLite store (user keys + cost ledger), pricing tally, and the HTTP
wiring (/register, /register/users, /savings, usage attribution). No deps."""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

from stackd.engines.base import EngineState  # noqa: E402
from stackd.manager import Manager  # noqa: E402
from stackd.pricing import load_pricing, savings, sync_manual_prices  # noqa: E402
from stackd.runner import FakeRunner  # noqa: E402
from stackd.serve import _extract_usage, make_server  # noqa: E402
from stackd.store import Store, _utc_day  # noqa: E402

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
CHECKS: list[tuple[str, bool]] = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))


class _Fake(BaseHTTPRequestHandler):
    """Doubles as the Open WebUI auth endpoint and the model upstream."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/v1/auths"):
            tok = self.headers.get("authorization", "")
            if tok == "Bearer sk-good":
                return self._json(200, {"email": "user@example.com", "name": "Test User"})
            return self._json(401, {"detail": "bad key"})
        return self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        return self._json(200, {"id": "x", "choices": [{"message": {"content": "hi"}}],
                                "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                                          "prompt_tokens_details": {"cached_tokens": 20}}})


def _req(url, *, token=None, method="GET", body=None):
    h = {}
    if token:
        h["authorization"] = f"Bearer {token}"
    if body is not None:
        h["content-type"] = "application/json"
    r = urllib.request.Request(url, data=(json.dumps(body).encode() if body is not None else None),
                               headers=h, method=method)

    def _parse(raw):
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {"_text": raw.decode(errors="replace")}

    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())


def main() -> int:
    tmp = pathlib.Path(__import__("tempfile").mkdtemp())
    db = tmp / "stackd.db"
    st = Store.open(str(db))

    # --- user keys ----------------------------------------------------------------
    st.register_key("A@B.com", "sk-1", now=1000)
    check("resolve_key is case-insensitive", st.resolve_key("a@b.com") == "sk-1")
    check("email_for_key", st.email_for_key("sk-1") == "a@b.com")
    st.register_key("a@b.com", "sk-2", now=1100)  # rotate
    check("rotated key", st.resolve_key("a@b.com") == "sk-2" and st.email_for_key("sk-1") is None)
    st.register_key("c@d.com", "sk-2", now=1200)  # same key moves to a new email
    check("key can only belong to one email", st.resolve_key("a@b.com") is None
          and st.email_for_key("sk-2") == "c@d.com")
    check("list_users", {u["email"] for u in st.list_users()} == {"c@d.com"})
    check("delete_user", st.delete_user("c@d.com") and st.list_users() == [])

    # --- migration from user_keys.json -------------------------------------------
    n = st.import_user_keys_json({"X@Y.com": {"api_key": "sk-x", "registered_at": 42},
                                  "z@z.com": {"api_key": "sk-z"}})
    check("import_user_keys_json", n == 2 and st.resolve_key("x@y.com") == "sk-x")

    # --- ledger: record, rollup, retain ----------------------------------------------
    day0 = "2026-08-01"
    old = time.time() - 30 * 86400
    for i in range(3):
        st.record_usage(user_email="u@x.com", requested_model="assistant",
                        served_stack="chat", served_model="Qwen3.8-27B", served_profile="chat",
                        prompt_tokens=1000, completion_tokens=500, cached_tokens=200,
                        prefill_ms=400, decode_ms=2000, now=old)   # ctx=1500
    st.record_usage(user_email="u@x.com", requested_model="assistant",
                    served_stack="chat", served_model="Qwen3.8-27B", served_profile="chat",
                    prompt_tokens=2000, completion_tokens=800,
                    prefill_ms=500, decode_ms=4000)  # today, ctx=2800
    moved = st.rollup(retain_days=7)
    check("rollup folds old rows", moved == 3)
    rows = st.usage_rows()
    check("usage_rows spans raw + folded", len(rows) == 2)
    check("folded totals preserved",
          sum(r["prompt_tokens"] for r in rows) == 1000 * 3 + 2000)
    check("usage_rows carry stack + checkpoint through the rollup",
          all(r["served_stack"] == "chat" and r["served_model"] == "Qwen3.8-27B" for r in rows))
    # timing + context survive record -> rollup -> usage_rows
    check("timing sums fold through the rollup",
          sum(r["decode_ms"] for r in rows) == 3 * 2000 + 4000
          and sum(r["prefill_ms"] for r in rows) == 3 * 400 + 500)
    check("ctx_tokens_sum + ctx_tokens_max fold through",
          sum(r["ctx_tokens_sum"] for r in rows) == 3 * 1500 + 2800
          and max(r["ctx_tokens_max"] for r in rows) == 2800)

    # --- v2 -> v3 migration: split the conflated column into stack + checkpoint --
    import sqlite3 as _sqm
    v2p = tmp / "v2.db"
    v2 = _sqm.connect(str(v2p))
    v2.executescript(
        # v2 usage_daily: a `served_model` column that actually held the stack id
        "CREATE TABLE usage_daily (day TEXT, user_email TEXT, requested_model TEXT,"
        " served_profile TEXT, served_model TEXT NOT NULL DEFAULT '?', reqs INTEGER,"
        " prompt_tokens INTEGER, completion_tokens INTEGER, cached_tokens INTEGER,"
        " PRIMARY KEY (day,user_email,requested_model,served_profile,served_model));"
        "INSERT INTO usage_daily VALUES ('2026-05-01','a@x.com','assistant','chat','coding',4,400,200,10);"
        # v2 usage: served_stack, no served_model
        "CREATE TABLE usage (id INTEGER PRIMARY KEY, ts REAL, day TEXT, user_email TEXT,"
        " requested_model TEXT, served_stack TEXT, served_profile TEXT, off_home INT,"
        " prompt_tokens INT, completion_tokens INT, cached_tokens INT, ok INT);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta VALUES ('schema_version','2');")
    v2.commit(); v2.close()
    mg = Store.open(str(v2p))
    ucols = {r["name"] for r in mg.conn.execute("PRAGMA table_info(usage)")}
    dcols = {r["name"] for r in mg.conn.execute("PRAGMA table_info(usage_daily)")}
    check("v3 migration: usage gains served_model", "served_model" in ucols)
    check("v3 migration: usage_daily has both served_stack + served_model",
          {"served_stack", "served_model"} <= dcols)
    mr = mg.conn.execute("SELECT * FROM usage_daily").fetchone()
    check("v3 migration: old stack id moved to served_stack, checkpoint seeded '?'",
          mr["served_stack"] == "coding" and mr["served_model"] == "?"
          and mr["reqs"] == 4 and mr["prompt_tokens"] == 400)
    check("v4 migration adds timing/context columns",
          {"prefill_ms", "decode_ms", "ctx_tokens"} <= ucols
          and {"prefill_ms", "decode_ms", "ctx_tokens_sum", "ctx_tokens_max"} <= dcols)
    check("migration stamps the current schema_version",
          mg.get_meta("schema_version") == str(__import__("stackd.store", fromlist=["SCHEMA_VERSION"]).SCHEMA_VERSION))
    t_old = time.time() - 30 * 86400
    for sk, art in (("coding", "Flash-Next"), ("coding", "Flash-Next"),
                    ("uncensored-big", "Flash-Next-heretic")):
        mg.record_usage(user_email="a@x.com", requested_model="assistant", served_stack=sk,
                        served_model=art, served_profile="coding",
                        prompt_tokens=100, completion_tokens=50, now=t_old)
    mg.rollup(retain_days=7)
    folded = {(r["served_stack"], r["served_model"]): r["reqs"] for r in mg.usage_rows()
              if r["day"] == _utc_day(t_old)}
    check("rollup groups folded rows by (stack, checkpoint)",
          folded.get(("coding", "Flash-Next")) == 2
          and folded.get(("uncensored-big", "Flash-Next-heretic")) == 1)

    # --- backfill from a llama-priority-proxy usage.sqlite ------------------------
    import sqlite3 as _sq
    oldpx = tmp / "oldproxy.sqlite"
    oc = _sq.connect(str(oldpx))
    oc.executescript(
        "CREATE TABLE requests(id INTEGER PRIMARY KEY, ts REAL, day TEXT, requested_label TEXT,"
        " served_model TEXT, scenario TEXT, engine TEXT, prompt_tokens INT, completion_tokens INT,"
        " cached_tokens INT, reasoning_tokens INT, user TEXT, client TEXT, ok INT, off_home INT);"
        "CREATE TABLE daily(day TEXT, requested_label TEXT, served_model TEXT, scenario TEXT,"
        " engine TEXT, user TEXT, ok INT, req_count INT, prompt_tokens INT, completion_tokens INT,"
        " cached_tokens INT, reasoning_tokens INT, off_home_count INT);"
        "CREATE TABLE energy(day TEXT PRIMARY KEY, gpu_wh REAL, host_wh REAL);"
        "CREATE TABLE price_points(model TEXT, effective_from TEXT, input_mtok REAL, output_mtok REAL,"
        " cache_read_mtok REAL, source TEXT, fetched_at REAL);")
    oc.execute("INSERT INTO requests VALUES(1,1e9,'2026-07-01','TakacsAI-low','q','everyday','llama.cpp',900,100,50,0,'A@B.com','c',1,0)")
    oc.execute("INSERT INTO requests VALUES(2,1e9,'2026-07-01','TakacsAI-low','q','everyday','llama.cpp',100,20,0,0,'shared','c',1,0)")
    oc.execute("INSERT INTO daily VALUES('2026-06-01','TakacsAI','q','coding','vllm','greg@x.com',1,5,7000,300,0,0,0)")
    oc.execute("INSERT INTO energy VALUES('2026-06-01',1000,500)")
    oc.execute("INSERT INTO price_points VALUES('anthropic/claude-x','2026-06-01',3.0,15.0,0.3,'manual',1e9)")
    oc.commit(); oc.close()

    bf = Store.open(str(tmp / "backfill.db"))
    bf.add_energy("2026-06-01", gpu_wh=42, host_wh=1)   # pre-existing -> import must NOT touch it
    res = bf.import_proxy_db(str(oldpx))
    check("import_proxy_db reports rows", res["reqs"] == 7 and res["usage_daily_rows"] == 3)
    brows = {(r["day"], r["user_email"], r["served_profile"]): r for r in bf.usage_rows()}
    check("requests folded, scenario->profile, shared->direct",
          brows[("2026-07-01", "direct", "everyday")]["prompt_tokens"] == 100
          and brows[("2026-07-01", "a@b.com", "everyday")]["prompt_tokens"] == 900)
    check("daily rows imported too", brows[("2026-06-01", "greg@x.com", "coding")]["completion_tokens"] == 300)
    er = {r["day"]: r for r in bf.energy_rows("2026-01-01", "2026-12-01")}
    check("energy: pre-existing day left untouched", er["2026-06-01"]["gpu_wh"] == 42)
    check("price point carried over",
          bf.price_on("anthropic/claude-x", "2026-06-02")["output_mtok"] == 15.0)

    # --- pricing --------------------------------------------------------------------
    pcfg = load_pricing(CFG / "pricing.json")
    sync_manual_prices(st, pcfg)
    check("price_on resolves manual price",
          st.price_on("anthropic/claude-sonnet-4", "2026-08-01")["output_mtok"] == 15.0)
    st.add_energy(_utc_day(old), gpu_wh=500, host_wh=200)
    st.add_energy(_utc_day(), gpu_wh=120, host_wh=90)
    sv = savings(st, pcfg)
    front = next(t for t in sv["tiers"] if t["label"] == "frontier")
    # 5000 in tok * $3/M + 2100 out * $15/M  - cache discount ; > 0 and net < gross
    check("savings gross positive", front["gross"] > 0)
    check("savings net = gross - energy", front["net"] < front["gross"])
    check("per-profile breakdown present", "chat" in front["by"]["served_profile"])
    check("breakdown carries per-tier net",
          "net" in next(iter(front["by"]["served_profile"].values())))
    check("tier carries its resolved price + annualized",
          front["price"] and "input_mtok" in front["price"] and "annualized" in front)
    check("savings exposes pricing status", "pricing_stale" in sv and "last_openrouter_fetch" in sv)
    # token-weighted: decode_tok 2300 over decode_ms 10000 -> 230 tok/s
    tpv = sv["throughput"]
    check("savings.throughput is token-weighted",
          abs(tpv["decode_tps"] - 230) < 1 and tpv["ctx_max"] == 2800 and tpv["prefill_tps"] > 0)
    # an UNTIMED row (decode_ms=0) with a big completion must not blow up the mean
    st.record_usage(user_email="u@x.com", requested_model="assistant", served_stack="chat",
                    served_model="Qwen3.8-27B", served_profile="chat",
                    prompt_tokens=9999, completion_tokens=9999)  # no timing
    check("untimed rows don't poison the weighted tok/s",
          abs(savings(st, pcfg)["throughput"]["decode_tps"] - 230) < 1)
    pcfg2 = {**pcfg, "hardware_cost": 1000, "payback_tier": "frontier"}
    check("payback_pct computed when hardware_cost set",
          savings(st, pcfg2)["payback_pct"] is not None)

    # --- OpenRouter refresh (fake fetch) ------------------------------------------
    from stackd.pricing import refresh_openrouter
    fake_or = {"data": [
        {"id": "anthropic/claude-sonnet-4",
         "pricing": {"prompt": "0.000004", "completion": "0.00002", "input_cache_read": "0.0000004"}},
    ]}
    n_upd = refresh_openrouter(st, pcfg, today="2026-09-10", fetch=lambda: fake_or)
    check("refresh_openrouter writes a changed point + stamps meta",
          n_upd == 1 and st.get_meta("last_openrouter_fetch") == "2026-09-10"
          and st.price_on("anthropic/claude-sonnet-4", "2026-09-10")["input_mtok"] == 4.0)

    # --- _extract_usage --------------------------------------------------------------
    check("_extract_usage from JSON",
          _extract_usage(b'{"usage":{"prompt_tokens":7}}')["prompt_tokens"] == 7)
    sse = b'data: {"choices":[]}\n\ndata: {"usage":{"prompt_tokens":9,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
    check("_extract_usage from SSE tail", _extract_usage(sse)["completion_tokens"] == 3)

    # --- prompt-cache estimator (frontier ROI proxy, not backend reuse) ------------
    from stackd.promptcache import PromptCacheModel, pc_units
    pcm = PromptCacheModel({"ttl_s": 300, "min_prefix_tokens": 100, "history_per_model": 8})
    sysmsg = {"role": "system", "content": "x" * 4000}
    b1 = {"messages": [sysmsg, {"role": "user", "content": "hello"}]}
    b2 = {"messages": [sysmsg, {"role": "user", "content": "hello"},
                       {"role": "assistant", "content": "hi"}, {"role": "user", "content": "again"}]}
    check("first request under a key has no prefix to match",
          pcm.measure("u\x00coding", pc_units(b1), 1100, now=1000) == 0)
    c2 = pcm.measure("u\x00coding", pc_units(b2), 1200, now=1001)
    check("follow-up matches the shared leading prefix", 0 < c2 < 1200)
    check("a different key shares no history",
          pcm.measure("v\x00coding", pc_units(b2), 1200, now=1002) == 0)
    check("match expires past the TTL window",
          pcm.measure("u\x00coding", pc_units(b2), 1200, now=1001 + 400) == 0)
    check("disabled model (no config block) always returns 0",
          PromptCacheModel(None).measure("k", pc_units(b2), 5000) == 0)
    check("below min_prefix_tokens -> 0",
          PromptCacheModel({"min_prefix_tokens": 999999}).measure(
              "k", pc_units(b1), 10) == 0)

    # --- HTTP wiring --------------------------------------------------------------------
    fake = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fake_url = f"http://127.0.0.1:{fake.server_address[1]}"

    mgr = Manager(CFG, tmp / "state.json", FakeRunner(ready_after=1))
    mgr.use("chat", now=0)

    # boot_reset: a persisted give-up / crash limbo must not survive a daemon restart
    from stackd.engines.base import EngineState as _ES
    names = list(mgr.state.stacks)
    dead = mgr.state.stacks[names[0]]
    dead.state, dead.restarts, dead.handle = _ES.error, 5, None   # crashed / gave up
    up = mgr.state.stacks[names[1]]
    up.state, up.restarts, up.handle = _ES.ready, 2, "h"          # genuinely up
    dropped = mgr.state.boot_reset()
    check("boot_reset drops the crashed stack", names[0] in dropped and names[0] not in mgr.state.stacks)
    check("boot_reset keeps the running stack, zeroes its budget",
          names[1] in mgr.state.stacks and mgr.state.stacks[names[1]].restarts == 0)
    for i in range(4):
        mgr.tick(now=(i + 1) * 5)
    mgr.state.stacks["chat"].endpoint = fake_url

    hstore = Store.open(str(tmp / "http.db"))
    httpd = make_server(mgr, "127.0.0.1", 0, api_key="admin", warm_wait_s=2,
                        store=hstore, owu_base_url=fake_url,
                        pricing_path=str(CFG / "pricing.json"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, _ = _req(f"{base}/register")
        check("GET /register is open (no auth)", code == 200)
        code, j = _req(f"{base}/register", method="POST", body={"api_key": "sk-bad"})
        check("POST /register rejects a bad key", code == 400)
        code, j = _req(f"{base}/register", method="POST", body={"api_key": "sk-good"})
        check("POST /register validates via OWU + lowercases email",
              code == 200 and j["email"] == "user@example.com")
        check("registered key is stored", hstore.resolve_key("user@example.com") == "sk-good")

        code, _ = _req(f"{base}/register/users")
        check("/register/users needs admin", code == 401)
        code, j = _req(f"{base}/register/users", token="admin")
        check("/register/users lists", code == 200 and len(j["users"]) == 1)
        code, j = _req(f"{base}/register/users/user@example.com/key", token="admin")
        check("/register/users/<e>/key reveals", j["api_key"] == "sk-good")

        # a chat request -> proxied, usage recorded, attributed to the OWU email header
        r = urllib.request.Request(
            f"{base}/v1/chat/completions", method="POST",
            data=json.dumps({"model": "assistant", "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"content-type": "application/json", "authorization": "Bearer admin",
                     "X-OpenWebUI-User-Email": "user@example.com"})
        with urllib.request.urlopen(r, timeout=10) as resp:
            resp.read()
        time.sleep(0.2)
        urows = hstore.usage_rows()
        check("usage row recorded", len(urows) == 1)
        check("attributed to the OWU user", urows[0]["user_email"] == "user@example.com")
        check("token counts captured", urows[0]["prompt_tokens"] == 100 and urows[0]["completion_tokens"] == 40)

        code, j = _req(f"{base}/savings", token="admin")
        check("GET /savings -> 200", code == 200 and j["reqs"] == 1)
        check("savings sees the user", "user@example.com" in j["tiers"][0]["by"]["user_email"])
    finally:
        httpd.shutdown()
        fake.shutdown()

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
