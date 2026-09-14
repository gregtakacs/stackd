"""Long agentic turns under a reasoning parser (Qwen3 + qwen3_coder) can run the
completion budget out mid-tool-call: finish_reason="length" with the tool-call
`arguments` truncated or empty -- or, if reasoning alone ran long enough, no tool call
is ever even started. Native-tool-call clients (Cline/Kilo) zod-validate a truncated
call and reject it with a bare "Invalid input".

stackd can't fix the client's validator, but it refuses to forward a visibly-broken
call at all: _ToolCallGate (streaming) and _sanitize_toolcall_body (non-streaming) drop
it and report a clean finish_reason="length" turn with no tool_calls instead -- the
same shape a client already handles as "ran out of room". There is no retry (each
tool-capable model is configured with its own full native max_output_tokens, so there's
no bigger number left to ask for, and a second generation can't safely replace content
already streamed to a non-deterministic client anyway). These tests cover the detector,
the gate's streaming state machine directly, the non-streaming sanitizer, and the relay
end to end."""
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

from stackd import events
from stackd.manager import Manager
from stackd.runner import FakeRunner
from stackd.serve import (_ToolCallGate, _clean_toolcall_finish, _completion_toolcall_state,
                          _sanitize_toolcall_body, _toolcall_truncated, make_server)

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
GOOD = json.dumps({"path": "a", "ln": 7})
TRUNC = '{"path": "a"'          # cut off — not valid JSON


def _ns(fr, args):
    return json.dumps({"choices": [{"finish_reason": fr,
                                    "message": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": args}}]}}]}).encode()


def _sse(parts):
    return b"".join(("data: " + json.dumps(p) + "\n\n").encode() for p in parts) + b"data: [DONE]\n\n"


# ---- _completion_toolcall_state / _toolcall_truncated (unchanged by the redesign) ----

def test_truncated_nonstream_detected():
    assert _toolcall_truncated(_ns("length", TRUNC)) is True


def test_complete_nonstream_not_flagged():
    assert _toolcall_truncated(_ns("tool_calls", GOOD)) is False


def test_argument_less_call_not_flagged():
    # a genuinely argument-less call ("") must NOT be flagged
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


# ---- _clean_toolcall_finish -----------------------------------------------------

def test_clean_toolcall_finish_strips_tool_calls_and_forces_length():
    doc = {"id": "x", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0}]}, "finish_reason": "length"}]}
    out = _clean_toolcall_finish(doc)
    assert out["choices"][0]["delta"] == {}
    assert out["choices"][0]["finish_reason"] == "length"
    assert out["id"] == "x"                       # other top-level fields preserved


def test_clean_toolcall_finish_handles_no_choices():
    out = _clean_toolcall_finish({"id": "x", "choices": []})
    assert out["choices"] == [{"index": 0, "delta": {}, "finish_reason": "length"}]


# ---- _ToolCallGate: the streaming state machine, fed directly -------------------

def _feed(gate, events_in):
    out = []
    for e in events_in:
        out.extend(gate.handle((b"data: " + json.dumps(e).encode() + b"\n\n") if e != "[DONE]" else b"data: [DONE]\n\n"))
    return out


def test_gate_streams_content_live_before_any_toolcall():
    gate = _ToolCallGate("m")
    out = _feed(gate, [{"choices": [{"index": 0, "delta": {"content": "hi"}}]}])
    assert len(out) == 1 and b'"content": "hi"' in out[0]      # relayed immediately, not held


