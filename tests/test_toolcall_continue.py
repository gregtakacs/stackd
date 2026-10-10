"""The continuation retries for a MALFORMED tool call (SGLang upstreams only): a
call that closed (finish_reason != "length") but whose arguments are bad JSON /
self-wrapped / off-schema is not forwarded; instead stackd re-asks the engine with
`continue_final_message`, prefixing exactly the reasoning + content the client has
already seen, and relays the first well-formed retry's call in its place. A truncated
call is never retried (still swallowed as "length"); a malformed one that no retry
repairs ends finish_reason="stop" with a note saying what was wrong."""
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import _env  # noqa: E402,F401

from stackd import events
from stackd.serve import (_TOOLCALL_CONTINUE_ATTEMPTS, _ToolCallGate, _completion_toolcall_state,
                          _continuation_prefix, _continue_toolcall, _sanitize_toolcall_body)

TOOLS = [{"type": "function", "function": {
    "name": "edit", "parameters": {"properties": {"path": {}, "ln": {}}, "required": ["path"]}}}]
GOOD = json.dumps({"path": "a", "ln": 7})
OFF_SCHEMA = json.dumps({"path__01": "a"})          # valid JSON, wrong shape
TRUNC = '{"path": "a"'
BODY = {"model": "m", "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "hi"}], "tools": TOOLS, "max_tokens": 4096}


class _Up(BaseHTTPRequestHandler):
    """Fake SGLang for the continuation request. `server.reply` picks the answer:
    "good" (a well-formed call), "bad" (off-schema again), "length", or "500" — or
    a list of those, consumed one per request (the last one repeats)."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.server.seen.append(json.loads(self.rfile.read(n) or b"{}"))
        reply = self.server.reply
        if isinstance(reply, list):
            reply = reply[min(len(self.server.seen) - 1, len(reply) - 1)]
        if reply == "500":
            p = b'{"error": "boom"}'
            self.send_response(500)
        else:
            args = GOOD if reply in ("good", "length") else OFF_SCHEMA
            fr = "length" if reply == "length" else "tool_calls"
            p = json.dumps({"choices": [{"index": 0, "finish_reason": fr, "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_r", "type": "function",
                                "function": {"name": "edit", "arguments": args}}]}}]}).encode()
            self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)


def _up(reply):
    s = ThreadingHTTPServer(("127.0.0.1", 0), _Up)
    s.reply, s.seen = reply, []
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}/v1/chat/completions"


def _sse(e):
    return (b"data: " + json.dumps(e).encode() + b"\n\n") if e != "[DONE]" else b"data: [DONE]\n\n"


def _stream(call_args, fr="tool_calls"):
    return [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "plan the edit"}}]},
        {"choices": [{"index": 0, "delta": {"content": "Editing now."}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c0", "type": "function",
                                                            "function": {"name": "edit", "arguments": call_args}}]}}]},
        {"id": "x", "choices": [{"index": 0, "delta": {}, "finish_reason": fr}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        "[DONE]",
    ]


def _run(gate, evs):
    out = []
    for e in evs:
        out.extend(gate.handle(_sse(e)))
    return [json.loads(o[5:].strip()) for o in out if o.strip() != b"data: [DONE]"]


# ---- prefix shape ---------------------------------------------------------------------

def test_prefix_closes_think_block_like_the_template():
    assert _continuation_prefix("\nplan\n", "\n\nok") == "plan\n</think>\n\nok"


def test_prefix_without_reasoning_is_just_content():
    assert _continuation_prefix("", "ok") == "ok"
    assert _continuation_prefix("", "") == ""


# ---- _continue_toolcall ---------------------------------------------------------------

def test_continue_request_shape():
    s, url = _up("good")
    try:
        msg, problems = _continue_toolcall(url, BODY, "plan", "Editing now.", TOOLS, "m")
    finally:
        s.shutdown()
    assert msg and msg["tool_calls"][0]["function"]["arguments"] == GOOD
    assert problems == [] and len(s.seen) == 1
    sent = s.seen[0]
    assert sent["stream"] is False and "stream_options" not in sent
    assert sent["continue_final_message"] is True
    assert sent["separate_reasoning"] is False
    assert sent["messages"][-1] == {"role": "assistant", "content": "plan\n</think>\n\nEditing now."}
    assert BODY["messages"][-1]["role"] == "user"                # caller's body untouched


def test_continue_with_no_prefix_is_a_plain_rerun():
    s, url = _up("good")
    try:
        _continue_toolcall(url, BODY, "", "", TOOLS, "m")
    finally:
        s.shutdown()
    sent = s.seen[0]
    assert "continue_final_message" not in sent and "separate_reasoning" not in sent
    assert sent["messages"][-1]["role"] == "user"


def test_continue_retries_bad_up_to_the_limit_and_reports_why():
    s, url = _up("bad")
    try:
        before = events.snapshot()["last_seq"]
        msg, problems = _continue_toolcall(url, BODY, "plan", "", TOOLS, "m")
    finally:
        s.shutdown()
    assert msg is None
    assert len(s.seen) == _TOOLCALL_CONTINUE_ATTEMPTS
    assert problems and "unknown params ['path__01']" in problems[0]
    fails = [e for e in events.snapshot(since_seq=before)["events"] if e["kind"] == "toolcall-continue-failed"]
    assert len(fails) == _TOOLCALL_CONTINUE_ATTEMPTS
    assert "edit: unknown params" in fails[0]["detail"]


def test_continue_succeeds_on_a_later_attempt():
    s, url = _up(["bad", "good"])
    try:
        msg, problems = _continue_toolcall(url, BODY, "plan", "", TOOLS, "m")
    finally:
        s.shutdown()
    assert msg and msg["tool_calls"][0]["function"]["arguments"] == GOOD
    assert len(s.seen) == 2


def test_continue_stops_early_on_length_or_upstream_error():
    for reply in ("length", "500"):
        s, url = _up(reply)
        try:
            msg, _ = _continue_toolcall(url, BODY, "plan", "", TOOLS, "m")
        finally:
            s.shutdown()
        assert msg is None and len(s.seen) == 1, reply


# ---- streaming gate ---------------------------------------------------------------------

def test_gate_replaces_malformed_call_with_continued_one():
    s, url = _up("good")
    try:
        before = events.snapshot()["last_seq"]
        out = _run(_ToolCallGate("m", TOOLS, continuation=(url, BODY)), _stream(OFF_SCHEMA))
    finally:
        s.shutdown()
    # reasoning + content streamed live, then the retry's call, then usage
    assert out[0]["choices"][0]["delta"] == {"reasoning_content": "plan the edit"}
    assert out[1]["choices"][0]["delta"] == {"content": "Editing now."}
    fin = out[2]["choices"][0]
    assert fin["finish_reason"] == "tool_calls"
    assert fin["delta"]["tool_calls"][0]["function"]["arguments"] == GOOD
    assert out[2]["id"] == "x"
    assert "usage" in out[3]
    assert s.seen[0]["messages"][-1]["content"] == "plan the edit\n</think>\n\nEditing now."
    assert any(e["kind"] == "toolcall-continued" for e in events.snapshot(since_seq=before)["events"])


def test_gate_ends_with_stop_and_a_note_when_retries_fail():
    s, url = _up("bad")
    try:
        out = _run(_ToolCallGate("m", TOOLS, continuation=(url, BODY)), _stream(OFF_SCHEMA))
    finally:
        s.shutdown()
    ch = out[2]["choices"][0]
    assert ch["finish_reason"] == "stop"
    assert "tool_calls" not in ch["delta"]
    note = ch["delta"]["content"]
    assert note.startswith("\n\n[stackd: dropped a malformed tool call")
    assert "edit: unknown params ['path__01']" in note
    assert "usage" in out[3]


def test_gate_never_retries_a_truncated_call():
    s, url = _up("good")
    try:
        out = _run(_ToolCallGate("m", TOOLS, continuation=(url, BODY)), _stream(TRUNC, fr="length"))
    finally:
        s.shutdown()
    assert s.seen == []
    assert out[2]["choices"][0]["finish_reason"] == "length"


def test_gate_without_continuation_still_stops_with_a_note():
    # non-SGLang engines get no retry, but the honest fallback all the same
    before = events.snapshot()["last_seq"]
    out = _run(_ToolCallGate("m", TOOLS), _stream(OFF_SCHEMA))
    assert out[2]["choices"][0]["finish_reason"] == "stop"
    assert "path__01" in out[2]["choices"][0]["delta"]["content"]
    new = events.snapshot(since_seq=before)["events"]
    assert any(e["kind"] == "toolcall-malformed" and "path__01" in e["detail"] for e in new)


def test_problem_descriptions():
    def probs(args):
        return _completion_toolcall_state(json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"function": {"name": "edit", "arguments": args}}]}}]}).encode(),
            TOOLS, with_problems=True)[2]
    assert probs(GOOD) == []
    assert "unparseable JSON" in probs(TRUNC)[0]
    assert "self-wrapped in {arguments}" in probs(json.dumps({"arguments": GOOD}))[0]
    assert "missing required params ['path']" in probs(json.dumps({"ln": 1}))[0]


# ---- non-streamed -------------------------------------------------------------------------

def _body(args, fr="tool_calls"):
    return json.dumps({"choices": [{"index": 0, "finish_reason": fr, "message": {
        "role": "assistant", "reasoning_content": "plan", "content": "Editing now.",
        "tool_calls": [{"id": "c0", "type": "function", "function": {"name": "edit", "arguments": args}}]}}]}).encode()


def test_sanitize_continues_a_malformed_body():
    s, url = _up("good")
    try:
        out = json.loads(_sanitize_toolcall_body(_body(OFF_SCHEMA), "m", TOOLS, continuation=(url, BODY)))
    finally:
        s.shutdown()
    ch = out["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    assert ch["message"]["tool_calls"][0]["function"]["arguments"] == GOOD
    assert ch["message"]["content"] == "Editing now."
    assert s.seen[0]["messages"][-1]["content"] == "plan\n</think>\n\nEditing now."


def test_sanitize_stops_with_a_note_when_retries_fail():
    s, url = _up("bad")
    try:
        out = json.loads(_sanitize_toolcall_body(_body(OFF_SCHEMA), "m", TOOLS, continuation=(url, BODY)))
    finally:
        s.shutdown()
    ch = out["choices"][0]
    assert ch["finish_reason"] == "stop" and ch["message"]["tool_calls"] == []
    assert ch["message"]["content"].startswith("Editing now.\n\n[stackd: dropped a malformed tool call")


def test_sanitize_does_not_retry_truncated_body():
    s, url = _up("good")
    try:
        out = json.loads(_sanitize_toolcall_body(_body(TRUNC, fr="length"), "m", TOOLS, continuation=(url, BODY)))
    finally:
        s.shutdown()
    assert s.seen == []
    assert out["choices"][0]["finish_reason"] == "length"
    assert out["choices"][0]["message"]["tool_calls"] == []
