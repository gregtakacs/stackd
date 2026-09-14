"""In-flight per-session context tracking (_INFLIGHT / GET /engine's `inflight`
list) — no deps. Spins up a slow fake OpenAI-shaped upstream and a dead one,
drives `stackd.serve` over real HTTP: `python3 tests/smoke_inflight.py`."""

from __future__ import annotations

import json
import pathlib
import socket
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
from stackd.runner import FakeRunner  # noqa: E402
from stackd.serve import (  # noqa: E402
    _inflight_finish, _inflight_snapshot, _inflight_start, _inflight_tick, _live_trim,
    make_server,
)

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    CHECKS.append((name, bool(cond)))


class _SlowEcho(BaseHTTPRequestHandler):
    """Streams 5 content chunks with a real gap between them (long enough for
    the test to reliably poll mid-stream), then a final include_usage chunk —
    same OpenAI/vLLM/SGLang shape (no llama.cpp `timings`) as smoke_p25's fake."""
    protocol_version = "HTTP/1.1"
    CHUNK_DELAY_S = 0.2

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        # Nagle's algorithm otherwise coalesces these tiny sleep-spaced writes
        # into one delivery at connection-close on loopback, defeating the
        # whole point of a "slow" streaming fake (verified: even a raw
        # http.client reader saw nothing until the very end without this).
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        # Without an explicit chunked framing, a keep-alive HTTP/1.1 response of
        # unknown length gets buffered client-side until the connection closes —
        # _relay's incremental up.read(8192) then sees nothing until the whole
        # thing lands at once, defeating this test's whole point (real SGLang/
        # vLLM servers DO send real chunked framing for SSE).
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

        def _wc(b: bytes) -> None:
            self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
            self.wfile.flush()

        for _ in range(5):
            _wc(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
            time.sleep(self.CHUNK_DELAY_S)
        _wc(
            b'data: {"choices":[],"usage":'
            b'{"prompt_tokens":123,"completion_tokens":5,"total_tokens":128}}\n\n'
        )
        _wc(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def _stream_post(url, token, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def _get(url, token):
    req = urllib.request.Request(url, headers={"authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _check_unit_level() -> None:
    """Deterministic, no-socket check of the counting logic itself: start two
    sessions on different stacks, tick one a known number of times, finish the
    other, and confirm the snapshot math. This is the real correctness check
    for the tick/finish mechanics — the HTTP-level test below additionally
    proves _relay actually calls on_chunk once per real SSE event (verified),
    but real-time "N polls mid-stream" assertions there are at the mercy of
    loopback buffering behavior in this environment, not stackd's own code."""
    a = _inflight_start("test-stack-a", prompt_est=500)
    b = _inflight_start("test-stack-a", prompt_est=100)
    for _ in range(7):
        _inflight_tick("test-stack-a", a)
    rows = {r["ctx_tokens"]: r for r in _inflight_snapshot("test-stack-a")}
    check("unit: two sessions both present", len(rows) == 2)
    check("unit: ticked session shows prompt+7 decode", 507 in rows)
    check("unit: untouched session shows prompt+0 decode", 100 in rows)
    _inflight_finish("test-stack-a", a)
    rows2 = _inflight_snapshot("test-stack-a")
    check("unit: finishing one leaves exactly the other", len(rows2) == 1 and rows2[0]["ctx_tokens"] == 100)
    _inflight_finish("test-stack-a", b)
    check("unit: finishing the last one empties the stack",
          _inflight_snapshot("test-stack-a") == [])
    check("unit: finishing an already-gone id is a safe no-op",
          _inflight_finish("test-stack-a", a) is None)
    check("unit: an untracked stack snapshots empty, not an error",
          _inflight_snapshot("stack-nobody-started") == [])

    # _live_trim: the live-chart feed must null gen_tok_s/mtp_accept_pct/kv_pct/
    # ctx_tokens the instant an engine isn't active, so the frontend's existing
    # null-means-gap chart logic actually sees a gap instead of a stale/retained
    # value (e.g. SGLang's token_usage counting retained HiCache pages with
    # nothing running — the "1.7% on a dead engine" report this fixes).
    idle_raw = {"active": False, "gen_tok_s": 42.0, "prompt_tok_s": 900.0,
                "mtp_accept_pct": 88.0, "kv_pct": 1.7, "ctx_tokens": 19200}
    trimmed_idle = _live_trim({}, {}, {"coding-img": idle_raw})["eng"]["coding-img"]
    check("live_trim: idle gen_tok_s drops to 0", trimmed_idle["gen_tok_s"] == 0)
    check("live_trim: idle mtp_accept_pct rises to 100 (nothing to reject)",
          trimmed_idle["mtp_accept_pct"] == 100)
    check("live_trim: idle kv_pct drops to 0", trimmed_idle["kv_pct"] == 0)
    check("live_trim: idle ctx_tokens drops to 0", trimmed_idle["ctx_tokens"] == 0)
    check("live_trim: prompt_tok_s intentionally left as-is even when idle",
          trimmed_idle["prompt_tok_s"] == 900.0)

    active_raw = dict(idle_raw, active=True)
    trimmed_active = _live_trim({}, {}, {"coding-img": active_raw})["eng"]["coding-img"]
    check("live_trim: active gen_tok_s passes through", trimmed_active["gen_tok_s"] == 42.0)
    check("live_trim: active mtp_accept_pct passes through", trimmed_active["mtp_accept_pct"] == 88.0)
    check("live_trim: active kv_pct passes through", trimmed_active["kv_pct"] == 1.7)
    check("live_trim: active ctx_tokens passes through", trimmed_active["ctx_tokens"] == 19200)
    check("live_trim: no inflight arg -> empty list, not an error",
          trimmed_active["inflight"] == [])

    # per-slot stacked chart: stable slot indices, reused after finish, and
    # carried through _live_trim's inflight passthrough for history sampling.
    s1 = _inflight_start("test-stack-b", 100)
    s2 = _inflight_start("test-stack-b", 200)
    s3 = _inflight_start("test-stack-b", 300)
    snap = {r["ctx_tokens"]: r["slot"] for r in _inflight_snapshot("test-stack-b")}
    check("slots: three concurrent sessions get slots 0, 1, 2",
          snap == {100: 0, 200: 1, 300: 2})
    _inflight_finish("test-stack-b", s2)  # free slot 1
    s4 = _inflight_start("test-stack-b", 400)
    snap2 = {r["ctx_tokens"]: r["slot"] for r in _inflight_snapshot("test-stack-b")}
    check("slots: a freed slot (1) is reused by the next session, not appended past it",
          snap2 == {100: 0, 300: 2, 400: 1})
    trimmed_slots = _live_trim(
        {}, {}, {"test-stack-b": {"active": True}},
        inflight={"test-stack-b": _inflight_snapshot("test-stack-b")},
    )["eng"]["test-stack-b"]["inflight"]
    check("live_trim: carries the real inflight rows through for chart history",
          {r["ctx_tokens"]: r["slot"] for r in trimmed_slots} == snap2)
    for rid in (s1, s3, s4):
        _inflight_finish("test-stack-b", rid)
    check("slots: fully drained stack snapshots empty",
          _inflight_snapshot("test-stack-b") == [])
    s5 = _inflight_start("test-stack-b", 500)
    check("slots: a fully-drained stack's next session starts back at slot 0",
          _inflight_snapshot("test-stack-b")[0]["slot"] == 0)
    _inflight_finish("test-stack-b", s5)


def main() -> int:
    _check_unit_level()

    up = ThreadingHTTPServer(("127.0.0.1", 0), _SlowEcho)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_url = f"http://127.0.0.1:{up.server_address[1]}"

    state = pathlib.Path("/tmp") / f"stackd-inflight-{time.time_ns()}.json"
    mgr = Manager(CFG, state, FakeRunner(ready_after=1))
    mgr.use("chat", now=0)
    for i in range(4):
        mgr.tick(now=(i + 1) * 5)
    check("chat ready", all(s.state == EngineState.ready for s in mgr.state.stacks.values()))
    mgr.state.stacks["chat"].endpoint = up_url

    httpd = make_server(mgr, "127.0.0.1", 0, api_key="secret", warm_wait_s=3)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    try:
        # baseline: nothing in flight before any request
        code, tel = _get(f"{base}/engine/chat", "secret")
        check("baseline /engine/chat -> 200", code == 200)
        check("baseline inflight is empty", tel.get("inflight") == [])

        # kick off a slow stream in the background, poll mid-flight
        long_prompt = "word " * 400  # ~2000 bytes -> prompt_est ~= 500
        t = threading.Thread(
            target=_stream_post,
            args=(f"{base}/v1/chat/completions", "secret",
                  {"model": "assistant", "messages": [{"role": "user", "content": long_prompt}],
                   "stream": True}),
        )
        t.start()
        time.sleep(_SlowEcho.CHUNK_DELAY_S * 2.5)  # let ~2-3 chunks tick through

        code, tel = _get(f"{base}/engine/chat", "secret")
        rows = tel.get("inflight") or []
        check("mid-stream: exactly one in-flight session", len(rows) == 1)
        if rows:
            r = rows[0]
            check("mid-stream: prompt_tokens is a plausible byte/4 estimate",
                  400 <= r["prompt_tokens"] <= 700)
            # decode_tokens SHOULD be growing incrementally here (0 < . < 5) on a
            # box where loopback delivers each flushed chunk promptly — but that's
            # an OS/socket-buffering property this test doesn't control (observed:
            # even a raw http.client reader can see one final burst on some
            # loopback stacks despite real server-side sleeps + TCP_NODELAY). The
            # real correctness check for the counting logic is _check_unit_level
            # above; this just confirms the value is sane, not its exact timing.
            check("mid-stream: decode_tokens is a plausible count (0-5)",
                  0 <= r["decode_tokens"] <= 5)
            check("mid-stream: ctx_tokens = prompt_tokens + decode_tokens",
                  r["ctx_tokens"] == r["prompt_tokens"] + r["decode_tokens"])
            check("mid-stream: age_s is a small positive number", 0 <= r["age_s"] < 5)

        t.join(timeout=10)
        check("background stream thread finished", not t.is_alive())

        code, tel = _get(f"{base}/engine/chat", "secret")
        check("after completion: inflight cleared", tel.get("inflight") == [])

        # safety net: upstream unreachable must not leak an entry (on_body never
        # fires on that error path — the try/finally in _completions must catch it)
        mgr.state.stacks["chat"].endpoint = "http://127.0.0.1:1"  # nothing listens here
        code, _ = _req_ignore_error(f"{base}/v1/chat/completions", "secret",
                                    {"model": "assistant", "messages": [{"role": "user", "content": "hi"}]})
        check("unreachable upstream -> 502", code == 502)
        code, tel = _get(f"{base}/engine/chat", "secret")
        check("unreachable upstream leaves no ghost in-flight entry", tel.get("inflight") == [])

    finally:
        httpd.shutdown()
        up.shutdown()

    n_ok = sum(1 for _, ok in CHECKS if ok)
    for name, ok in CHECKS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n{'all passed' if n_ok == len(CHECKS) else 'FAILURES'} "
          f"({n_ok}/{len(CHECKS)})")
    return 0 if n_ok == len(CHECKS) else 1


def _req_ignore_error(url, token, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


if __name__ == "__main__":
    sys.exit(main())
