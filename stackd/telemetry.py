"""Best-effort live GPU stats for the web UI (`GET /gpu`).

`cuda0` comes from `nvidia-smi` (wrapped so a wedged driver can't stall us — see
`runner._nvidia_smi`). `igpu0` comes from the amdgpu `sysfs` node first (needs
only `/dev/dri` access + the always-mounted `/sys`), then `rocm-smi --json` as a
fallback. If neither is reachable the field is just `null` and the UI falls back
to the solver's pool math. Cached for a couple of seconds so a fast dashboard
poll doesn't re-read sysfs / fork smi on every request.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_TTL_S = 2.0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def _f(x) -> float | None:
    try:
        return round(float(str(x).strip().rstrip("%")), 1)
    except (TypeError, ValueError):
        return None


def _cuda0() -> dict | None:
    from stackd.runner import _nvidia_smi
    out = _nvidia_smi([
        "--query-gpu=memory.used,memory.total,utilization.gpu,"
        "power.draw,power.limit,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return None
    row = out.strip().splitlines()[0].split(",")
    if len(row) < 6:
        return None
    used, total, util, pw, pw_max, temp = (_f(v) for v in row[:6])
    return {
        "vram_used_gib": round(used / 1024, 2) if used is not None else None,
        "vram_total_gib": round(total / 1024, 2) if total is not None else None,
        "util_pct": util, "power_w": pw, "power_limit_w": pw_max, "temp_c": temp,
    }


def _read(path: str):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _igpu0_sysfs() -> dict | None:
    """Read the integrated AMD GPU straight from `/sys/class/drm/cardN/device`.
    Picks the card whose `driver` symlink is `amdgpu` (the dGPU is `nvidia`).

    On an APU the dedicated `vram` window is tiny (~1 GiB stolen); the iGPU really
    allocates from `gtt` (a GART view of system RAM). So `used` = vram + gtt used
    and `total` = gtt total — that's what lines up with the igpu0 lane's budget.

    `gtt_used_gib` / `gtt_total_gib` / `stolen_used_gib` are broken out separately
    because only GTT is a claim on host RAM: the stolen window is carved out at
    boot and never appears in MemTotal, so charging it to the host_unified pool
    (as `vram_used_gib` does, kept for compatibility) slightly overstates it. The
    dashboard's pool decomposition uses the GTT figure. Fixed 2026-09-07: with a
    42 GiB image model resident, cgroup anon + AnonPages + Shmem + SUnreclaim
    accounted for only 30.9 of a 54.4 GiB "OS used" — the missing ~20 GiB was
    exactly `gtt_used`, driver pages that belong to no process's RSS.
    """
    import re
    for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        if not re.fullmatch(r"card[0-9]+", os.path.basename(os.path.dirname(dev))):
            continue  # skip the cardN-DP-* connector dirs
        try:
            drv = os.path.basename(os.path.realpath(os.path.join(dev, "driver")))
        except OSError:
            continue
        if drv != "amdgpu":
            continue
        vram_u = _b_to_gib(_read(os.path.join(dev, "mem_info_vram_used"))) or 0
        gtt_u = _b_to_gib(_read(os.path.join(dev, "mem_info_gtt_used"))) or 0
        gtt_t = _b_to_gib(_read(os.path.join(dev, "mem_info_gtt_total")))
        used = _f(vram_u + gtt_u) if (vram_u or gtt_u) else None
        total = _f(gtt_t) if gtt_t else _f(_b_to_gib(_read(os.path.join(dev, "mem_info_vram_total"))))
        util = _f(_read(os.path.join(dev, "gpu_busy_percent")))
        pw = pw_cap = temp = None
        for hw in glob.glob(os.path.join(dev, "hwmon", "hwmon*")):
            pw = pw or _uw_to_w(_read(os.path.join(hw, "power1_average")) or _read(os.path.join(hw, "power1_input")))
            pw_cap = pw_cap or _uw_to_w(_read(os.path.join(hw, "power1_cap")))
            temp = temp or _mc_to_c(_read(os.path.join(hw, "temp1_input")))
        if used is None and util is None:
            return None
        return {"vram_used_gib": used, "vram_total_gib": total, "util_pct": util,
                "power_w": _f(pw), "power_limit_w": _f(pw_cap), "temp_c": _f(temp),
                "gtt_used_gib": _f(gtt_u) if gtt_u else 0.0,
                "gtt_total_gib": _f(gtt_t), "stolen_used_gib": _f(vram_u) if vram_u else 0.0}
    return None


def _b_to_gib(s):
    try:
        return int(s) / (1024 ** 3)
    except (TypeError, ValueError):
        return None


def _uw_to_w(s):
    try:
        return int(s) / 1_000_000
    except (TypeError, ValueError):
        return None


def _mc_to_c(s):
    try:
        return int(s) / 1000
    except (TypeError, ValueError):
        return None


def _rocm_pick(card: dict, *needles: str):
    for k, v in card.items():
        kl = k.lower()
        if all(n in kl for n in needles):
            return v
    return None


def _igpu0_rocm() -> dict | None:
    try:
        r = subprocess.run(
            ["timeout", "-k", "2", "8", "rocm-smi", "--showuse",
             "--showmeminfo", "vram", "--showpower", "--json"],
            capture_output=True, text=True, timeout=13,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        doc = json.loads(r.stdout)
    except Exception:
        return None
    card = next((v for k, v in doc.items() if k.lower().startswith("card") and isinstance(v, dict)), None)
    if not card:
        return None
    used = _rocm_pick(card, "vram", "used")
    total = _rocm_pick(card, "vram", "total")

    def to_gib(b):  # rocm-smi reports VRAM in bytes
        try:
            b = float(b)
        except (TypeError, ValueError):
            return None
        return round(b / (1024 ** 3), 2) if b > (1 << 20) else round(b, 2)

    return {
        "vram_used_gib": to_gib(used),
        "vram_total_gib": to_gib(total),
        "util_pct": _f(_rocm_pick(card, "gpu", "use") or _rocm_pick(card, "gfx", "activity")),
        "power_w": _f(_rocm_pick(card, "average", "power") or _rocm_pick(card, "socket", "power")),
        "power_limit_w": _f(_rocm_pick(card, "power", "cap")),
        "temp_c": _f(_rocm_pick(card, "temperature", "edge") or _rocm_pick(card, "temperature", "sensor")),
    }


def _igpu0() -> dict | None:
    return _igpu0_sysfs() or _igpu0_rocm()


def gpu_stats() -> dict:
    now = time.time()
    with _lock:
        if _cache["data"] is not None and now - _cache["at"] < _TTL_S:
            return _cache["data"]
    data = {"cuda0": _cuda0(), "igpu0": _igpu0(), "sampled_at": now}
    with _lock:
        _cache["at"], _cache["data"] = now, data
    return data


# --- host CPU / load / RAM (for the dashboard's 10-min live strip) -----------
_host_prev: dict = {"total": 0.0, "idle": 0.0}


def host_stats() -> dict:
    out: dict = {"loadavg": None, "cpu_pct": None, "mem_used_gib": None,
                 "mem_total_gib": None, "mem_available_gib": None,
                 "ncpu": os.cpu_count(), "sampled_at": time.time()}
    try:
        with open("/proc/loadavg") as fh:
            out["loadavg"] = [float(x) for x in fh.read().split()[:3]]
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/stat") as fh:
            parts = fh.readline().split()[1:]
        vals = [float(x) for x in parts]
        total, idle = sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0.0)
        dt, di = total - _host_prev["total"], idle - _host_prev["idle"]
        _host_prev.update(total=total, idle=idle)
        if dt > 0:
            out["cpu_pct"] = round(100.0 * (1.0 - di / dt), 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        mi = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                mi[k] = float(v.strip().split()[0]) / (1024 * 1024)  # kB -> GiB
        if "MemTotal" in mi:
            mt = mi["MemTotal"]
            mf = mi.get("MemFree", 0.0)
            avail = mi.get("MemAvailable", mf)
            shmem = mi.get("Shmem", 0.0)
            sunreclaim = mi.get("SUnreclaim", 0.0)
            # file-backed page cache that drops without swap under pressure. Cached
            # already folds in Shmem/tmpfs (which needs swap, not reclaim) so back
            # it out. This is the "would just get evicted" pool.
            cache_reclaimable = max(
                0.0, mi.get("Buffers", 0.0) + mi.get("Cached", 0.0)
                + mi.get("SReclaimable", 0.0) - shmem)
            phys_used = mt - mf                       # everything not on the freelist
            # what's left once the reclaimable pools are removed: process anon +
            # mlocked + page tables + kernel stacks etc. — the genuinely committed
            # slice that a new allocation can't get back cheaply. NOTE: this is a
            # *derived* figure, so anything the kernel charges outside
            # Cached/Shmem/SUnreclaim lands in it — on an AMD box that includes the
            # iGPU's GTT window (~20 GiB with an image model resident), which is why
            # it reads far above AnonPages. The real per-process number is
            # `mem_anonpages_gib` below; the host-RAM lane needs both to keep
            # driver-pinned GPU pages out of "host + other processes".
            anon = max(0.0, phys_used - cache_reclaimable - shmem - sunreclaim)
            out["mem_total_gib"] = round(mt, 2)
            out["mem_free_gib"] = round(mf, 2)
            out["mem_available_gib"] = round(avail, 2)
            out["mem_used_gib"] = round(mt - avail, 2)          # unchanged: the marker
            out["mem_phys_used_gib"] = round(phys_used, 2)      # bar total (MemTotal-MemFree)
            out["mem_cache_reclaimable_gib"] = round(cache_reclaimable, 2)
            out["mem_shmem_gib"] = round(shmem, 2)
            out["mem_slab_unreclaim_gib"] = round(sunreclaim, 2)
            out["mem_anonpages_gib"] = round(mi.get("AnonPages", 0.0), 2)
            out["mem_anon_gib"] = round(anon, 2)
    except (OSError, ValueError):
        pass
    return out


# --- per-container memory (dashboard host-RAM lane: name the anon tenants) ----
_cmem_lock = threading.Lock()
_cmem_cache: dict = {"at": 0.0, "key": None, "data": {}, "refreshing": False}
_CMEM_TTL_S = 8.0


def _one_container_mem(base: str, name: str, timeout: float) -> tuple[str, dict] | None:
    """cgroup memory for one container via the Docker Engine API. `one-shot=true`
    skips the 1s CPU sample `stream=false` otherwise takes — we only want memory."""
    url = f"{base}/containers/{name}/stats?stream=false&one-shot=true"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            m = (json.loads(r.read()) or {}).get("memory_stats") or {}
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    st = m.get("stats") or {}
    g = 1024 ** 3
    # cgroup v2: `anon` is the non-reclaimable working set; `file` is this
    # container's slice of the global page cache (already in meminfo — don't
    # re-add it to the lane). `shmem` is tmpfs / SysV / POSIX shared memory
    # charged to this cgroup — for an engine that offloads a big structure to
    # /dev/shm (SGLang's FP8 n-gram PLE table, HiCache host pool) it dwarfs
    # anon and is just as un-reclaimable, so attribute it to the engine too.
    # cgroup v1 fallback: `rss` / `cache`.
    anon = st.get("anon", st.get("rss", 0.0)) or 0.0
    fileb = st.get("file", st.get("cache", 0.0)) or 0.0
    shmem = st.get("shmem", 0.0) or 0.0
    usage = m.get("usage", 0.0) or 0.0
    return name, {"anon_gib": round(anon / g, 2),
                  "shmem_gib": round(shmem / g, 2),
                  "file_gib": round(fileb / g, 2),
                  "usage_gib": round(usage / g, 2)}


def _cmem_refresh(base: str, names: list[str], key, timeout: float) -> None:
    out: dict = {}
    try:
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
            for res in ex.map(lambda n: _one_container_mem(base, n, timeout), names):
                if res:
                    out[res[0]] = res[1]
    finally:
        with _cmem_lock:
            _cmem_cache.update(at=time.time(), key=key, data=out, refreshing=False)


def container_mem(base_url: str | None, names, *, timeout: float = 3.5) -> dict:
    """{container_name: {anon_gib, file_gib, usage_gib}} for the given containers,
    from cgroup memory.stat via the Docker Engine API. NON-BLOCKING: returns the
    last cached sample immediately (an empty dict on the very first call) and
    refreshes in the background when the cache is stale, so a hung Docker API
    can't stall the `/host` poll. Best-effort — a gone/unreachable container just
    drops out of the map."""
    names = sorted({n for n in (names or []) if n})
    if not base_url or not names:
        return {}
    key = (base_url, tuple(names))
    now = time.time()
    with _cmem_lock:
        fresh = _cmem_cache["key"] == key and now - _cmem_cache["at"] < _CMEM_TTL_S
        data = dict(_cmem_cache["data"]) if _cmem_cache["key"] == key else {}
        if not fresh and not _cmem_cache["refreshing"]:
            _cmem_cache["refreshing"] = True
            threading.Thread(target=_cmem_refresh, daemon=True,
                             args=(base_url.rstrip("/"), names, key, timeout)).start()
    return data


# --- normalised per-engine telemetry (llama.cpp /slots+/metrics, vLLM + SGLang /metrics) ---
def _prom(text: str) -> dict:
    """Prometheus exposition -> {metric_name: last_value}. Labels ignored."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            name, val = line.rsplit(" ", 1)
            name = name.split("{", 1)[0].strip()
            out[name] = float(val)
        except ValueError:
            continue
    return out


