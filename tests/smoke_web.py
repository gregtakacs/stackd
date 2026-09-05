"""Web UI backend — the dashboard shell, static assets, and the JSON endpoints
it polls (/events, /gpu, /history, /profiles, /slots). Stdlib only."""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402

from stackd import events, telemetry  # noqa: E402
from stackd.manager import Manager  # noqa: E402
from stackd.runner import FakeRunner  # noqa: E402
from stackd.serve import make_server  # noqa: E402
from stackd.store import Store, _utc_day  # noqa: E402

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
CHECKS: list[tuple[str, bool]] = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))


def _req(url, *, token=None, method="GET", body=None):
    h = {}
    if token:
        h["authorization"] = f"Bearer {token}"
    if body is not None:
        h["content-type"] = "application/json"
    r = urllib.request.Request(
        url, data=(json.dumps(body).encode() if body is not None else None),
        headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            return resp.status, resp.headers.get("content-type", ""), raw
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read()


def _json_req(url, **kw):
    code, _ct, raw = _req(url, **kw)
    try:
        return code, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return code, {"_text": raw.decode(errors="replace")}


def main() -> int:
    tmp = pathlib.Path(__import__("tempfile").mkdtemp())

    # canned nvidia-smi so /gpu has a cuda0 block without a real GPU
    telemetry._cache.update(at=0.0, data=None)
    import stackd.runner as _runner
    _runner._nvidia_smi = lambda *a, **k: "12345, 98304, 42, 210.5, 600, 61\n"

    store = Store.open(str(tmp / "web.db"))
    day = _utc_day()
    store.record_usage(user_email="u@x.com", requested_model="assistant",
                       served_stack="chat", served_model="Qwen3.8-27B-UD-Q4_K_XL",
                       served_profile="chat",
                       prompt_tokens=2000, completion_tokens=800, cached_tokens=200)
    store.add_energy(day, gpu_wh=120, host_wh=90)

    mgr = Manager(CFG, tmp / "state.json", FakeRunner(ready_after=1))
    mgr.use("chat", now=0)
    for i in range(4):
        mgr.tick(now=(i + 1) * 5)

    httpd = make_server(mgr, "127.0.0.1", 0, api_key="admin", warm_wait_s=2,
                        store=store, pricing_path=str(CFG / "pricing.json"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # --- shell + static (no auth) --------------------------------------------
        code, ct, raw = _req(f"{base}/")
        check("GET / serves the dashboard shell", code == 200 and b"<title>stackd" in raw)
        check("GET / is html", ct.startswith("text/html"))
        code, ct, raw = _req(f"{base}/static/uplot.min.js")
        check("GET /static/uplot.min.js served", code == 200 and b"uPlot" in raw)
        check("static content-type is js", "javascript" in ct)
        code, _ct, _raw = _req(f"{base}/static/../serve.py")
        check("static path traversal blocked", code == 404)

        # --- auth gate on data endpoints ---------------------------------------
        code, _ = _json_req(f"{base}/events")
        check("/events needs a token", code == 401)
        code, _ = _json_req(f"{base}/profiles")
        check("/profiles needs a token", code == 401)

        # --- /events -----------------------------------------------------------
        code, j = _json_req(f"{base}/events", token="admin")
        check("/events shape", code == 200 and isinstance(j.get("events"), list) and "last_seq" in j)
        before = j["last_seq"]
        code, _ = _json_req(f"{base}/profiles/coding/activate", token="admin", method="POST", body={})
        check("activate coding -> 200", code == 200)
        time.sleep(0.1)
        code, j = _json_req(f"{base}/events?since={before}", token="admin")
        check("activate produced new events", code == 200 and len(j["events"]) >= 1)
        check("new events carry a source", all(e.get("source") for e in j["events"]))

        # --- /slots + /engine (coding is active -> coding resident) ---
        code, j = _json_req(f"{base}/slots/does-not-exist", token="admin")
        check("/slots on an unknown stack -> 404", code == 404)
        code, j = _json_req(f"{base}/slots/coding", token="admin")
        check("/slots on a vLLM stack -> unsupported (not an error)",
              code == 200 and j.get("unsupported") is True)
        code, j = _json_req(f"{base}/engine/does-not-exist", token="admin")
        check("/engine on an unknown stack -> 404", code == 404)
        code, j = _json_req(f"{base}/engine/coding", token="admin")
        check("/engine returns a normalised (all-None on an unreachable engine) block",
              code == 200 and j.get("template", "").startswith("vllm")
              and "gen_tok_s" in j and "mtp_accept_pct" in j)

        _json_req(f"{base}/profiles/chat/activate", token="admin", method="POST", body={})

        # --- /host -----------------------------------------------------------
        code, j = _json_req(f"{base}/host", token="admin")
        check("/host returns cpu/load/mem keys", code == 200
              and {"loadavg", "cpu_pct", "mem_used_gib", "mem_total_gib"} <= set(j))

        # --- /gpu ------------------------------------------------------------
        telemetry._cache.update(at=0.0, data=None)
        code, j = _json_req(f"{base}/gpu", token="admin")
        check("/gpu returns a cuda0 block from nvidia-smi", code == 200 and j["cuda0"]["util_pct"] == 42.0)
        check("/gpu vram parsed to GiB", 11.9 < j["cuda0"]["vram_used_gib"] < 12.1)
        # igpu0 is None where there's no AMD GPU / no /sys access, else a stats dict
        check("/gpu igpu0 is null or a well-formed block",
              j["igpu0"] is None or {"vram_used_gib", "util_pct", "temp_c"} <= set(j["igpu0"]))

        # --- /history ------------------------------------------------------
        code, j = _json_req(f"{base}/history?days=7", token="admin")
        check("/history day span", code == 200 and len(j["days"]) == 7)
        check("/history aligns every series to the day span",
              all(len(v) == 7 for v in j["energy"].values())
              and all(len(v) == 7 for v in j["savings"].values()))
        check("/history buckets requests under the serving profile",
              sum(j["requests"].get("chat", [])) == 1)
        check("/history picks up energy", sum(j["energy"]["gpu_kwh"]) > 0)
        code, j = _json_req(f"{base}/history?days=999", token="admin")
        check("/history clamps days", len(j["days"]) == 120)
        code, j = _json_req(f"{base}/history?from=2026-08-20&to=2026-08-24", token="admin")
        check("/history honours an explicit from/to range",
              code == 200 and j["days"] == ["2026-08-20", "2026-08-21", "2026-08-22",
                                            "2026-08-23", "2026-08-24"])

        # --- /savings/facts (cost explorer feed) ------------------------
        code, j = _json_req(f"{base}/savings/facts", token="admin")
        check("/savings/facts -> 200 with a facts[] + tiers[]", code == 200
              and isinstance(j.get("facts"), list) and isinstance(j.get("tiers"), list)
              and "energy_cost" in j)
        f0 = next((f for f in j["facts"] if f["reqs"]), None)
        # both dims come straight from the ledger now — no config-time remap
        check("/savings/facts carries served_stack + served_model + requested_model",
              f0 is not None and f0["served_stack"] == "chat"
              and f0["served_model"] == "Qwen3.8-27B-UD-Q4_K_XL"
              and f0["requested_model"] == "assistant")
        check("/savings/facts prices each fact per tier label",
              f0 is not None and set(f0["gross"]) == {t["label"] for t in j["tiers"]})
        check("/savings/facts fact gross sums to the tier total",
              all(abs(round(sum(f["gross"][t["label"]] for f in j["facts"]), 4)
                      - t["total_gross"]) < 1e-6 for t in j["tiers"]))

        # --- /profiles ---------------------------------------------------
        code, j = _json_req(f"{base}/profiles", token="admin")
        check("/profiles lists all configured profiles", code == 200
              and {p["name"] for p in j["profiles"]} == {"chat", "coding"})
        ev = next(p for p in j["profiles"] if p["name"] == "chat")
        cd = next(p for p in j["profiles"] if p["name"] == "coding")
        check("/profiles marks the active one", ev["active"] and not cd["active"])
        check("/profiles sorted by priority desc", j["profiles"][0]["priority"] >= j["profiles"][-1]["priority"])
        check("/profiles reports fit + placement", "fits" in ev and isinstance(ev["placement"], dict))
        check("/profiles previews would_evict for an inactive profile", isinstance(cd["would_evict"], list))

        # --- /status enrichment ---------------------------------------
        code, j = _json_req(f"{base}/status", token="admin")
        check("/status pools carry a breakdown", code == 200
              and any(isinstance(p.get("breakdown"), dict) and p["breakdown"] for p in j["pools"]))
        check("/status exposes devices[]", isinstance(j.get("devices"), list) and j["devices"])
        check("/status exposes footprint sources", isinstance(j.get("sources"), dict))
    finally:
        httpd.shutdown()

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
