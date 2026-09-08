"""Long agentic turns under a reasoning parser (Qwen3 + qwen3_coder) can run the
completion budget out mid-tool-call: finish_reason="length" with the tool-call
`arguments` truncated or empty. Native-tool-call clients (Cline/Kilo) zod-validate the
tool call and reject it with a bare "Invalid input". stackd can't fix the client's
validator, but it can refuse to FORWARD a visibly-broken tool call: _relay_validated
detects it and re-issues the one upstream request once with a temporary, window-safe
larger output cap. These cover the detector, the budget math, and the relay end to end."""
import json
import pathlib
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import _env  # noqa: E402,F401  — config/${VAR} env for the Manager the relay test builds

from stackd.manager import Manager
from stackd.runner import FakeRunner
from stackd.serve import (_ctx_ceiling, _retry_body_with_budget,
                          _toolcall_truncated, make_server)

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
GOOD = json.dumps({"path": "a", "ln": 7})
TRUNC = '{"path": "a"'          # cut off — not valid JSON


def _ns(fr, args):
    return json.dumps({"choices": [{"finish_reason": fr,
                                    "message": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": args}}]}}]}).encode()


def _sse(parts):
    return b"".join(("data: " + json.dumps(p) + "\n\n").encode() for p in parts) + b"data: [DONE]\n\n"


def test_truncated_nonstream_detected():
    assert _toolcall_truncated(_ns("length", TRUNC)) is True


def test_complete_nonstream_not_flagged():
    assert _toolcall_truncated(_ns("tool_calls", GOOD)) is False


def test_argument_less_call_not_flagged():
    # a genuinely argument-less call ("") must NOT be retried
    assert _toolcall_truncated(_ns("tool_calls", "")) is False


def test_plain_text_not_flagged():
    buf = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}]}).encode()
    assert _toolcall_truncated(buf) is False


def test_truncated_sse_detected_across_deltas():
    s = _sse([{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": '{"path": "a"'}}]}}]},
              {"choices": [{"delta": {}, "finish_reason": "length"}]}])
    assert _toolcall_truncated(s) is True


def test_complete_sse_not_flagged():
    s = _sse([{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": GOOD}}]}}]},
              {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}])
    assert _toolcall_truncated(s) is False


def test_budget_sets_explicit_cap_when_absent():
    assert _retry_body_with_budget({"messages": []}, {"prompt_tokens": 90000}, 100000) == {"messages": [], "max_tokens": 9488}


def test_budget_refuses_when_window_full():
    assert _retry_body_with_budget({"max_tokens": 8192}, {"prompt_tokens": 99800}, 100000) is None


def test_budget_never_exceeds_window():
    # headroom exists (mml - prompt - 512 > cur), so the bump
    nb = _retry_body_with_budget({"max_tokens": 1024}, {"prompt_tokens": 200000}, 262144)
    assert nb is not None and nb["max_tokens"] + 200000 <= 262144 - 512
    # a tight window that can only shrink -> refuse to retry (None), never 400
    assert _retry_body_with_budget({"max_tokens": 1024}, {"prompt_tokens": 261600}, 262144) is None


def test_budget_preserves_max_completion_tokens_spelling():
    nb = _retry_body_with_budget({"max_completion_tokens": 1024}, {"prompt_tokens": 1000}, 262144)
    assert "max_completion_tokens" in nb and "max_tokens" not in nb and nb["max_completion_tokens"] > 1024


def test_ctx_ceiling_reads_params_and_cmd_extra():
    class M:
        class engine:
            params = {"max_model_len": 262144}
            class container:
                cmd_extra = []
    assert _ctx_ceiling(M) == 262144

    class C:
        class engine:
            params = {}
            class container:
                cmd_extra = ["--max-model-len", "131072"]
    assert _ctx_ceiling(C) == 131072
    assert _ctx_ceiling(None) is None


class _Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        b = json.loads(self.rfile.read(n) or b"{}")
        self.server.attempts.append(b.get("max_tokens", 0))
        trunc = b.get("max_tokens", 0) <= 1024        # small cap -> model truncates the call
        args, fr = (TRUNC, "length") if trunc else (GOOD, "tool_calls")
        if b.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            evs = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": args}}]}}]},
                   {"choices": [{"delta": {}, "finish_reason": fr}]}]
            for e in evs:
                self.wfile.write(("data: " + json.dumps(e) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        msg = {"choices": [{"index": 0, "finish_reason": fr,
                            "message": {"role": "assistant", "tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "edit", "arguments": args}}]}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": b.get("max_tokens", 0), "total_tokens": 10}}
        p = json.dumps(msg).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)


def _post(base, body):
    r = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                               headers={"content-type": "application/json", "authorization": "Bearer secret"}, method="POST")
    with urllib.request.urlopen(r, timeout=10) as resp:
        raw = resp.read()
        ct = resp.headers.get("content-type") or ""
        return ("sse", raw) if "event-stream" in ct else ("json", json.loads(raw or b"{}"))


def _serve():
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Up)
    up.attempts = []
    threading.Thread(target=up.serve_forever, daemon=True).start()
    mgr = Manager(CFG, pathlib.Path(f"/tmp/tcguard-{time.time_ns()}.json"), FakeRunner(ready_after=1))
    mgr.use("chat", now=0)
    for i in range(4):
        mgr.tick(now=(i + 1) * 5)
    mgr.state.stacks["chat"].endpoint = f"http://127.0.0.1:{up.server_address[1]}"
    httpd = make_server(mgr, "127.0.0.1", 0, api_key="secret", warm_wait_s=3)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return up, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _drive(streaming):
    up, httpd, base = _serve()
    try:
        body = {"model": "assistant", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1024, "tools": [{"type": "function", "function": {"name": "edit", "parameters": {}}}]}
        if streaming:
            body["stream"] = True
        return up.attempts, _post(base, body)
    finally:
        httpd.shutdown()
        up.shutdown()


def test_relay_retries_truncated_toolcall_once_nonstream():
    attempts, (kind, resp) = _drive(streaming=False)
    assert kind == "json"
    assert len(attempts) == 2 and attempts[1] > attempts[0]        # exactly one retry, bigger cap
    a = resp["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(a) == {"path": "a", "ln": 7}                 # client got a COMPLETE call


def test_relay_retries_truncated_toolcall_once_stream():
    attempts, (kind, raw) = _drive(streaming=True)
    assert kind == "sse"
    assert len(attempts) == 2 and attempts[1] > attempts[0]
    # Re-fold the delta tool-call arguments (escaping-agnostic) and confirm the client
    # would reassemble a WHOLE call — and never sees the truncated first attempt.
    args, frs = "", []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:") or line == b"data: [DONE]":
            continue
        obj = json.loads(line[5:].strip())
        for ch in obj.get("choices") or []:
            if ch.get("finish_reason"):
                frs.append(ch["finish_reason"])
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                args += (tc.get("function") or {}).get("arguments") or ""
    assert json.loads(args) == {"path": "a", "ln": 7}       # valid, complete
    assert "tool_calls" in frs and "length" not in frs      # forwarded completion not truncated
    assert "[DONE]" in raw.decode(errors="replace")


def test_relay_does_not_retry_complete_toolcall():
    # a whole tool call on the FIRST attempt must hit the upstream exactly once
    up, httpd, base = _serve()
    try:
        body = {"model": "assistant", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8192, "tools": [{"type": "function", "function": {"name": "edit", "parameters": {}}}]}
        kind, resp = _post(base, body)
        assert up.attempts == [8192]
        assert json.loads(resp["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"path": "a", "ln": 7}
    finally:
        httpd.shutdown()
        up.shutdown()