def _prom_label(text: str, metric: str, label: str):
    """Pull one label value out of a labelled Prometheus line (e.g. the token
    capacity buried in `vllm:cache_config_info{...,kv_cache_size_tokens="310321"}`)."""
    for line in text.splitlines():
        if line.startswith(metric + "{"):
            import re
            mm = re.search(rf'{re.escape(label)}="([^"]*)"', line)
            if mm:
                return mm.group(1)
    return None


def _prom_sum(text: str, metric: str, **match) -> float | None:
    """Sum every series of `metric` whose label set contains all of `match`
    (label=value). No `match` -> sum across every series of that name. `_prom`
    keys by bare name so it silently keeps only the last of a multi-series
    counter (e.g. SGLang's `generation_tokens_total{is_streaming=...}`); this
    adds them up instead. None if no series matched."""
    total, seen = 0.0, False
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == "#" or not line.startswith(metric):
            continue
        head, _, val = line.rpartition(" ")
        if head.split("{", 1)[0].strip() != metric:
            continue
        if match:
            labels = head[len(metric):]
            if not all(f'{k}="{v}"' in labels for k, v in match.items()):
                continue
        try:
            total += float(val); seen = True
        except ValueError:
            pass
    return total if seen else None


def _http_json(url: str, timeout: float = 4.0):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _http_text(url: str, timeout: float = 4.0) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


