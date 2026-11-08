"""Named, scoped API keys (store.api_keys + serve._caller).

The admin key (STACKD_API_KEY) keeps doing everything it did. A named key with scope
'inference' may list models and run completions and nothing else; its usage is booked to
`key:<name>` whatever forwarded-user headers its caller sends; revoking it takes effect
immediately (the daemon's in-memory table is refreshed), and only the admin can manage keys.
Auth never takes the server lock, which a converge holds for tens of seconds."""
import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import _env  # noqa: E402,F401

from stackd.manager import Manager
from stackd.runner import FakeRunner
from stackd.serve import make_server
from stackd.store import Store

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
ADMIN = "secret-admin"


class _Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        p = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)


class _Srv:
    def __enter__(self):
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), _Up)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        mgr = Manager(CFG, pathlib.Path(f"/tmp/apikeys-{time.time_ns()}.json"), FakeRunner(ready_after=1))
        mgr.use("chat", now=0)
        for i in range(4):
            mgr.tick(now=(i + 1) * 5)
        mgr.state.stacks["chat"].endpoint = f"http://127.0.0.1:{self.up.server_address[1]}"
        self.store = Store.open(":memory:")
        self.httpd = make_server(mgr, "127.0.0.1", 0, api_key=ADMIN, warm_wait_s=3, store=self.store)
        self.handler = self.httpd.RequestHandlerClass
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.up.shutdown()

    def req(self, method, path, key=None, body=None, headers=None):
        h = {"content-type": "application/json", **(headers or {})}
        if key:
            h["authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def chat(self, key, headers=None):
        return self.req("POST", "/v1/chat/completions", key,
                        {"model": "assistant", "messages": [{"role": "user", "content": "hi"}]}, headers)

    def mint(self, name, scope="inference"):
        code, body = self.req("POST", "/keys", ADMIN, {"name": name, "scope": scope})
        assert code == 201, body
        return body["key"]


def _usage_users(store):
    return {r["user_email"] for r in store.conn.execute("SELECT user_email FROM usage")}


def test_admin_key_unchanged():
    with _Srv() as s:
        assert s.req("GET", "/v1/models", ADMIN)[0] == 200
        assert s.req("GET", "/status", ADMIN)[0] == 200
        assert s.chat(ADMIN)[0] == 200
        assert s.req("GET", "/status")[0] == 401
        assert s.req("GET", "/status", "wrong")[0] == 401


def test_inference_key_can_infer_and_nothing_else():
    with _Srv() as s:
        k = s.mint("delegate-test")
        assert k.startswith("sk-stackd-") and len(k) > 40
        assert s.req("GET", "/v1/models", k)[0] == 200
        assert s.req("GET", "/models", k)[0] == 200
        code, body = s.chat(k)
        assert code == 200 and body["choices"][0]["message"]["content"] == "ok"
        # everything that is not inference is closed to it
        for method, path in [("GET", "/status"), ("GET", "/profiles"), ("GET", "/savings"),
                             ("GET", "/events"), ("GET", "/keys"), ("GET", "/register/users"),
                             ("POST", "/reload"), ("POST", "/profiles/chat/activate"),
                             ("POST", "/keys"), ("DELETE", "/keys/delegate-test"),
                             ("POST", "/shutdown")]:
            code, _ = s.req(method, path, k)
            assert code in (401, 403), (method, path, code)
        # and it did not shut anything down
        assert s.req("GET", "/v1/models", k)[0] == 200


def test_usage_is_booked_to_the_key_and_cannot_be_spoofed():
    with _Srv() as s:
        k = s.mint("agent-a")
        assert s.chat(k, headers={"X-OpenWebUI-User-Email": "victim@example.com"})[0] == 200
        assert s.chat(ADMIN, headers={"X-OpenWebUI-User-Email": "greg@example.com"})[0] == 200
        users = _usage_users(s.store)
        assert "key:agent-a" in users
        assert "victim@example.com" not in users          # header ignored for a named key
        assert "greg@example.com" in users                # still honoured for the admin/OWU path


def test_revoke_takes_effect_immediately_and_name_can_be_reused():
    with _Srv() as s:
        k = s.mint("tmp")
        assert s.req("GET", "/v1/models", k)[0] == 200
        assert s.req("POST", "/keys", ADMIN, {"name": "tmp"})[0] == 400       # live name clash
        assert s.req("DELETE", "/keys/tmp", ADMIN)[1]["revoked"] is True
        assert s.req("GET", "/v1/models", k)[0] == 401
        assert s.req("DELETE", "/keys/tmp", ADMIN)[0] == 404
        k2 = s.mint("tmp")
        assert k2 != k and s.req("GET", "/v1/models", k2)[0] == 200
        listed = s.req("GET", "/keys?all=1", ADMIN)[1]["keys"]
        assert [x["name"] for x in listed] == ["tmp", "tmp"]
        assert sum(1 for x in listed if x["revoked_at"]) == 1
        assert all("key" not in x and len(x["key_prefix"]) == 16 for x in listed)   # never the secret


def test_admin_scoped_named_key_is_admin():
    with _Srv() as s:
        k = s.mint("dashboard", scope="admin")
        assert s.req("GET", "/status", k)[0] == 200
        assert s.req("GET", "/keys", k)[0] == 200


def test_validation():
    with _Srv() as s:
        assert s.req("POST", "/keys", ADMIN, {"name": "Bad Name"})[0] == 400
        assert s.req("POST", "/keys", ADMIN, {"name": "ok", "scope": "root"})[0] == 400
        assert s.req("POST", "/keys", ADMIN, {"name": ""})[0] == 400


def test_only_the_hash_is_stored():
    with _Srv() as s:
        k = s.mint("hashed")
        dump = "\n".join(s.store.conn.iterdump())
        assert k not in dump


def test_auth_does_not_wait_on_the_server_lock():
    # Endpoints may take the lock themselves; the auth DECISION must not. With the lock held
    # (a converge in progress), bad keys and out-of-scope keys are answered at once, and a
    # first use of a key (which wants to record last_used_at) does not block either.
    with _Srv() as s:
        k = s.mint("lockfree")
        s.handler.lock.acquire()
        try:
            t0 = time.time()
            assert s.req("GET", "/status", "not-a-key")[0] == 401
            assert s.req("POST", "/reload", k)[0] in (401, 403)
            assert s.req("DELETE", "/keys/lockfree", k)[0] == 403
            assert time.time() - t0 < 2.0
        finally:
            s.handler.lock.release()
        assert s.req("GET", "/v1/models", k)[0] == 200

def test_usage_records_the_key_dimension_and_facts_carry_it():
    from stackd.pricing import savings_facts
    with _Srv() as s:
        k = s.mint("agent-b")
        assert s.chat(k)[0] == 200 and s.chat(ADMIN)[0] == 200
        got = {r[0] for r in s.store.conn.execute("SELECT api_key FROM usage")}
        assert got == {"agent-b", "admin"}
        cfg = {"electricity_price_per_kwh": 0, "tiers": [], "currency": "USD"}
        facts = savings_facts(s.store, cfg)["facts"]
        by_key = {}
        for f in facts:
            by_key[f["api_key"]] = by_key.get(f["api_key"], 0) + f["reqs"]
        assert by_key.get("agent-b") == 1 and by_key.get("admin") == 1


def test_v10_migration_books_old_rows_to_admin_and_keeps_totals():
    import sqlite3
    import tempfile
    path = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(path)
    # a v9-shaped ledger: no api_key anywhere, PK without it
    c.executescript("""
        CREATE TABLE usage (id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, user_email TEXT,
            requested_model TEXT, served_stack TEXT, served_model TEXT, served_profile TEXT,
            off_home INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            cached_tokens INTEGER DEFAULT 0, ok INTEGER DEFAULT 1, ctx_tokens INTEGER DEFAULT 0);
        CREATE TABLE usage_daily (day TEXT, user_email TEXT, requested_model TEXT, served_profile TEXT,
            served_stack TEXT NOT NULL DEFAULT '?', served_model TEXT NOT NULL DEFAULT '?',
            reqs INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            cached_tokens INTEGER DEFAULT 0, ctx_tokens_sum INTEGER DEFAULT 0, ctx_tokens_sq_sum INTEGER DEFAULT 0,
            ctx_tokens_max INTEGER DEFAULT 0, ctx_n INTEGER DEFAULT 0,
            PRIMARY KEY (day, user_email, requested_model, served_profile, served_stack, served_model));
        INSERT INTO usage(ts,day,user_email,requested_model,prompt_tokens) VALUES (1,'2026-01-01','a@x','m',10);
        INSERT INTO usage_daily(day,user_email,requested_model,served_profile,reqs,prompt_tokens)
            VALUES ('2025-01-01','a@x','m','p',7,700), ('2025-01-02','b@x','m','p',3,300);
    """)
    c.close()
    st = Store.open(path)
    rows = st.conn.execute("SELECT api_key, SUM(reqs), SUM(prompt_tokens) FROM usage_daily GROUP BY 1").fetchall()
    assert [tuple(r) for r in rows] == [("admin", 10, 1000)]
    pk = [r[1] for r in st.conn.execute("PRAGMA table_info(usage_daily)") if r[5]]
    assert "api_key" in pk
    assert {r["api_key"] for r in st.usage_rows()} == {"admin"}       # raw NULL reads as admin
    Store.open(path)                                                    # idempotent


def test_brave_scope_reaches_only_brave_hit():
    with _Srv() as s:
        k = s.mint("searxng", scope="brave")
        code, _ = s.req("POST", "/brave/hit", k, {"engine": "braveapi"})
        assert code not in (401, 403), code
        assert s.req("GET", "/v1/models", k)[0] == 401
        assert s.chat(k)[0] == 401
        assert s.req("GET", "/status", k)[0] == 401


def test_trusted_key_keeps_forwarded_users_untrusted_does_not():
    with _Srv() as s:
        code, body = s.req("POST", "/keys", ADMIN, {"name": "open-webui", "trust_user_headers": True})
        assert code == 201 and body["trust_user_headers"] is True
        owu = body["key"]
        agent = s.mint("agent-c")
        assert s.chat(owu, headers={"X-OpenWebUI-User-Email": "kid@example.com"})[0] == 200
        assert s.chat(owu)[0] == 200                                   # no header -> key:<name>
        assert s.chat(agent, headers={"X-OpenWebUI-User-Email": "kid@example.com"})[0] == 200
        rows = {(r[0], r[1]) for r in s.store.conn.execute("SELECT user_email, api_key FROM usage")}
        assert ("kid@example.com", "open-webui") in rows
        assert ("key:open-webui", "open-webui") in rows
        assert ("key:agent-c", "agent-c") in rows and ("kid@example.com", "agent-c") not in rows
        listed = {k["name"]: k for k in s.req("GET", "/keys", ADMIN)[1]["keys"]}
        assert listed["open-webui"]["trust_user_headers"] is True and listed["agent-c"]["trust_user_headers"] is False


def test_mint_scope_is_honoured_only_by_the_toolbox_verifier():
    from stackd.serve import _named_key_can_mint
    with _Srv() as s:
        m = s.mint("owu-toolbox", scope="mint")
        i = s.mint("plain")
        a = s.mint("ops", scope="admin")
        assert _named_key_can_mint(m) and _named_key_can_mint(a)
        assert not _named_key_can_mint(i) and not _named_key_can_mint("nope") and not _named_key_can_mint("")
        assert s.req("GET", "/v1/models", m)[0] == 401                 # mint key can't infer
        assert s.req("GET", "/status", m)[0] == 401
        s.req("DELETE", "/keys/owu-toolbox", ADMIN)
        assert not _named_key_can_mint(m)                              # revocation reaches it too


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