def test_gate_holds_from_first_toolcall_delta_until_terminal():
    gate = _ToolCallGate("m")
    out1 = _feed(gate, [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": ""}}]}}]}])
    assert out1 == []                                          # held, nothing relayed yet
    out2 = _feed(gate, [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": GOOD}}]}}]}])
    assert out2 == []                                          # still held


def test_gate_flushes_everything_held_on_clean_finish():
    gate = _ToolCallGate("m")
    before = events.snapshot()["last_seq"]
    out = _feed(gate, [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": GOOD}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])
    assert len(out) == 2                                       # both held events flushed, in order
    assert b'"arguments"' in out[0]
    assert b'"finish_reason": "tool_calls"' in out[1]
    assert not any(e["kind"].startswith("toolcall-") for e in events.snapshot(since_seq=before)["events"])


def test_gate_swallows_and_logs_on_length_with_open_call():
    gate = _ToolCallGate("m")
    before = events.snapshot()["last_seq"]
    out = _feed(gate, [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": TRUNC}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
    ])
    assert len(out) == 1                                       # one synthetic event, not the broken one
    doc = json.loads(out[0].strip()[len(b"data:"):])
    assert doc["choices"][0]["finish_reason"] == "length"
    assert doc["choices"][0]["delta"] == {}
    new = events.snapshot(since_seq=before)["events"]
    assert any(e["kind"] == "toolcall-swallowed" and e["stack"] == "m" for e in new)


def test_gate_logs_unstarted_length_without_touching_the_stream():
    gate = _ToolCallGate("m")
    before = events.snapshot()["last_seq"]
    out = _feed(gate, [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "thinking..."}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
    ])
    assert len(out) == 2                                       # both relayed live, untouched
    new = events.snapshot(since_seq=before)["events"]
    assert any(e["kind"] == "toolcall-unstarted-length" and e["stack"] == "m" for e in new)


def test_gate_events_after_resolution_pass_through_untouched():
    gate = _ToolCallGate("m")
    _feed(gate, [
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": GOOD}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ])
    out = _feed(gate, [{"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}])
    assert len(out) == 1 and b'"usage"' in out[0]


# ---- _sanitize_toolcall_body: the non-streaming counterpart ----------------------

def test_sanitize_rewrites_a_truncated_body():
    before = events.snapshot()["last_seq"]
    out = _sanitize_toolcall_body(_ns("length", TRUNC), "m")
    assert out is not None
    doc = json.loads(out)
    assert doc["choices"][0]["message"]["tool_calls"] == []
    assert doc["choices"][0]["finish_reason"] == "length"
    new = events.snapshot(since_seq=before)["events"]
    assert any(e["kind"] == "toolcall-swallowed" and e["stack"] == "m" for e in new)


def test_sanitize_leaves_a_complete_body_untouched():
    assert _sanitize_toolcall_body(_ns("tool_calls", GOOD), "m") is None


def test_sanitize_logs_unstarted_length_without_rewriting():
    before = events.snapshot()["last_seq"]
    buf = json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "..."}}]}).encode()
    assert _sanitize_toolcall_body(buf, "m") is None
    new = events.snapshot(since_seq=before)["events"]
    assert any(e["kind"] == "toolcall-unstarted-length" and e["stack"] == "m" for e in new)


# ---- end to end, over real HTTP --------------------------------------------------

class _Up(BaseHTTPRequestHandler):
    """Fake upstream. `_scenario` in the request body picks the shape of the
    response: "complete" (a whole tool call), "truncated" (cut off mid-arguments,
    finish_reason=length), or "reasoning_only" (hits length before any tool call)."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        b = json.loads(self.rfile.read(n) or b"{}")
        self.server.attempts.append(b.get("max_tokens", 0))
        scenario = b.get("_scenario", "complete")

        if b.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            evs = []
            if scenario == "reasoning_only":
                evs = [{"choices": [{"index": 0, "delta": {"reasoning_content": "thinking..."}}]},
                       {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}]
            else:
                args = GOOD if scenario == "complete" else TRUNC
                fr = "tool_calls" if scenario == "complete" else "length"
                evs = [{"choices": [{"index": 0, "delta": {"content": "on it\n"}}]},
                       {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "edit", "arguments": args}}]}}]},
                       {"choices": [{"index": 0, "delta": {}, "finish_reason": fr}]}]
            for e in evs:
                self.wfile.write(("data: " + json.dumps(e) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b'data: {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        if scenario == "reasoning_only":
            msg = {"choices": [{"index": 0, "finish_reason": "length",
                                "message": {"role": "assistant", "content": "thinking..."}}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        else:
            args = GOOD if scenario == "complete" else TRUNC
            fr = "tool_calls" if scenario == "complete" else "length"
            msg = {"choices": [{"index": 0, "finish_reason": fr,
                                "message": {"role": "assistant", "tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "edit", "arguments": args}}]}}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
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


def _drive(scenario, streaming):
    up, httpd, base = _serve()
    try:
        body = {"model": "assistant", "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 32768, "_scenario": scenario,
                "tools": [{"type": "function", "function": {"name": "edit", "parameters": {}}}]}
        if streaming:
            body["stream"] = True
        return up.attempts, _post(base, body)
    finally:
        httpd.shutdown()
        up.shutdown()


def test_relay_forwards_a_complete_toolcall_nonstream():
    attempts, (kind, resp) = _drive("complete", streaming=False)
    assert kind == "json"
    assert attempts == [32768]                                 # exactly one upstream hit, no retry
    a = resp["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(a) == {"path": "a", "ln": 7}


def test_relay_swallows_a_truncated_toolcall_nonstream():
    attempts, (kind, resp) = _drive("truncated", streaming=False)
    assert kind == "json"
    assert attempts == [32768]                                 # still exactly one hit — no retry
    ch = resp["choices"][0]
    assert ch["finish_reason"] == "length"
    assert ch["message"]["tool_calls"] == []                   # broken call dropped, not forwarded


def test_relay_forwards_a_complete_toolcall_stream():
    attempts, (kind, raw) = _drive("complete", streaming=True)
    assert kind == "sse"
    assert attempts == [32768]
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
    assert json.loads(args) == {"path": "a", "ln": 7}
    assert "tool_calls" in frs and "length" not in frs
    assert b'"content": "on it' in raw                          # pre-call content DID stream live
    assert "[DONE]" in raw.decode(errors="replace")


def test_relay_swallows_a_truncated_toolcall_stream():
    before = events.snapshot()["last_seq"]
    attempts, (kind, raw) = _drive("truncated", streaming=True)
    assert kind == "sse"
    assert attempts == [32768]
    assert TRUNC.encode() not in raw                            # the broken fragment never reached the wire
    frs = []
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:") or line == b"data: [DONE]":
            continue
        obj = json.loads(line[5:].strip())
        for ch in obj.get("choices") or []:
            if ch.get("finish_reason"):
                frs.append(ch["finish_reason"])
    assert frs == ["length"]
    assert b'"content": "on it' in raw                          # pre-call content still streamed live
    new = events.snapshot(since_seq=before)["events"]
    assert any(e["kind"] == "toolcall-swallowed" for e in new)


def test_relay_forwards_reasoning_only_length_untouched():
    # tools declared, but the model never started one before hitting length --
    # nothing for the gate/sanitizer to rewrite, just observe and pass through.
    attempts, (kind, resp) = _drive("reasoning_only", streaming=False)
    assert kind == "json"
    assert resp["choices"][0]["finish_reason"] == "length"
    assert "tool_calls" not in resp["choices"][0]["message"]
    assert resp["choices"][0]["message"]["content"] == "thinking..."