_eng_state: dict = {}   # endpoint -> {prev, sess_start, last_sess}


def _win_rate(a: dict | None, b: dict, tk: str, sc: str):
    """tokens/s over the window a->b, from the monotonic count/time counters."""
    if a and b and a.get(tk) is not None and b.get(tk) is not None:
        dtk, dsc = b[tk] - a[tk], (b.get(sc) or 0) - (a.get(sc) or 0)
        if dsc > 0.02 and dtk > 0:
            return round(dtk / dsc, 1)
    return None


def _win_mtp(a: dict | None, b: dict):
    if a and b and a.get("draft") is not None and b.get("draft") is not None:
        dd, da = b["draft"] - a["draft"], b["acc"] - (a.get("acc") or 0)
        if dd > 0:
            return round(100.0 * da / dd, 1)
    return None


def _wall_rate(a: dict | None, b: dict, key: str):
    """value/s over real wall-clock time — for the one signal (slot context
    position) that actually moves mid-request; the /metrics counters and the
    throughput gauge only update when a request finishes."""
    if a and b and a.get(key) is not None and b.get(key) is not None:
        dv, dt = b[key] - a[key], b["ts"] - a["ts"]
        if dt > 0.5 and 0 < dv < 1e6:
            return round(dv / dt, 1)
    return None


