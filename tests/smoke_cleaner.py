"""P8 — the built-in ComfyUI scratch janitor (stackd/cleaner.py), ported from the
comfyui-cleaner container. Pure stdlib: temp dirs + a fake ComfyUI HTTP server.

    python3 tests/smoke_cleaner.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

from stackd.cleaner import Cleaner, prune_history, sweep_files  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))


def _touch(path: str, age_min: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")
    t = time.time() - age_min * 60
    os.utime(path, (t, t))


# --- fake ComfyUI: serves a fixed /history, records POSTed deletes ---------------
_DELETED: list = []


class _Comfy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    history_blob: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        raw = json.dumps(self.history_blob).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _DELETED.extend(body.get("delete", []))
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()


def main() -> int:
    scratch = tempfile.mkdtemp()
    try:
        # --- sweep_files --------------------------------------------------------
        _touch(os.path.join(scratch, "output", "old.png"), age_min=10)
        _touch(os.path.join(scratch, "temp", "old.latent"), age_min=5)
        _touch(os.path.join(scratch, "output", "fresh.png"), age_min=0.1)
        _touch(os.path.join(scratch, "input", "sub", "old_nested.png"), age_min=30)
        n = sweep_files(scratch, ttl_min=2)
        check("sweep removed 3 stale files", n == 3)
        check("fresh file kept", os.path.exists(os.path.join(scratch, "output", "fresh.png")))
        check("stale nested file removed", not os.path.exists(os.path.join(scratch, "input", "sub", "old_nested.png")))
        check("dirs left intact", os.path.isdir(os.path.join(scratch, "output")))
        check("missing scratch dir is a no-op", sweep_files("/nonesuch", 2) == 0)

        # --- prune_history ----------------------------------------------------
        _touch(os.path.join(scratch, "output", "live.png"), age_min=0)
        _Comfy.history_blob = {
            "p-errored": {"outputs": {}},  # 0 images -> prune
            "p-dangling": {"outputs": {"9": {"images": [
                {"filename": "gone.png", "subfolder": "", "type": "output"}]}}},  # file absent -> prune
            "p-live": {"outputs": {"9": {"images": [
                {"filename": "live.png", "subfolder": "", "type": "output"}]}}},  # file present -> keep
        }
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Comfy)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ep = f"http://127.0.0.1:{srv.server_address[1]}"
        _DELETED.clear()
        pruned = prune_history(ep, scratch)
        srv.shutdown()
        check("prune reported 2", pruned == 2)
        check("errored + dangling entries deleted", set(_DELETED) == {"p-errored", "p-dangling"})
        check("live entry NOT deleted", "p-live" not in _DELETED)

        # --- Cleaner toggle + counters --------------------------------------
        c = Cleaner(scratch, file_ttl_min=2, interval_s=1, enabled=True)
        check("enabled by default", c.enabled is True)
        c.set_enabled(False)
        check("toggled off", c.enabled is False and c.status()["enabled"] is False)
        c.set_enabled(True)
        _touch(os.path.join(scratch, "temp", "stale2.bin"), age_min=9)
        swept, _ = c.run_once(endpoint=None)
        check("run_once sweeps with no endpoint", swept == 1 and c.swept_total == 1)
        check("status carries totals + last_run", c.status()["swept_total"] == 1 and c.status()["last_run"])

        # --- HTTP routes: GET /cleaner, POST /cleaner/{on,off} ---------------
        import urllib.error
        import urllib.request

        from stackd.manager import Manager
        from stackd.runner import FakeRunner
        from stackd.serve import make_server

        CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
        st = pathlib.Path(tempfile.mkdtemp()) / "s.json"
        m = Manager(CFG, st, FakeRunner(ready_after=1))
        httpd = make_server(m, "127.0.0.1", 0, api_key="k", cleaner=c)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def _req(path, method="GET", tok="k"):
            r = urllib.request.Request(base + path, method=method)
            if tok:
                r.add_header("authorization", f"Bearer {tok}")
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, {}

        code, body = _req("/cleaner")
        check("GET /cleaner -> 200 + shape", code == 200 and "enabled" in body and "swept_total" in body)
        code, _ = _req("/cleaner", tok=None)
        check("GET /cleaner needs auth", code == 401)
        code, body = _req("/cleaner/off", method="POST")
        check("POST /cleaner/off disables", code == 200 and body.get("enabled") is False and c.enabled is False)
        code, body = _req("/cleaner/on", method="POST")
        check("POST /cleaner/on re-enables", code == 200 and body.get("enabled") is True and c.enabled is True)
        httpd.shutdown()
        st.unlink(missing_ok=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
