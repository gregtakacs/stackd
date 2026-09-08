"""Web UI backend — the dashboard shell, static assets, and the JSON endpoints
it polls (/events, /gpu, /history, /profiles, /slots). Stdlib only."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402

from stackd import events, serve, telemetry  # noqa: E402
from stackd.manager import Manager  # noqa: E402
from stackd.runner import FakeRunner  # noqa: E402
from stackd.serve import make_server  # noqa: E402
from stackd.store import Store, _utc_day  # noqa: E402

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
CHECKS: list[tuple[str, bool]] = []


def check(name, cond, info=None):
    CHECKS.append((name + (f"   [{info}]" if info is not None and not cond else ""), bool(cond)))


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


def _raw_req(url, *, token=None, extra=None):
    """Like _req but returns the response HEADERS too — the caching checks need
    the ETag / cache-control a response carried, which _req throws away."""
    h = dict(extra or {})
    if token:
        h["authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


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
        # The placement map used to carry a 1500-word essay per lane that nobody
        # read. Lane notes are one glance now; anything that needs explaining lives
        # in a row's note cell or a tooltip. (Regex over the served shell, since the
        # prose is built in JS — this catches the source growing back, not the DOM.)
        notes = ["".join(re.findall(r'"([^"]*)"', m.group(1)))
                 for m in re.finditer(r"note: ((?:\"[^\"]*\"|\s*\+\s*|\n)+)", raw.decode())]
        check("lane notes stay short (<=260 chars each)", all(len(n) <= 260 for n in notes),
              [len(n) for n in notes])
        # A unit-less number on a bar reads as nothing: every number column states
        # its unit in the header, on the measured table and the budget-only one.
        check("every number column carries its unit in the header",
              b'"live GiB"' in raw and b'"plan GiB"' in raw and b'"budget GiB"' in raw)
        # A 2px accent ring inside a 9px swatch is mostly ring: it ate the tenant's
        # real colour, which is the only thing distinguishing klein from the chat
        # engine there. Grouping is carried by the divider + the "iGPU GTT" suffix.
        check("legend swatches wear no accent ring (the colour stays real)",
              b"i.gtt" not in raw)
        # The lane must fit one grid track: a lane spanning the whole row pushed the
        # map onto its own line on a desktop.
        check("no lane spans the whole grid row", b"grid-column:1 / -1" not in raw)
        # The live charts keep their own 1 s sample grid, decoupled from the poll.
        # Sampling once per poll meant any pause — refresh select focused, poll
        # error, `paused`, an engine that stopped reporting — left a hole in the
        # time axis, and uPlot bridged it with a curve: idle stretches read as a
        # slow smooth ramp instead of flat-or-missing.
        check("charts sample on their own 1s grid, not once per poll",
              b"const SAMPLE_MS = 1000" in raw and b"setInterval(pumpCharts, SAMPLE_MS)" in raw)
        check("1s is the default refresh rate",
              b'<option value="1000" selected>' in raw
              and b'<option value="3000" selected>' not in raw)
        check("no live line bridges a data gap", b"spanGaps: true" not in raw)
        check("focus pauses only the DOM repaints — the poll and 1s chart feed keep running",
              b"snapshot(gpu, host, tel)" in raw
              and b"if (uiLocked()) return;" in raw
              and b"if (uiLocked() || tick._busy)" not in raw)
        check("each poll endpoint degrades independently (a slow /v1/models can't drop /gpu)",
              b'api("/status").catch(() => null)' in raw
              and b'api("/v1/models").catch(() => null)' in raw
              and b'api("/gpu").catch(() => ({}))' in raw)
        # The daemon keeps its own 1 s sample ring buffer (/live?since=), so the
        # seconds the browser loses to background-tab throttling can be refilled
        # with real data instead of a held line or a hole. On return the grid
        # tail belongs to the backfill (pump parked) until it has caught up.
        check("the hidden-tab gap is backfilled from the daemon's 1s buffer",
              b"async function backfillLive" in raw
              and b'api("/live?since="' in raw
              and b"backfillLive().then(() => tick())" in raw
              and b"if (typeof backfilling !== \"undefined\" && backfilling) return;" in raw)
        # The 1s timer is only THROTTLED while hidden, not stopped — if the pump kept
        # running it would ratchet the grid tail to ~now on held/null points, so the
        # return-time backfill (which keys off that tail) sees a fake "caught up" and
        # /live?since=now yields nothing: blank until a hard refresh. Park the pump on
        # document.hidden so the tail stays at the last real sample and gets refilled.
        check("the 1s pump is parked while the tab is hidden (blank-until-refresh guard)",
              b"function pumpCharts() {" in raw
              and b"if (typeof document !== \"undefined\" && document.hidden) return;" in raw)
        # Parking the pump fixed the tail *ratchet*, but the gap still came back partly
        # unfilled: the append-backfill requested `/live?since=<browser-clock tail>` and
        # dropped every daemon sample at/below that tail, so a browser and a stackd host
        # that disagree by NTP lost the newest part of the window. The grid is now on the
        # DAEMON clock alone — `since` is anchored to browser-now (never TS[last]) and the
        # whole replay REBUILDS TS/SER, with live points shifted onto that timeline by
        # CLK_OFF. A refresh and a refocus must therefore show the identical window.
        check("backfill rebuilds the grid on the daemon clock (no browser-clock since)",
              b"since = Date.now() / 1000 - 630" in raw
              and b"let CLK_OFF = 0;" in raw
              and b"function nowT() { return Date.now() / 1000 + CLK_OFF; }" in raw
              and b"TS.length = 0; for (const k in SER) delete SER[k];" in raw
              and b"const t = now / 1000 + CLK_OFF;" in raw
              and b"TS[TS.length - 1] : Date.now()" not in raw)   # the old browser-clock since is gone
        # Engine captions (tokps/mtp/kv corners) follow the ACTIVE engine, else
        # the one with the most real data, and name themselves when >1 engine
        # shares the page. (engineNames[0] pinned captions to the alphabetical
        # first — a mostly-idle autocomplete — so they read frozen or blank
        # while the working engine's lines were live.) The "fullest" count is
        # windowed to the last ~30 samples so the session that just ended beats
        # one that ended 9 minutes ago.
        check("engine captions follow the active/fullest engine and name it",
              b"const isActive = n =>" in raw
              and b"cnt(b, tail) - cnt(a, tail)" in raw and b"cnt(b, 0) - cnt(a, 0)" in raw
              and b"e0pre" in raw and b'e0.toUpperCase() + " "' in raw)
        # ...and a caption fix is worthless if it never reaches the browser.
        # _web_file used to be functools.lru_cached: with the source tree
        # bind-mounted, the daemon kept serving the dashboard it booted with
        # while the fixed file sat on disk (the "still nothing in the corners"
        # report). Now the shell carries a build id, /health reports the live
        # one, and the page says so when they differ.
        check("shell is stamped with a build id (token replaced)",
              re.search(rb'const WEB_REV = "[0-9a-f]{16}"', raw) and b"__WEB_REV__" not in raw)
        code, hd, _body = _raw_req(f"{base}/")
        code2, _hd2, body2 = _raw_req(f"{base}/", extra={"if-none-match": hd.get("etag", "")})
        check("shell revalidates: no-cache + ETag, and a matching If-None-Match is a 304",
              code == 200 and bool(hd.get("etag")) and "no-cache" in (hd.get("cache-control") or "")
              and code2 == 304 and body2 == b"")
        hcode, hj = _json_req(f"{base}/health")
        check("/health publishes the same build id the shell was stamped with",
              hcode == 200 and hj.get("web_rev")
              and f'const WEB_REV = "{hj["web_rev"]}"'.encode() in raw)
        check("a tab that predates the deploy notices and reloads when idle",
              b"async function checkRev" in raw and b'setInterval(checkRev, 20000)' in raw
              and b'idle && sessionStorage.getItem("stackd_reloaded") !== live' in raw
              and b'id="webStale"' in raw and b'!uiLocked()' in raw)
        # The auto-reload itself cannot be driven headless (no Chrome-driving
        # library here, and --dump-dom can't see sessionStorage), so the four
        # idle guards are asserted as source: each one exists to stop a reload
        # landing on top of something the user was doing, and any of them could
        # go missing one careless line-edit at a time.
        check("the idle reload refuses to fire over a hidden tab, a locked UI, "
              "a running tick, or fresh input — once per build",
              b'document.visibilityState === "visible" && !uiLocked()' in raw
              and b"!tick._busy && Date.now() - lastInputAt > 15000" in raw
              and b'sessionStorage.setItem("stackd_reloaded", live)' in raw
              and b'for (const ev of ["pointerdown", "keydown", "wheel"])' in raw
              and b"lastInputAt = Date.now()" in raw)
        web_bak = serve._WEB_DIR
        (tmp / "web").mkdir()
        serve._WEB_DIR = tmp / "web"
        (serve._WEB_DIR / "dashboard.html").write_bytes(b"<title>stackd asset v1</title>")
        _c1, _h1, b1 = _raw_req(f"{base}/")
        time.sleep(0.02)
        (serve._WEB_DIR / "dashboard.html").write_bytes(b"<title>stackd asset version two</title>")
        _c2, _h2, b2 = _raw_req(f"{base}/")
        serve._WEB_DIR = web_bak
        check("web assets are re-read when the file changes (bind-mounted edit, no restart)",
              b"v1" in b1 and b"version two" in b2)
        # The engine card must not reflow when a generation finishes: "(live)" ->
        # "(last session)" and "context" -> "peak context · 130.4k / 131k" wrapped
        # the stat row and visibly grew the card on every session end. Equal-length
        # suffixes; the long peak string lives in the tooltip.
        check("engine card labels keep their width when a generation finishes",
              b'"tok/s gen (last)"' in raw and b'"context (last)"' in raw
              and b'"peak context"' not in raw and b"gen (last session)" not in raw)
        # The image ladder is a grid of name/verdict/footprint/action. Reasons are
        # sentences ("host RAM 122 > 64.9 free (vulkan) (unreachable: ...)"); inline,
        # they wrapped each row into a paragraph and knocked the numbers out of
        # column, so they belong in the tooltip.
        check("image ladder is a grid with the reasons in tooltips",
              b"grid-template-columns:minmax(0,1fr) 96px 78px 68px" in raw
              and b'el("span", "st"' in raw
              and b" vram/" not in raw and b"refused before start" not in raw)
        check("image ladder rows show the model capabilities on the line",
              b'el("span", "caps"' in raw and b"CAP_ABBR" in raw)
        # one slow /engine (long prefill) must not stall the tick: the engine
        # reads sit on their own short deadline with a brief stale hold, and the
        # overall GET deadline keeps the worst-case tick under the sample grid's
        # 20s hold window — a wedged engine degrades to a held line, not a hole
        check("engine reads are deadline-bounded so a busy engine can't gap the charts",
              b"AbortSignal.timeout(1200)" in raw and b"LAST_TEL" in raw
              and b"AbortSignal.timeout(8000)" in raw)
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

        # --- /image: the ladder must ship a verdict, not raw numbers for the
        # client to guess with. It used to publish only footprint_gib, so the
        # dashboard compared that to device headroom and offered
        # flux2-dev-turbo on the iGPU — a model whose host_ram_gib (122) alone
        # exceeds this box's MemTotal, i.e. a load the scheduler refuses.
        code, j = _json_req(f"{base}/image", token="admin")
        rows = j.get("prefer", []) if code == 200 else []
        check("/image verdicts every backend of every candidate",
              code == 200 and rows and all(
                  set(r["backends"]) == set(r["fit"]) and all(
                      isinstance(f["fits"], bool) and isinstance(f["reason"], str)
                      for f in r["fit"].values()) for r in rows))
        check("/image publishes both axes it judged against",
              all("footprint_gib" in r and "host_ram_gib" in r for r in rows)
              and isinstance(j.get("host_ram_avail_gib"), (int, float))
              and j["mem_total_gib"] > 0)
        _bad = [r for r in rows if not r["fits"]]
        _good = [r for r in rows if r["fits"]]
        check("refused rows name no device and spell out every reason",
              all(r["would_load_on"] is None and all(f["reason"] for f in r["fit"].values())
                  for r in _bad))
        check("rows that do fit name the device + backend they would land on",
              all(r["would_load_on"] is not None and r["would_load_on"][0] in j["headroom_gib"]
                  for r in _good))
        check("a verdict of `fits` never comes with a reason",
              all(not f["reason"] for r in _good for f in r["fit"].values() if f["fits"]))

        _json_req(f"{base}/profiles/chat/activate", token="admin", method="POST", body={})

        # --- /host -----------------------------------------------------------
        code, j = _json_req(f"{base}/host", token="admin")
        check("/host returns cpu/load/mem keys", code == 200
              and {"loadavg", "cpu_pct", "mem_used_gib", "mem_total_gib"} <= set(j))
        check("/host carries the physical-RAM decomposition (dashboard host-RAM lane)",
              {"mem_phys_used_gib", "mem_cache_reclaimable_gib", "mem_anon_gib",
               "mem_shmem_gib", "mem_slab_unreclaim_gib", "mem_free_gib",
               "mem_anonpages_gib"} <= set(j))
        # The dashboard needs the *real* AnonPages, not the derived one: `anon` is
        # computed as phys - cache - shmem - slab, so anything the kernel charges
        # outside those pools lands in it — on an AMD box that is the iGPU's GTT
        # window (~20 GiB with an image model resident), which the lane must carve
        # separately or "host + other processes" reports 7x its real size.
        check("/host real AnonPages <= the derived anon (the gap is driver/GTT pages)",
              j["mem_anonpages_gib"] >= 0 and j["mem_anonpages_gib"] <= j["mem_anon_gib"] + 0.6)
        check("/host decomposition sums to physical use",
              abs((j["mem_anon_gib"] + j["mem_cache_reclaimable_gib"] + j["mem_shmem_gib"]
                   + j["mem_slab_unreclaim_gib"]) - j["mem_phys_used_gib"]) <= 0.6)
        check("/host container_mem is a no-op under FakeRunner (no .base), not an error",
              "containers" not in j and "containers_error" not in j)

        # --- /gpu ------------------------------------------------------------
        telemetry._cache.update(at=0.0, data=None)
        code, j = _json_req(f"{base}/gpu", token="admin")
        check("/gpu returns a cuda0 block from nvidia-smi", code == 200 and j["cuda0"]["util_pct"] == 42.0)
        check("/gpu vram parsed to GiB", 11.9 < j["cuda0"]["vram_used_gib"] < 12.1)
        _ig = j.get("igpu0")
        if _ig:
            check("/gpu igpu0 breaks GTT out from the stolen window (pool charge is GTT only)",
                  {"gtt_used_gib", "gtt_total_gib", "stolen_used_gib"} <= set(_ig)
                  and _ig["vram_used_gib"] >= _ig["gtt_used_gib"]
                  and _ig["gtt_total_gib"] > 0)
        else:
            check("/gpu igpu0 absent with no amdgpu — the lane must skip ceiling/bracket/readout", True)
        # igpu0 is None where there's no AMD GPU / no /sys access, else a stats dict
        check("/gpu igpu0 is null or a well-formed block",
              j["igpu0"] is None or {"vram_used_gib", "util_pct", "temp_c"} <= set(j["igpu0"]))

        # --- /history ------------------------------------------------------
        code, j = _json_req(f"{base}/history?days=7", token="admin")
        check("/history day span", code == 200 and len(j["days"]) == 7)
        check("/history aligns every series to the day span",
              all(len(v) == 7 for v in j["energy"].values())
              and all(len(v) == 7 for v in j["savings"].values())
              and all(len(v) == 7 for v in j["throughput"].values()))
        check("/history throughput has the four token-weighted series",
              set(j["throughput"]) == {"decode_tps", "prefill_tps", "ctx_avg", "ctx_max"})
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
        # A subset check, not an equality: the box's config.local overlay adds its
        # own profiles (coding-long, code-autocomplete…) on top of the shipped
        # config/, and this suite reads whichever config is actually loaded. An
        # exact set made the suite red inside the container for the wrong reason
        # and cast doubt on the other 67 checks.
        code, j = _json_req(f"{base}/profiles", token="admin")
        _names = {p["name"] for p in j["profiles"]}
        _want = {pr.profile for pr in mgr.cfg.profiles.values()}
        check("/profiles lists exactly the configured profiles", code == 200
              and _names == _want, (_names, _want))
        check("/profiles always carries the two reference profiles it is tested against",
              {"chat", "coding"} <= _names, sorted(_names))
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

        # --- the GTT clamp: ONE source of truth for the iGPU ceiling -------------
        # IGPU_VRAM_BUDGET_GIB is a config *wish*; the GTT window the driver
        # exposes is the hardware. fit.budget_facts takes the min, and /status,
        # the breakdown's ≤ label and validate's flag all follow that one number.
        # The dashboard used to be the only place that clamped, so `stackctl
        # status` printed ≤90 over a lane that said 62.2 on this box — and a
        # mistuned budget passed validation and then OOMed the host. These are the
        # checks that keep the two halves from drifting apart again.
        _c, j = _json_req(f"{base}/status", token="admin")
        _ig = next((d for d in (j.get("devices") or []) if d.get("device") == "igpu0"), {})
        _cfg_budget = _ig.get("budget")
        check("with no window measurable, nothing is clamped and the config budget stands",
              _cfg_budget and _ig.get("gtt_window") is None
              and _ig.get("budget_effective") == _cfg_budget)
        os.environ["STACKD_GTT_WINDOW_GIB"] = "62.2"
        try:
            _c, j = _json_req(f"{base}/status", token="admin")
            _ig = next((d for d in (j.get("devices") or []) if d.get("device") == "igpu0"), {})
            check("/status budget_effective == min(configured budget, measured GTT window)",
                  _ig.get("gtt_window") == 62.2 and _cfg_budget > 62.2
                  and _ig.get("budget_effective") == min(_cfg_budget, 62.2),
                  _ig)
            _hu = next((p for p in j["pools"] if p["pool"] == "host_unified"), {})
            _lbl = [k for k in (_hu.get("breakdown") or {}) if k.startswith("vram:igpu0")]
            check("the breakdown's ≤ label follows the effective cap, not the env var",
                  len(_lbl) == 1 and "(≤62.2)" in _lbl[0], _lbl)
            check("validate names the clamp rather than silently shrinking the cap",
                  any("clamped" in f and "62.2" in f and "90" in f for f in j.get("flags") or []),
                  j.get("flags"))
            # The whole point: the shell must read the server's number, not
            # re-derive its own min() and drift from it again.
            _sc, _sct, shell = _req(f"{base}/")
            check("the dashboard's gttCap consumes the server's budget_effective",
                  _sc == 200 and b"budget_effective" in shell
                  and b"const gttSrvCap" in shell
                  and b"const gttCaps = [gttCeiling, gttWindow, gttSrvCap]" in shell)
            check("the printed cap is the same min() the fit math used",
                  b"gttSrvCap || gttCeiling" in shell)
        finally:
            os.environ.pop("STACKD_GTT_WINDOW_GIB", None)
        # --- GENHIST backfill: corner averages that outlive the tab --------------
        # The 10-min average on the request corners was a purely client-side ring:
        # reload the page, or bounce the daemon, and it read `avg –` until the next
        # generation, however warm the box was — while the sticky "last" number
        # beside it had been persisted months ago. serve.py now keeps its own ring
        # in meta and replays it on /live.
        _lk = threading.Lock()
        _gb = (b'data: {"id":2,"timings":{"predicted_per_second":120.0,'
               b'"prompt_per_second":4000.0,"prompt_n":80,"predicted_n":40}}\n\n')
        serve._GENHIST.clear()
        serve._GENHIST_SEEDED.clear()
        check("a daemon with nothing recorded replays an empty history",
              serve._genhist_seed(store, ["chat"], _lk) == {})
        _now = time.time()
        serve._meta_set(store, "genhist/chat",
                        [{"t": _now - 7200, "gen": 1.0, "prompt": 1.0, "mtp": 1.0}]
                        + [{"t": _now - 300 + i, "gen": 50.0 + i, "prompt": 900.0, "mtp": 70.0}
                           for i in range(4)], _lk)
        serve._GENHIST_SEEDED.clear()
        _h = serve._genhist_seed(store, ["chat"], _lk)
        check("the replay window is the window the corner averages, oldest points dropped",
              len(_h.get("chat") or []) == 4 and [p["gen"] for p in _h["chat"]] == [50.0, 51.0, 52.0, 53.0],
              _h)
        serve._capture_gen("chat", _gb, store, _lk)
        check("a completed request lands in the replay ring",
              len(serve._GENHIST["chat"]) == 5, serve._GENHIST["chat"])
        check("the ring stays in timestamp order as requests arrive out of a restart",
              [p["t"] for p in serve._GENHIST["chat"]]
              == sorted(p["t"] for p in serve._GENHIST["chat"]))
        serve._GENHIST.clear()
        serve._GENHIST_SEEDED.clear()
        serve._LAST_GEN.clear()          # a replay must not fake a sticky "last"
        _c, jl = _json_req(f"{base}/live?since=0", token="admin")
        _gh = (jl.get("gen_history") or {}).get("chat") or []
        check("GET /live replays the ring to a cold dashboard",
              _c == 200 and len(_gh) == 5 and _gh[-1]["gen"] == 120.0, _gh)
        check("GET /live keeps its old shape for an older dashboard",
              "samples" in jl and "next" in jl)
        check("the replay is read-only — it must not fake a sticky 'last' number",
              "chat" not in serve._LAST_GEN, serve._LAST_GEN)
        check("the ring is capped so meta cannot grow without bound",
              len(serve._genhist_prune([{"t": _now - i * 0.1, "gen": float(i)} for i in range(400)]))
              == serve._GENHIST_MAX_PTS)
        check("pruning skips malformed points instead of raising",
              len(serve._genhist_prune([None, {"gen": 5}, {"t": "x"}, {"t": _now}])) == 1)
        # vLLM/SGLang have no per-request `timings` at all, so nothing reaches the
        # append above — and this box serves its main profile on vLLM. Those
        # corners were always filled by the dashboard's active-poll fallback, so
        # the server has to derive the same points from its own 1 s buffer or the
        # reload blanks exactly the engines that are actually running.
        _bk = list(serve._LIVE_BUF)
        try:
            serve._LIVE_BUF.clear()
            for k in range(120):                       # oldest first, 1 s apart, like the sampler
                serve._LIVE_BUF.append({"ts": _now - (119 - k),
                                        "eng": {"vllmish": {"active": True, "gen_tok_s": 40.0 + k,
                                                            "prompt_tok_s": None,
                                                            "mtp_accept_pct": None}}})
            _lv = serve._genhist_from_live(["vllmish"], {"vllmish": "vllm-cuda"}, {})
            _pts = [p["t"] for p in _lv.get("vllmish") or []]
            check("a no-timings engine gets its corner back from the live buffer",
                  len(_pts) == 24 and _pts == sorted(_pts), len(_pts))
            check("the derived series is stepped, not one point per sample",
                  len(_pts) * 5 > 115 and all(
                      b - a >= serve._GENHIST_LIVE_STEP_S for a, b in zip(_pts, _pts[1:])))
            check("a stack with a real per-request ring is not overwritten by samples",
                  serve._genhist_from_live(["vllmish"], {"vllmish": "vllm-cuda"},
                                           {"vllmish": [{"t": _now, "gen": 1.0}]}) == {})
            check("a timings-capable engine keeps its own source, not samples",
                  serve._genhist_from_live(["llamish"], {"llamish": "llamacpp-cuda"}, {}) == {})
            serve._LIVE_BUF.clear()
            serve._LIVE_BUF.extend({"ts": _now - k, "eng": {"vllmish": {"active": False,
                                                                       "gen_tok_s": 163.0,
                                                                       "prompt_tok_s": 30209.7,
                                                                       "mtp_accept_pct": 35.0}}}
                                   for k in range(30))
            check("idle session gauges are not re-timestamped as fresh points "
                  "(they would freeze the corner warm forever)",
                  serve._genhist_from_live(["vllmish"], {"vllmish": "vllm-cuda"}, {}) == {})
        finally:
            serve._LIVE_BUF.clear()
            serve._LIVE_BUF.extend(_bk)
        _shc, _sht, _shell = _req(f"{base}/")
        _sh = _shell.decode()
        check("the dashboard seeds GENHIST from the replay instead of thin air",
              _shc == 200 and "seedGenHist(pay.gen_history)" in _sh
              and "let genHistSeeded = false" in _sh)
        # the trap: tick() pushes a point the first time it sees a last_request.at
        # transition, so seeding must consume the newest one or the first poll
        # counts that request twice and skews every average on screen.
        check("seeding claims lastReqAt so the newest request is not double-counted",
              "lastReqAt[n] = newest" in _sh and "have.has(p.t)" in _sh)
        check("the (n pts) figure is what the mean consumed, not the whole ring",
              "const n = w.n || 0;" in _sh and "600, 120, w)" in _sh)

    finally:
        httpd.shutdown()

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