def _slot_decoded(s: dict):
    """tokens DECODED by one slot: the counter that moves only during
    generation. Newer llama.cpp builds moved it into the slot's
    ``next_token`` list; older ones expose it at the top level."""
    v = s.get("n_decoded")
    if v is not None:
        return v
    nt = s.get("next_token")
    if isinstance(nt, list) and nt and isinstance(nt[0], dict):
        return nt[0].get("n_decoded", 0) or 0
    return 0


def engine_telemetry(endpoint: str, template: str) -> dict:
    """Normalised live stats for one engine. All keys may be None.

    llama.cpp's `*_seconds` gauges read 0 when idle, so rates are derived from
    the monotonic counters. `active` says whether a request is generating right
    now; while active the rates are the live window rate, and once a request
    finishes they stick at *that session's average* until the next one (so the
    charts and the engine card hold a real number instead of dropping to 0)."""
    d = {"template": template, "active": False,
         "gen_tok_s": None, "prompt_tok_s": None, "kv_pct": None,
         "ctx_tokens": None, "ctx_max": None, "kv_pool_tokens": None,
         "slots": None, "running": None, "waiting": None,
         "mtp_accept_pct": None, "mtp_accepted_total": None, "mtp_drafted_total": None,
         "gen_tok_s_session": None, "prompt_tok_s_session": None, "mtp_accept_pct_session": None}
    ep = endpoint.rstrip("/")
    st = _eng_state.setdefault(ep, {"prev": None, "sess_start": None, "last_sess": {}})
    now = time.time()
    try:
        if template.startswith("llamacpp"):
            slots = []
            try:
                slots = _http_json(ep + "/slots")
                slots = slots if isinstance(slots, list) else slots.get("slots", [])
            except Exception:  # noqa: BLE001 — /slots may be disabled
                pass
            per_ctx = max((s.get("n_ctx", 0) or 0 for s in slots), default=0)
            fills = []
            for s in slots:
                u = s.get("n_past")
                if u is None:
                    u = max(s.get("n_prompt_tokens", 0), s.get("n_prompt_tokens_processed", 0)) + s.get("n_decoded", 0)
                    if not s.get("is_processing"):
                        u = max(u, s.get("n_prompt_tokens_cache", 0))
                fills.append(u)
            busiest = max(fills, default=0)
            d["ctx_tokens"] = busiest or None
            d["ctx_max"] = d["kv_pool_tokens"] = per_ctx or None
            if per_ctx:
                d["kv_pct"] = round(100.0 * busiest / per_ctx, 1)
            running_slots = sum(1 for s in slots if s.get("is_processing"))
            if slots:
                d["slots"] = len(slots)

            m = _prom(_http_text(ep + "/metrics"))
            running = running_slots or int(m.get("llamacpp:requests_processing") or 0)
            d["running"] = running
            d["waiting"] = m.get("llamacpp:requests_deferred", d["waiting"])
            acc_t = m.get("llamacpp:spec_decode_num_accepted_tokens_total")
            dr_t = m.get("llamacpp:spec_decode_num_draft_tokens_total")
            d["mtp_accepted_total"], d["mtp_drafted_total"] = acc_t, dr_t

            # live per-slot signals. The context position grows during prefill
            # AND decode, so on its own it reads the prefill rate as the
            # generation rate (context loading showing up as tok/s of output).
            # n_decoded moves only during generation and
            # n_prompt_tokens_processed only while the prompt is evaluated, so
            # each is a clean phase-specific rate.
            proc_pos = sum((s.get("n_past") or s.get("n_prompt_tokens", 0))
                           for s in slots if s.get("is_processing"))
            proc_dec = sum(_slot_decoded(s) for s in slots if s.get("is_processing"))
            proc_pref = sum((s.get("n_prompt_tokens_processed") or 0)
                            for s in slots if s.get("is_processing"))
            cur = {"ts": now, "running": running, "pos": proc_pos,
                   "dec": proc_dec, "pref": proc_pref,
                   "pred_tok": m.get("llamacpp:tokens_predicted_total"),
                   "pred_s": m.get("llamacpp:tokens_predicted_seconds_total"),
                   "prompt_tok": m.get("llamacpp:prompt_tokens_total"),
                   "prompt_s": m.get("llamacpp:prompt_seconds_total"),
                   "acc": acc_t, "draft": dr_t}
            # keep a short sample history so the rate baseline is a sample from
            # ~2s ago regardless of how many clients are polling /engine (a single
            # `st["prev"]` gets clobbered to a ~0 dt when two pollers interleave).
            hist = st.setdefault("hist", [])
            hist.append(cur)
            st["hist"] = [h for h in hist if now - h["ts"] <= 20.0][-60:]
            prev = st["hist"][-2] if len(st["hist"]) > 1 else None       # most recent
            base_wall = next((h for h in reversed(st["hist"][:-1]) if now - h["ts"] >= 1.8), None)
            active = running > 0
            was_active = bool(st.get("was_active"))
            st["was_active"] = active
            d["active"] = active

            if active:
                if st["sess_start"] is None:
                    st["sess_start"] = dict(cur)
                    st["ctx_peak"] = busiest
                st["ctx_peak"] = max(st.get("ctx_peak", 0), busiest)
            if not active and was_active and st["sess_start"]:
                ss = st["sess_start"]
                st["last_sess"] = {
                    "gen_tok_s": _win_rate(ss, cur, "pred_tok", "pred_s"),
                    "prompt_tok_s": _win_rate(ss, cur, "prompt_tok", "prompt_s"),
                    "mtp_accept_pct": _win_mtp(ss, cur),
                    "ctx_peak": st.get("ctx_peak"),
                }
                st["sess_start"] = None
            ls = st["last_sess"]
            d["gen_tok_s_session"] = ls.get("gen_tok_s")
            d["prompt_tok_s_session"] = ls.get("prompt_tok_s")
            d["mtp_accept_pct_session"] = ls.get("mtp_accept_pct")
            d["ctx_peak_session"] = ls.get("ctx_peak")
            # while idle, surface the last session's peak context (not the ~0 current)
            if not active and ls.get("ctx_peak"):
                d["ctx_tokens"] = ls["ctx_peak"]
                if per_ctx:
                    d["kv_pct"] = round(100.0 * ls["ctx_peak"] / per_ctx, 1)

            life_mtp = round(100.0 * acc_t / dr_t, 1) if (acc_t and dr_t) else None
            if active:
                base = prev if (prev and prev.get("running", 0) > 0) else st["sess_start"]
                g = m.get("llamacpp:predicted_tokens_seconds")
                # live rates from the phase-specific slot counters (~2s window
                # delta; the /metrics counters only move at request end).
                bw = base_wall if (base_wall and base_wall.get("running", 0) > 0) else None
                live = _wall_rate(bw, cur, "dec")
                if live is None and bw is not None:
                    # builds without an n_decoded field: position growth with
                    # the prefill growth netted out is the decode growth —
                    # but only while the prefill counter is quiet, otherwise
                    # the net is just sampling lag between the two counters.
                    if _wall_rate(bw, cur, "pref") is None:
                        net = (cur["pos"] - bw["pos"]) - (cur["pref"] - bw["pref"])
                        dt = cur["ts"] - bw["ts"]
                        if dt > 0.5 and 0 < net < 1e6:
                            live = round(net / dt, 1)
                d["gen_tok_s"] = live or _win_rate(base, cur, "pred_tok", "pred_s") \
                    or (round(g, 1) if g else None) or ls.get("gen_tok_s")
                # prompt rate: the slot counter while the context loads (the
                # /metrics prompt counters only jump at request end), then the
                # session number until the next request.
                d["prompt_tok_s"] = _wall_rate(bw, cur, "pref") \
                    or _win_rate(base, cur, "prompt_tok", "prompt_s") or ls.get("prompt_tok_s")
                d["mtp_accept_pct"] = _win_mtp(base, cur) or ls.get("mtp_accept_pct") or life_mtp
            else:
                d["gen_tok_s"] = ls.get("gen_tok_s")
                d["prompt_tok_s"] = ls.get("prompt_tok_s")
                d["mtp_accept_pct"] = ls.get("mtp_accept_pct") or life_mtp
        elif template.startswith("vllm"):
            # vLLM v1 exposes only monotonic counters (no avg-throughput gauges),
            # so tok/s is a poll-to-poll delta — same windowing as the llama.cpp
            # branch: live rate while a request runs, then hold that session's
            # average until the next one instead of dropping to 0.
            mtext = _http_text(ep + "/metrics")
            m = _prom(mtext)
            running = int(m.get("vllm:num_requests_running") or 0)
            waiting = int(m.get("vllm:num_requests_waiting") or 0)
            d["running"], d["waiting"] = running, waiting
            active = running > 0
            d["active"] = active

            # KV pool capacity (tokens) is a label on cache_config_info; combine
            # with the usage fraction for a live token count next to the %.
            pool = _prom_label(mtext, "vllm:cache_config_info", "kv_cache_size_tokens")
            d["kv_pool_tokens"] = int(pool) if (pool or "").isdigit() else None
            if "vllm:kv_cache_usage_perc" in m:                 # 0..1 fraction of the KV pool
                frac = m["vllm:kv_cache_usage_perc"]
                d["kv_pct"] = round(100.0 * frac, 1)
                if d["kv_pool_tokens"]:
                    d["ctx_tokens"] = round(frac * d["kv_pool_tokens"])

            acc_t = m.get("vllm:spec_decode_num_accepted_tokens_total")
            dr_t = m.get("vllm:spec_decode_num_draft_tokens_total")
            d["mtp_accepted_total"], d["mtp_drafted_total"] = acc_t, dr_t

            cur = {
                "ts": now, "running": running,
                "gen_tok": m.get("vllm:generation_tokens_total"),
                "prompt_tok": m.get("vllm:prompt_tokens_total"),
                # per-request prefill histogram sums -> true prefill throughput
                "pf_tok": m.get("vllm:request_prefill_kv_computed_tokens_sum"),
                "pf_s": m.get("vllm:request_prefill_time_seconds_sum"),
                "acc": acc_t, "draft": dr_t,
            }
            hist = st.setdefault("hist", [])
            hist.append(cur)
            st["hist"] = [h for h in hist if now - h["ts"] <= 20.0][-60:]
            prev = st["hist"][-2] if len(st["hist"]) > 1 else None
            base = next((h for h in reversed(st["hist"][:-1]) if now - h["ts"] >= 1.8), prev)

            was_active = bool(st.get("was_active"))
            st["was_active"] = active
            if active and st["sess_start"] is None:
                st["sess_start"] = dict(cur)
            if not active and was_active and st["sess_start"]:
                ss = st["sess_start"]
                st["last_sess"] = {
                    "gen_tok_s": _wall_rate(ss, cur, "gen_tok"),
                    "prompt_tok_s": _win_rate(ss, cur, "pf_tok", "pf_s")
                                    or _wall_rate(ss, cur, "prompt_tok"),
                    "mtp_accept_pct": _win_mtp(ss, cur),
                }
                st["sess_start"] = None
            ls = st["last_sess"]
            d["gen_tok_s_session"] = ls.get("gen_tok_s")
            d["prompt_tok_s_session"] = ls.get("prompt_tok_s")
            d["mtp_accept_pct_session"] = ls.get("mtp_accept_pct")

            life_mtp = round(100.0 * acc_t / dr_t, 1) if (acc_t and dr_t) else None
            if active:
                b = base if (base and base.get("running", 0) > 0) else st["sess_start"]
                d["gen_tok_s"] = _wall_rate(b, cur, "gen_tok") or ls.get("gen_tok_s")
                d["prompt_tok_s"] = (_win_rate(b, cur, "pf_tok", "pf_s")
                                     or _wall_rate(b, cur, "prompt_tok")
                                     or ls.get("prompt_tok_s"))
                d["mtp_accept_pct"] = _win_mtp(b, cur) or ls.get("mtp_accept_pct") or life_mtp
            else:
                d["gen_tok_s"] = ls.get("gen_tok_s")
                d["prompt_tok_s"] = ls.get("prompt_tok_s")
                d["mtp_accept_pct"] = ls.get("mtp_accept_pct") or life_mtp

        elif template.startswith("sglang"):
            # SGLang (--enable-metrics) exposes live gauges (sglang:gen_throughput
            # tok/s, sglang:token_usage 0..1 KV fill, sglang:spec_accept_rate)
            # AND monotonic token counters. Prefer the gauges while a request
            # runs; hold the last session's numbers when idle (same as vLLM).
            m = _prom(_http_text(ep + "/metrics"))
            running = int(m.get("sglang:num_running_reqs") or 0)
            d["running"] = running
            d["waiting"] = int(m.get("sglang:num_queue_reqs") or 0)
            active = running > 0
            d["active"] = active

            pool = m.get("sglang:max_total_num_tokens")
            d["kv_pool_tokens"] = int(pool) if pool else None
            d["ctx_max"] = (int(m["sglang:context_len"]) if m.get("sglang:context_len")
                            else d["kv_pool_tokens"])
            # sglang:token_usage / kv_used_tokens are fills of the *whole KV pool*
            # (max_total_num_tokens ~= 1.6x the context window here, shared across
            # --max-running-requests and inclusive of retained radix/HiCache prefix
            # pages) -- NOT a single request's position in its window. Derive the
            # live fill in tokens, then express the "context" meter window-relative
            # (matches the ctx_tokens/ctx_max caption + serve._engine's idle path).
            frac = m.get("sglang:token_usage")          # 0..1 fill of the KV pool
            used = m.get("sglang:kv_used_tokens")
            if used:
                d["ctx_tokens"] = int(used)
            elif frac is not None and d["kv_pool_tokens"]:
                d["ctx_tokens"] = round(frac * d["kv_pool_tokens"])
            d["kv_pool_pct"] = round(100.0 * frac, 1) if frac is not None else None
            if d.get("ctx_tokens") is not None and d.get("ctx_max"):
                d["kv_pct"] = round(min(100.0, 100.0 * d["ctx_tokens"] / d["ctx_max"]), 1)
            elif frac is not None:
                d["kv_pct"] = round(100.0 * frac, 1)

            gauge_tps = m.get("sglang:gen_throughput")   # tok/s, live gauge
            acc_rate = m.get("sglang:spec_accept_rate")  # 0..1 while spec active
            acc_len = m.get("sglang:spec_accept_length") # avg accepted draft tok / step

            cur = {"ts": now, "running": running,
                   "gen_tok": m.get("sglang:generation_tokens_total"),
                   "prompt_tok": m.get("sglang:prompt_tokens_total")}
            hist = st.setdefault("hist", [])
            hist.append(cur)
            st["hist"] = [h for h in hist if now - h["ts"] <= 20.0][-60:]
            prev = st["hist"][-2] if len(st["hist"]) > 1 else None
            base = next((h for h in reversed(st["hist"][:-1]) if now - h["ts"] >= 1.8), prev)

            was_active = bool(st.get("was_active"))
            st["was_active"] = active
            if active and st["sess_start"] is None:
                st["sess_start"] = dict(cur)
            mtp_pct = (round(100.0 * acc_rate, 1) if acc_rate
                       else round(25.0 * acc_len, 1) if acc_len else None)  # /4 draft tok
            if mtp_pct:
                st["last_acc_pct"] = mtp_pct
            if not active and was_active and st["sess_start"]:
                ss = st["sess_start"]
                st["last_sess"] = {
                    "gen_tok_s": _wall_rate(ss, cur, "gen_tok"),
                    "prompt_tok_s": _wall_rate(ss, cur, "prompt_tok"),
                    "mtp_accept_pct": st.get("last_acc_pct"),
                }
                st["sess_start"] = None
            ls = st["last_sess"]
            d["gen_tok_s_session"] = ls.get("gen_tok_s")
            d["prompt_tok_s_session"] = ls.get("prompt_tok_s")
            d["mtp_accept_pct_session"] = ls.get("mtp_accept_pct")
            if active:
                b = base if (base and base.get("running", 0) > 0) else st["sess_start"]
                d["gen_tok_s"] = ((gauge_tps if gauge_tps and gauge_tps > 0 else None)
                                  or _wall_rate(b, cur, "gen_tok") or ls.get("gen_tok_s"))
                d["prompt_tok_s"] = _wall_rate(b, cur, "prompt_tok") or ls.get("prompt_tok_s")
                d["mtp_accept_pct"] = mtp_pct or ls.get("mtp_accept_pct")
            else:
                d["gen_tok_s"] = ls.get("gen_tok_s")
                d["prompt_tok_s"] = ls.get("prompt_tok_s")
                d["mtp_accept_pct"] = ls.get("mtp_accept_pct") or st.get("last_acc_pct")
    except Exception as e:  # noqa: BLE001 — telemetry is best-effort
        d["error"] = str(e)
    return d


