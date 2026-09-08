"""Measure the real VRAM an image pipeline actually holds during a generation —
the number `config/media/image.yaml` `footprint_gib` should carry, instead of a
hand guess.

`stackctl image bench <model>` makes the model resident on the elastic tier
(via the daemon), then submits one real `generate` per requested size while a
background thread samples `nvidia-smi` — reporting the peak total VRAM and the
peak held by the ComfyUI process specifically (the part that competes with an
LLM for the device).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.request

from stackd.imagegen.comfyui_client import get_json, post_json, submit_workflow
from stackd.imagegen import workflows

GEN_TIMEOUT_S = 200.0   # default per-generation cap — abort rather than hang.
# A cold first generation on a big model (Flux.2-dev on the Vulkan iGPU stages
# ~51 GiB of weights before the first step) blows past this; pass a larger
# --gen-timeout, or rely on the warm-up pass below so the measured run is warm.


class BenchAborted(RuntimeError):
    pass

DEFAULT_PROMPT = ("a photograph of a wooden desk by a window — a laptop, a ceramic "
                  "mug, a small potted plant, and a stack of books, soft daylight")


def _nvsmi(args: list[str]) -> str:
    from stackd.runner import _nvidia_smi
    return _nvidia_smi(args, timeout=20) or ""   # per-process queries are slow under load


def _total_used_mib() -> float:
    out = _nvsmi(["--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    try:
        return float(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def _igpu_used_mib() -> float:
    """AMD iGPU total used (vram+gtt) via amdgpu sysfs, in MiB -- the vulkan/ROCm
    equivalent of _total_used_mib(). `_nvidia_smi` only ever sees the discrete
    NVIDIA card, so a bench running on igpu0 must NOT fall through to it (that
    silently reports the RTX's unrelated usage -- including any co-resident LLM
    there -- instead of the iGPU's). Best-effort: 0.0 if sysfs isn't readable."""
    from stackd.telemetry import _igpu0_sysfs
    d = _igpu0_sysfs()
    if not d or d.get("vram_used_gib") is None:
        return 0.0
    return d["vram_used_gib"] * 1024  # GiB -> MiB, matching _total_used_mib()'s unit


def _comfy_used_mib() -> float:
    """Sum of GPU memory held by the ComfyUI process(es) — the python compute
    apps (the LLM shows up as llama-server / vllm, not python)."""
    out = _nvsmi(["--query-compute-apps=used_memory,process_name",
                  "--format=csv,noheader,nounits"])
    total = 0.0
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and "python" in parts[1].lower():
            try:
                total += float(parts[0])
            except ValueError:
                pass
    return total


def _host_used_mib() -> float:
    """Whole-machine RAM in use (MemTotal - MemAvailable), in MiB -- same source
    telemetry.py's host_stats() reads for the dashboard. cuda0's own generate
    already touches host RAM (CLIP/VAE staging, page cache for the safetensors);
    on the iGPU it matters far more, since GTT is carved straight from system
    RAM -- the "vulkan" VRAM figure above IS mostly a host RAM charge already,
    but this catches whatever ISN'T counted there (comfyui's own process RSS,
    non-GTT staging buffers) so footprint_gib + this can be checked against
    host_unified headroom too, not just the device pool."""
    try:
        mi: dict[str, float] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                mi[k] = float(v.strip().split()[0]) / 1024  # kB -> MiB
        if "MemTotal" not in mi:
            return 0.0
        avail = mi.get("MemAvailable", mi.get("MemFree", 0.0))
        return mi["MemTotal"] - avail
    except (OSError, ValueError, IndexError):
        return 0.0


def _container_mem_mib(container_name: str) -> float:
    """The ComfyUI container's OWN cgroup memory (MiB) via the Docker Engine API
    (`DOCKER_API_URL`, the scoped socket-proxy) — the slice of host RAM this
    container is actually responsible for, isolated from the co-resident LLMs and
    the rest of the box. This is what `host_ram_gib` should be built from: the
    whole-machine `MemTotal - MemAvailable` delta above also swings with page
    cache and any other resident process, so it over-books ComfyUI's share (the
    70G-vs-53G gap on flux2-dev-turbo). Best-effort: 0.0 if DOCKER_API_URL is
    unset or the stats call fails — bench_image then falls back to the
    whole-machine delta so a bench never reports no host-RAM number at all."""
    base = os.environ.get("DOCKER_API_URL", "").rstrip("/")
    if not base or not container_name:
        return 0.0
    try:
        with urllib.request.urlopen(
                f"{base}/containers/{container_name}/stats?stream=false", timeout=20) as r:
            st = json.loads(r.read() or b"{}")
        return float(st.get("memory_stats", {}).get("usage", 0)) / (1024 * 1024)
    except Exception:  # noqa: BLE001 — telemetry must never fail the bench
        return 0.0


def _container_name_from_endpoint(endpoint: str) -> str | None:
    """`http://comfyui-cuda:8188` -> `comfyui-cuda`. The ComfyUI endpoint's host
    is the docker service name, which matches the container name stackd creates
    (see image.yaml `containers.<backend>.name`), so it can be queried at the
    socket-proxy directly."""
    m = re.match(r"https?://([^/:]+)", (endpoint or "").strip())
    return m.group(1) if m else None


class _VramSampler(threading.Thread):
    def __init__(self, interval: float = 0.4, backend: str = "cuda",
                 container: str | None = None):
        super().__init__(daemon=True)
        self.interval = interval
        self.backend = backend
        self.container = container
        self._stop = threading.Event()
        self.peak_total = 0.0
        self.peak_comfy = 0.0
        self.peak_host = 0.0
        self.peak_container = 0.0

    def reset(self):
        self.peak_total = 0.0
        self.peak_comfy = 0.0
        self.peak_host = 0.0
        self.peak_container = 0.0

    def run(self):
        while not self._stop.wait(self.interval):
            self.peak_total = max(self.peak_total, total_used_mib(self.backend))
            self.peak_comfy = max(self.peak_comfy, comfy_used_mib(self.backend))
            self.peak_host = max(self.peak_host, _host_used_mib())
            if self.container:
                self.peak_container = max(self.peak_container, _container_mem_mib(self.container))

    def stop(self):
        self._stop.set()


def total_used_mib(backend: str) -> float:
    return _igpu_used_mib() if backend == "vulkan" else _total_used_mib()


def comfy_used_mib(backend: str) -> float:
    # No per-process breakdown for the iGPU: unified memory has no rocm-smi
    # equivalent of `nvidia-smi --query-compute-apps` wired up here. Report the
    # device total instead of a bogus 0 -- still useful as "how full is igpu0".
    return _igpu_used_mib() if backend == "vulkan" else _comfy_used_mib()


def _set_primitive(graph: dict, node_id: str | None, value) -> None:
    """The size/seed roles point at PrimitiveInt nodes whose input key is
    `value`, not the role name — set it directly."""
    n = graph.get(node_id or "")
    if n is not None:
        n.setdefault("inputs", {})["value"] = value


async def _queue_ids(base: str) -> set:
    q = await get_json(f"{base.rstrip('/')}/queue", timeout=15)
    return {e[1] for e in (q.get("queue_running", []) + q.get("queue_pending", []))
            if isinstance(e, list) and len(e) > 1}


async def _abort_queue(base: str) -> None:
    b = base.rstrip("/")
    for call in (post_json(f"{b}/interrupt", {}, timeout=10),
                 post_json(f"{b}/queue", {"clear": True}, timeout=10)):
        try:
            await call
        except Exception:  # noqa: BLE001
            pass


async def _one_generation(model: str, px: int, prompt: str, base: str,
                          timeout_s: float = GEN_TIMEOUT_S) -> float:
    _name, graph, nodes, _entry = workflows.load_model("generate", model)
    seed = int(time.time() * 1000) % (2 ** 31 - 1)
    nid = nodes.get("positive") or nodes.get("prompt")
    if nid:
        workflows.set_node(graph, nid, "positive", prompt)
    _set_primitive(graph, nodes.get("width"), px)
    _set_primitive(graph, nodes.get("height"), px)
    _set_primitive(graph, nodes.get("seed"), seed)

    t0 = time.monotonic()
    pid = await submit_workflow(graph, base)
    # completion: either the prompt shows up in /history, or (fallback, since the
    # scratch janitor can clear /history mid-poll) it was seen in the queue and
    # has since left it. Never return before having positive evidence it ran.
    deadline = time.monotonic() + timeout_s
    seen_in_queue = False
    b = base.rstrip("/")
    while True:
        try:
            hist = await get_json(f"{b}/history/{pid}", timeout=15)
            if pid in hist:
                return round(time.monotonic() - t0, 1)
        except Exception:  # noqa: BLE001
            pass
        ids = await _queue_ids(base)
        if pid in ids:
            seen_in_queue = True
        elif seen_in_queue:
            return round(time.monotonic() - t0, 1)
        if time.monotonic() > deadline:
            await _abort_queue(base)
            raise BenchAborted(
                f"{px}px generation exceeded {timeout_s:.0f}s — queue interrupted. "
                f"Raise --gen-timeout, or the model may be thrashing weight reloads "
                f"under host-RAM pressure (check `docker logs` for repeated model loads).")
        await asyncio.sleep(1.5)


async def bench_image(model: str, sizes: list[int], prompt: str, base: str,
                      backend: str = "cuda", gen_timeout: float = GEN_TIMEOUT_S,
                      warmup: int = 1) -> dict:
    container = _container_name_from_endpoint(base)
    sampler = _VramSampler(backend=backend, container=container)
    sampler.start()
    time.sleep(0.6)
    # Baseline is read HERE, before any generation (warm-up included) — the
    # vulkan footprint math is peak − baseline, so the model's weights must not
    # be staged yet or they'd fall out of the delta.
    baseline_total = total_used_mib(backend)
    baseline_comfy = comfy_used_mib(backend)
    baseline_host = _host_used_mib()
    points = []
    try:
        # Warm-up: a cold first generation stages/compiles weights (tens of GiB
        # on a big model) and is neither representative nor reliably under the
        # timeout. Run it, throw the timing away, then measure warm — the number
        # `footprint_gib` wants is the settled resident+active peak anyway. The
        # warm-up gets a generous cap since it's the one expected to be slow.
        for i in range(max(0, warmup)):
            wpx = sizes[0]
            print(f"  warm-up {i + 1}/{warmup} ({wpx}²) — not measured …", flush=True)
            try:
                wsecs = await _one_generation(model, wpx, prompt, base,
                                              timeout_s=max(gen_timeout, 900.0))
                print(f"  warm-up {i + 1}: {wsecs}s", flush=True)
            except Exception as e:  # noqa: BLE001 — a bad warm-up still lets the measured run try
                print(f"  warm-up {i + 1}: {e} — measuring anyway", flush=True)
        for px in sizes:
            sampler.reset()
            print(f"  {px}²: generating …", flush=True)
            try:
                secs = await _one_generation(model, px, prompt, base, timeout_s=gen_timeout)
            except BenchAborted as e:
                print(f"  {px}²: ABORTED — {e}", flush=True)
                points.append({"px": px, "ok": False, "error": str(e)})
                break   # one hang means the rest will hang too — stop
            except Exception as e:  # noqa: BLE001
                print(f"  {px}²: FAILED — {e}", flush=True)
                points.append({"px": px, "ok": False, "error": str(e)})
                continue
            time.sleep(1.0)   # catch a late VAE spike, then read the resident level
            added = max(0.0, (sampler.peak_total - baseline_total) / 1024)
            added_host = max(0.0, (sampler.peak_host - baseline_host) / 1024)
            pt = {"px": px, "ok": True, "secs": secs,
                  "peak_total_gib": round(sampler.peak_total / 1024, 2),
                  "added_gib": round(added, 2),                       # peak_total − baseline: the pipeline's own footprint
                  "peak_comfy_gib": round(sampler.peak_comfy / 1024, 2),
                  "resident_comfy_gib": round(comfy_used_mib(backend) / 1024, 2),
                  "peak_host_gib": round(sampler.peak_host / 1024, 2),
                  "added_host_gib": round(added_host, 2),
                  "container_peak_gib": round(sampler.peak_container / 1024, 2)}
            points.append(pt)
            host_note = (f"· container {pt['container_peak_gib']}G "
                         if pt["container_peak_gib"] else "")
            print(f"  {px}²: {secs}s · added {pt['added_gib']}G "
                  f"· total peak {pt['peak_total_gib']}G "
                  f"· comfy-pid {pt['peak_comfy_gib']}G "
                  f"{host_note}· host RAM +{pt['added_host_gib']}G (peak {pt['peak_host_gib']}G)",
                  flush=True)
    finally:
        sampler.stop()
    ok_pts = [p for p in points if p.get("ok")]
    peak_total = max((p["peak_total_gib"] for p in ok_pts), default=0.0)
    peak_comfy = max((p["peak_comfy_gib"] for p in ok_pts), default=0.0)
    peak_host = max((p["peak_host_gib"] for p in ok_pts), default=0.0)
    added_peak = max((p["added_gib"] for p in ok_pts), default=0.0)
    added_host = max((p["added_host_gib"] for p in ok_pts), default=0.0)
    peak_container = max((p.get("container_peak_gib", 0.0) for p in ok_pts), default=0.0)
    if backend == "vulkan":
        # igpu0 shares VRAM/GTT with system RAM, and whatever else is placed
        # there (e.g. code-autocomplete) can be co-resident during a bench.
        # _pick_image() already subtracts THAT separately (via the LLM-placement
        # headroom check), so using the device's raw total here double-counts
        # it on top -- delta-vs-baseline isolates just this pipeline's own
        # cost instead. Reliable IFF the container was freshly restarted right
        # before this bench (an in-place-swapped-over baseline is its own,
        # separate contamination risk -- always `docker rm -f` the container
        # before benching the next model, never swap in place then bench).
        footprint = added_peak
    else:
        # cuda0 benches run on a device cleared of LLMs (bench-image profile),
        # so total VRAM on the card IS this pipeline's footprint bar a ~1G
        # idle ComfyUI base.
        footprint = max(peak_total - 1.0, peak_comfy, 0.0)
    # host RAM: the container's OWN cgroup peak (`peak_container` — what the
    # ComfyUI container actually held, isolated from the co-resident LLMs and the
    # box's page cache) is the honest figure for `host_ram_gib`. The whole-
    # machine `added_host` delta over-books it: `MemTotal - MemAvailable` also
    # swings with page cache and any other resident process (the 70G-reserved-
    # vs-53G-held gap on flux2-dev-turbo). Fall back to the whole-machine delta
    # when the Docker API is unreachable (no DOCKER_API_URL, e.g. stackd running
    # on the host) so a bench still reports a number. Both are checked live
    # against real remaining host RAM by reconciler.py::_real_host_ram_avail on
    # every pick/swap.
    host_ram_base = peak_container if peak_container > 0 else added_host
    return {
        "model": model,
        "baseline_total_gib": round(baseline_total / 1024, 2),
        "peak_total_gib": round(peak_total, 2),
        "peak_comfy_gib": peak_comfy,
        "footprint_gib": round(footprint, 1),
        "suggest_footprint_gib": int(footprint + 3 + 0.999),   # ceil + 3 GiB cushion
        "baseline_host_gib": round(baseline_host / 1024, 2),
        "peak_host_gib": peak_host,
        "added_host_gib": added_host,
        "container_peak_gib": peak_container,
        "suggest_host_ram_gib": int(host_ram_base + 3 + 0.999),   # ceil + 3 GiB cushion
        "points": points,
    }
