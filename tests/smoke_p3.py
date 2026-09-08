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


def _get(url, token=None, method="GET", body=None):
    data = None
    if method == "POST":
        data = json.dumps(body).encode() if body is not None else b"{}"
    r = urllib.request.Request(url, method=method, data=data)
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
    m.use("chat", now=0)
    ready(m)
    m.state.image.endpoint = up_url

    caps = m.capabilities()
    img = caps["image"]
    check("capabilities.image present", img is not None)
    check("elastic image tier picked dev-turbo on the RTX for chat",
          img["active_model"] == "flux2-dev-turbo" and img["device"] == "cuda0")
    check("image capabilities surfaced", "edit" in img["capabilities"])
    check("image serveable when ready", img["serveable"] is True)
    check("video slot empty (no video tier)", caps["video"] is None)
    check("engines() lists 2 LLM stacks + the image slot", len(m.engines()) == 3)

    # status(): the resident image tier's declared host_ram_gib folds into the
    # host_unified pool so the dashboard's reserved-vs-actual view is complete.
    ld = next(l for l in m.cfg.media["image"].prefer if l.active_model == "flux2-dev-turbo")
    ld.host_ram_gib = {"cuda": 40.0}
    stt = m.status()
    irb = stt["image_ram_budget"]
    check("status.image_ram_budget carries the resident entry's host_ram_gib",
          irb and irb["model"] == "flux2-dev-turbo" and irb["gib"] == 40.0)
    # The dashboard draws the allowance with the same GTT/RSS split on both bars,
    # and it can only do that if the payload separates them: host_ram_gib is the
    # TOTAL, footprint_gib the GPU part. None is a legal answer (older config, no
    # bench figure) — the lane then draws the whole allowance as GTT rather than
    # guessing — but the key must exist.
    check("status.image_ram_budget splits gib into footprint_gib for the RAM lane",
          irb and "footprint_gib" in irb
          and (irb["footprint_gib"] is None or 0 < irb["footprint_gib"] <= irb["gib"]))
    hup = next(p for p in stt["pools"] if p["pool"] == "host_unified")
    check("host_unified pool charges the image tier's host RAM",
          hup["breakdown"].get("image:flux2-dev-turbo") == 40.0
          and hup["used"] <= hup["limit"] + 1e-6)
    ld.host_ram_gib = {}
    check("no host_ram_gib -> image_ram_budget is None, pool unaffected",
          m.status()["image_ram_budget"] is None
          and "image:flux2-dev-turbo" not in
          next(p for p in m.status()["pools"] if p["pool"] == "host_unified")["breakdown"])

    # mid-swap contention: a co-device (cuda0) stack warming -> image not serveable
    m.state.stacks["chat"].state = EngineState.warming
    caps2 = m.capabilities()
    check("image not serveable while co-device warms", caps2["image"]["serveable"] is False)
    check("co_device_warming flagged", caps2["image"]["co_device_warming"] is True)
    ep, why = m.comfyui_target()
    check("comfyui_target refuses during co-device warm", ep is None and "warming" in why)
    m.state.stacks["chat"].state = EngineState.ready

    ep, why = m.comfyui_target()
    check("comfyui_target returns endpoint when serveable", ep == up_url)

    # switching to coding: vLLM claims the RTX, so the image tier rebuilds itself
    # as Flux Klein on the iGPU (derived from headroom, not declared)
    m.use("coding", now=1000)
    ready(m, t0=1000)
    cimg = m.capabilities()["image"]
    check("image tier rebuilt on the iGPU under coding",
          bool(cimg) and cimg["device"] == "igpu0" and cimg["backend"] == "vulkan")
    check("coding image active_model = flux2-klein", cimg["active_model"] == "flux2-klein")
    m.state.image.endpoint = up_url
    ep, why = m.comfyui_target()
    check("comfyui_target routes to the iGPU image engine under coding", ep == up_url)

    # back to chat, drive it over HTTP
    m.use("chat", now=2000)
    ready(m, t0=2000)
    m.state.image.endpoint = up_url

    # --- image: warm — forced generation on profile-ready, AND re-armed by a
    # manual /image/model swap (dashboard "Load" button), not just the first
    # model a profile ever lands on. ---
    from stackd.config.models import ImageSupport
    m.cfg.profiles["chat"].image = ImageSupport.warm
    m.state.warmed_for = None
    evs = m.tick(now=2005)
    check("warm-fire event once LLM + image tier are ready",
          any(e.action == "warm-fire" for e in evs))
    check("warmed_for keyed by profile:model", m.state.warmed_for == "chat:flux2-dev-turbo")
    evs2 = m.tick(now=2010)
    check("no duplicate warm-fire while resident model is unchanged",
          not any(e.action == "warm-fire" for e in evs2))
    m.set_image(model="flux2-klein")
    m.state.image.endpoint = up_url
    evs3 = m.tick(now=2015)
    check("manual swap re-arms warm-fire for the newly-picked model",
          any(e.action == "warm-fire" for e in evs3))
    check("warmed_for follows the swap", m.state.warmed_for == "chat:flux2-klein")
    m.cfg.profiles["chat"].image = ImageSupport.cold   # restore state for the rest of this file's checks
    m.set_image(model="flux2-dev-turbo")
    m.state.image.endpoint = up_url

    httpd = make_server(m, "127.0.0.1", 0, api_key="k", warm_wait_s=2)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, body = _get(f"{base}/capabilities", token="k")
        check("GET /capabilities -> 200",
              code == 200 and body["image"]["active_model"] == "flux2-dev-turbo")
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

        # --- elastic image tier control plane ---------------------------------
        code, body = _get(f"{base}/image", token="k")
        check("GET /image -> 200 + shape",
              code == 200 and body["resident"]["active_model"] == "flux2-dev-turbo"
              and "headroom_gib" in body and len(body["prefer"]) == 3)
        code, _ = _get(f"{base}/image")
        check("GET /image needs auth", code == 401)

        code, body = _get(f"{base}/image/model", token="k", method="POST",
                          body={"name": "flux2-klein"})
        check("POST /image/model swaps the pipeline",
              code == 200 and body["ok"] and body["active_model"] == "flux2-klein")
        ready(m, t0=2100)
        code, body = _get(f"{base}/image/capability", token="k", method="POST",
                          body={"need": "edit"})
        check("POST /image/capability (already covered) -> 200 ok",
              code == 200 and body["ok"])
        code, body = _get(f"{base}/image/model", token="k", method="POST",
                          body={"name": "no-such-pipeline"})
        check("POST /image/model unknown -> 409", code == 409 and body["ok"] is False)
        code, _ = _get(f"{base}/image/model", method="POST", body={"name": "flux2-klein"})
        check("POST /image/model needs auth", code == 401)

        # under coding, /comfyui follows to the iGPU Flux engine (not 503)
        m.use("coding", now=3000)
        ready(m, t0=3000)
        m.state.image.endpoint = up_url
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