def engine_counters(endpoint: str, template: str) -> dict | None:
    """Raw cumulative counters for the throughput sampler (serve._engine_sampler).

    Unlike `engine_telemetry` (which smooths into live rates) this returns the
    monotonic totals straight off `/metrics`, so the sampler can take honest
    poll-to-poll deltas and fold them token-weighted into `engine_daily`:

      gen_tok / gen_s     - generation tokens / decode seconds. gen_s is
                            counter-based for llama.cpp (`*_seconds_total`) and
                            SGLang (`inter_token_latency_seconds_sum`, streaming);
                            vanilla vLLM has no such counter, so there the sampler
                            falls back to wall-clock while a request is in flight.
      prompt_tok / prefill_s - prompt tokens prefilled / prefill seconds
                            (counter-based for all three engines).
      ctx_tokens          - current KV fill in tokens (for a running peak).
      running             - in-flight request count (0 => idle).

    None if the endpoint is unreachable / the template is unknown. Individual
    keys are None when that engine doesn't expose them."""
    ep = endpoint.rstrip("/")
    out: dict = {"gen_tok": None, "gen_s": None, "prompt_tok": None,
                 "prefill_s": None, "ctx_tokens": None, "running": None}
    try:
        if template.startswith("llamacpp"):
            m = _prom(_http_text(ep + "/metrics"))
            out["gen_tok"] = m.get("llamacpp:tokens_predicted_total")
            out["gen_s"] = m.get("llamacpp:tokens_predicted_seconds_total")
            out["prompt_tok"] = m.get("llamacpp:prompt_tokens_total")
            out["prefill_s"] = m.get("llamacpp:prompt_seconds_total")
            out["running"] = int(m.get("llamacpp:requests_processing") or 0)
            try:
                slots = _http_json(ep + "/slots")
                slots = slots if isinstance(slots, list) else slots.get("slots", [])
                out["ctx_tokens"] = max(
                    ((s.get("n_past") or s.get("n_prompt_tokens", 0)) for s in slots),
                    default=0) or None
            except Exception:  # noqa: BLE001 — /slots may be disabled
                pass
        elif template.startswith("vllm"):
            mtext = _http_text(ep + "/metrics")
            m = _prom(mtext)
            out["gen_tok"] = _prom_sum(mtext, "vllm:generation_tokens_total")
            # real KV-computed prefill tokens (excludes vLLM's own prefix-cache
            # hits) paired with the matching time sum -> honest prefill tok/s
            out["prompt_tok"] = (_prom_sum(mtext, "vllm:request_prefill_kv_computed_tokens_sum")
                                 or _prom_sum(mtext, "vllm:prompt_tokens_total"))
            out["prefill_s"] = _prom_sum(mtext, "vllm:request_prefill_time_seconds_sum")
            out["running"] = int(_prom_sum(mtext, "vllm:num_requests_running") or 0)
            frac = m.get("vllm:kv_cache_usage_perc")
            pool = _prom_label(mtext, "vllm:cache_config_info", "kv_cache_size_tokens")
            if frac is not None and (pool or "").isdigit():
                out["ctx_tokens"] = round(frac * int(pool))
        elif template.startswith("sglang"):
            t = _http_text(ep + "/metrics")
            # decode: pair streaming gen tokens with the inter-token-latency sum
            # (both streaming-domain) -> a real cumulative decode-seconds counter,
            # so a whole request between polls is still measured. Falls back to
            # the total gen counter when the fork doesn't expose ITL.
            out["gen_tok"] = (_prom_sum(t, "sglang:generation_tokens_total", is_streaming="true")
                              or _prom_sum(t, "sglang:generation_tokens_total"))
            out["gen_s"] = _prom_sum(t, "sglang:inter_token_latency_seconds_sum")
            # prefill: the `prefill_forward` stage wall time is the cost to turn
            # the WHOLE prompt into a decode-ready state — it includes restoring
            # KV pages from the radix/HiCache/NIXL tiers, not just fresh compute.
            # So the numerator must be every token that stage made ready
            # (compute + cache), else a prefix hit craters the rate (a few
            # thousand computed tokens over compute+restore seconds). This is
            # "effective" prefill throughput — how fast the prompt is ingested,
            # cache included — and it matches the live card's basis.
            _pf_c = _prom_sum(t, "sglang:realtime_tokens_total", mode="prefill_compute")
            _pf_h = _prom_sum(t, "sglang:realtime_tokens_total", mode="prefill_cache")
            out["prompt_tok"] = (((_pf_c or 0) + (_pf_h or 0))
                                 or _prom_sum(t, "sglang:prompt_tokens_total"))
            out["prefill_s"] = (_prom_sum(t, "sglang:per_stage_req_latency_seconds_sum",
                                          stage="prefill_forward")
                                or _prom_sum(t, "sglang:time_to_first_token_seconds_sum"))
            out["running"] = int(_prom_sum(t, "sglang:num_running_reqs") or 0)
            used = _prom_sum(t, "sglang:kv_used_tokens") or _prom_sum(t, "sglang:num_used_tokens")
            pool = _prom_sum(t, "sglang:max_total_num_tokens")
            frac = _prom_sum(t, "sglang:token_usage")
            if used:
                out["ctx_tokens"] = int(used)
            elif frac is not None and pool:
                out["ctx_tokens"] = round(frac * pool)
        else:
            return None
    except Exception:  # noqa: BLE001 — best-effort
        return None
    return out
