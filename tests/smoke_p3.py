"""P3 — comfyui-mcp-facing capability surface + /comfyui passthrough + the
mid-swap contention guard. No deps: `python3 tests/smoke_p3.py`."""

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


def check(name, cond):
    CHECKS.append((name, bool(cond)))


class _Comfy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._reply({"system": "comfyui-fake", "path": self.path})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self._reply({"queued": True, "echo": json.loads(self.rfile.read(n) or b"{}")})


def _get(url, token=None, method="GET"):
    r = urllib.request.Request(url, method=method,
                               data=(b"{}" if method == "POST" else None))
    if token:
        r.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def ready(m, t0=0.0):
    for i in range(4):
        m.tick(now=t0 + (i + 1) * 5)


def main() -> int:
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Comfy)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_url = f"http://127.0.0.1:{up.server_address[1]}"

    state = pathlib.Path("/tmp") / f"stackd-p3-{time.time_ns()}.json"
    m = Manager(CFG, state, FakeRunner(ready_after=1))
    m.use("everyday", now=0)
    ready(m)
    m.state.stacks["everyday-image"].endpoint = up_url

    caps = m.capabilities()
    img = caps["image"]
    check("capabilities.image present", img is not None)
    check("everyday image active_model = flux2-dev-turbo (RTX)", img["active_model"] == "flux2-dev-turbo")
    check("image workflow_templates surfaced", "flux-inpaint" in img["workflow_templates"])
    check("image serveable when ready", img["serveable"] is True)
    check("video slot empty (no video stack)", caps["video"] is None)
    check("engines() lists all 3 running stacks", len(m.engines()) == 3)

    # mid-swap contention: a co-device (cuda0) stack warming -> image not serveable
    m.state.stacks["everyday-chat"].state = EngineState.warming
    caps2 = m.capabilities()
    check("image not serveable while co-device warms", caps2["image"]["serveable"] is False)
    check("co_device_warming flagged", caps2["image"]["co_device_warming"] is True)
    ep, why = m.comfyui_target()
    check("comfyui_target refuses during co-device warm", ep is None and "warming" in why)
    m.state.stacks["everyday-chat"].state = EngineState.ready

    ep, why = m.comfyui_target()
    check("comfyui_target returns endpoint when serveable", ep == up_url)

    # coding carries its own image engine (Flux Klein on the iGPU)
    m.use("coding", now=1000)
    ready(m, t0=1000)
    cimg = m.capabilities()["image"]
    check("coding image engine is the iGPU stack", bool(cimg) and cimg["stack"] == "coding-image")
    check("coding image engine is on igpu0", cimg["device"] == "igpu0")
    check("coding image active_model = flux2-klein", cimg["active_model"] == "flux2-klein")
    m.state.stacks["coding-image"].endpoint = up_url
    ep, why = m.comfyui_target()
    check("comfyui_target routes to the iGPU image engine under coding", ep == up_url)

    # back to everyday, drive it over HTTP
    m.use("everyday", now=2000)
    ready(m, t0=2000)
    m.state.stacks["everyday-image"].endpoint = up_url
    httpd = make_server(m, "127.0.0.1", 0, api_key="k", warm_wait_s=2)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, body = _get(f"{base}/capabilities", token="k")
        check("GET /capabilities -> 200", code == 200 and body["image"]["stack"] == "everyday-image")
        code, _ = _get(f"{base}/capabilities")
        check("GET /capabilities needs auth", code == 401)
        code, body = _get(f"{base}/engines", token="k")
        check("GET /engines -> 200", code == 200 and len(body["engines"]) == 3)
        code, body = _get(f"{base}/comfyui/system_stats", token="k")
        check("GET /comfyui/* proxies to ComfyUI when serveable",
              code == 200 and body.get("path") == "/system_stats")

        # POST /reload — re-reads config + re-converges the active profile
        code, body = _get(f"{base}/reload", method="POST")
        check("POST /reload needs auth", code == 401)
        code, body = _get(f"{base}/reload", token="k", method="POST")
        check("POST /reload -> 200 + shape",
              code == 200 and "reloaded" in body and "status" in body)

        # under coding, /comfyui follows to the iGPU Flux engine (not 503)
        m.use("coding", now=3000)
        ready(m, t0=3000)
        m.state.stacks["coding-image"].endpoint = up_url
        code, body = _get(f"{base}/comfyui/system_stats", token="k")
        check("GET /comfyui/* under coding routes to the iGPU image engine",
              code == 200 and body.get("path") == "/system_stats")
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
