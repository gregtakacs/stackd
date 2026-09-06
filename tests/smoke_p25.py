"""P2.5 HTTP front checks — no deps. Spins up a fake OpenAI upstream that echoes
the request body, points a stack at it, and drives `stackd.serve` over real
HTTP: `python3 tests/smoke_p25.py`."""

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
from stackd.runner import FakeRunner  # noqa: E402
from stackd.serve import make_server  # noqa: E402

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    CHECKS.append((name, bool(cond)))


class _Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if body.get("stream"):
            # minimal OpenAI SSE with a final include_usage chunk, no llama.cpp `timings`
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for ln in (
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":21,"total_tokens":28}}\n\n',
                b"data: [DONE]\n\n",
            ):
                self.wfile.write(ln)
                self.wfile.flush()
                time.sleep(0.05)
            return
        payload = json.dumps({"echo": body, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _req(url, *, token=None, body=None):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers,
                               method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    # fake upstream
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_url = f"http://127.0.0.1:{up.server_address[1]}"

    state = pathlib.Path("/tmp") / f"stackd-p25-{time.time_ns()}.json"
    mgr = Manager(CFG, state, FakeRunner(ready_after=1))
    mgr.use("chat", now=0)
    for i in range(4):
        mgr.tick(now=(i + 1) * 5)
    check("chat ready",
          all(s.state == EngineState.ready for s in mgr.state.stacks.values()))
    mgr.state.stacks["chat"].endpoint = up_url

    httpd = make_server(mgr, "127.0.0.1", 0, api_key="secret", warm_wait_s=3)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    try:
        code, _ = _req(f"{base}/health")
        check("/health no auth -> 200", code == 200)

        code, _ = _req(f"{base}/v1/models")
        check("/v1/models without token -> 401", code == 401)

        code, models = _req(f"{base}/v1/models", token="secret")
        ids = {m["id"]: m for m in models.get("data", [])}
        check("/v1/models lists assistant + variants",
              "assistant" in ids and "assistant-xhigh" in ids and "assistant-coder" in ids)
        check("/v1/models carries real context_length",
              ids.get("assistant", {}).get("context_length") == 262144)

        code, res = _req(f"{base}/v1/chat/completions", token="secret",
                         body={"model": "assistant-xhigh", "messages": [{"role": "user", "content": "hi"}]})
        check("chat -> 200", code == 200)
        check("preset force-merged (reasoning_effort=xhigh)",
              res.get("echo", {}).get("reasoning_effort") == "xhigh")
        check("llamacpp: model name NOT rewritten",
              res.get("echo", {}).get("model") == "assistant-xhigh")
        check("proxied to the right upstream path",
              res.get("path") == "/v1/chat/completions")

        code, res = _req(f"{base}/v1/chat/completions", token="secret",
                         body={"model": "no-such-model", "messages": []})
        check("unknown model -> 404", code == 404)

        # reactive entry: agentic label outranks chat -> 503 warming (no tick thread here)
        code, res = _req(f"{base}/v1/chat/completions", token="secret",
                         body={"model": "assistant-coder", "messages": []})
        check("agentic request triggers entry -> 503 warming",
              code == 503 and res.get("error", {}).get("type") == "warming")
        check("reactive entry switched active profile to coding",
              mgr.state.active_profile == "coding")

        # bring coding up, point it at the echo upstream, retry as stand-in
        for i in range(4):
            mgr.tick(now=100 + (i + 1) * 5)
        mgr.state.stacks["coding"].endpoint = up_url
        code, res = _req(f"{base}/v1/chat/completions", token="secret",
                         body={"model": "assistant", "messages": []})
        check("stand-in serves chat name while coding active", code == 200)
        check("vllm stand-in: body model rewritten to the stack's served name (stack name by default)",
              res.get("echo", {}).get("model") == "coding")

        # --- Ollama timing injection for an OpenAI-shaped (vLLM/SGLang) stream ---
        # coding still points at the echo upstream; stream a request and confirm
        # the proxy adds a `stackd-timing` chunk with eval_count/eval_duration so
        # Open WebUI can show a tok/s rate.
        raw = b""
        r = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps({"model": "assistant", "messages": [], "stream": True,
                             "stream_options": {"include_usage": True}}).encode(),
            headers={"content-type": "application/json", "authorization": "Bearer secret"},
            method="POST")
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
        tline = next((l for l in raw.split(b"\n\n") if b"stackd-timing" in l), b"")
        tj = json.loads(tline[len(b"data: "):]) if tline else {}
        tu = tj.get("usage", {})
        check("timing chunk injected before [DONE]",
              b"stackd-timing" in raw and raw.rfind(b"stackd-timing") < raw.rfind(b"[DONE]"))
        check("timing chunk carries eval_count == completion_tokens", tu.get("eval_count") == 21)
        check("timing chunk carries a positive eval_duration (ns)",
              isinstance(tu.get("eval_duration"), int) and tu["eval_duration"] > 0)
        check("real usage chunk preserved untouched",
              b'"completion_tokens":21' in raw and b'"prompt_tokens":7' in raw)

        code, st = _req(f"{base}/profiles/chat/activate", token="secret", body={})
        check("control: /profiles/chat/activate -> 200", code == 200)
        check("control: active profile is chat", mgr.state.active_profile == "chat")
    finally:
        httpd.shutdown()
        up.shutdown()
        state.unlink(missing_ok=True)

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
