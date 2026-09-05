"""Footprint benchmarking (P0). Spawn a stack at a few context points, sample
VRAM + process RAM at idle-after-warm, fit a line per axis, write a catalog
curve. `ingest` skips the spawning and fits from hand-measured points; `verify`
spawns a whole profile and checks the prediction against reality.

The measurement needs the real box; the fit / catalog write / ingest / signature
logic is exercised by tests with a `FakeSampler`.
"""

from __future__ import annotations

import copy
import datetime
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from stackd.catalog import Catalog, curve_key, primary_device
from stackd.config.models import Config
from stackd.engines.registry import adapter_for
from stackd.runner import DeviceKnobs, LaunchContext, Mount, Runner
from stackd.validator import validate_profile

_WARM_PROMPT = {"messages": [{"role": "user", "content": "ok"}], "max_tokens": 1, "stream": False}


class Sampler(Protocol):
    def vram_used_gib(self, backend: str, index: int) -> float: ...
    def proc_ram_gib(self, pid: int) -> float: ...


class NvidiaProcSampler:
    def vram_used_gib(self, backend: str, index: int) -> float:
        if backend == "cuda":
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                 "-i", str(index)], capture_output=True, text=True, timeout=15).stdout
            return round(float(out.strip().splitlines()[0]) / 1024, 2)
        # integrated AMD GPU: amdgpu sysfs first (no tool needed), then rocm-smi
        from stackd.telemetry import _igpu0_sysfs
        s = _igpu0_sysfs()
        if s and s.get("vram_used_gib") is not None:
            return round(s["vram_used_gib"], 2)
        try:
            out = subprocess.run(["rocm-smi", "--showmemuse", "--json"],
                                 capture_output=True, text=True, timeout=15).stdout
            digits = "".join(c for c in out if c.isdigit())
            return round(int(digits or 0) / 1024**3, 2)
        except (FileNotFoundError, subprocess.SubprocessError):
            return 0.0

    def proc_ram_gib(self, pid: int) -> float:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return round(int("".join(filter(str.isdigit, line))) / 1024**2, 2)
        except OSError:
            return 0.0
        return 0.0


@dataclass
class FakeSampler:
    """vram(ctx) and ram(ctx) are linear in the test so the fit is exact."""
    vram0: float = 30.0
    vram_per_ktok: float = 0.04
    ram0: float = 2.0
    ram_per_ktok: float = 0.005
    _ctx: float = 0.0

    def for_ctx(self, ctx: float) -> None:
        self._ctx = ctx

    def vram_used_gib(self, backend: str, index: int) -> float:
        return round(self.vram0 + self.vram_per_ktok * self._ctx / 1000, 2)

    def proc_ram_gib(self, pid: int) -> float:
        return round(self.ram0 + self.ram_per_ktok * self._ctx / 1000, 2)


def _wait_ready(runner: Runner, url: str | None, timeout: float,
                handle: str | None = None) -> bool:
    if not url:
        time.sleep(0.1)
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if runner.http_ok(url):
            return True
        if handle is not None:
            code = runner.poll(handle)
            if code is not None:                       # container exited / gone
                raise RuntimeError(f"engine container exited (code {code}) before it "
                                   f"became ready — check the image / GPU access")
        time.sleep(1.0)
    return False


def _warm(endpoint: str | None) -> None:
    if not endpoint:
        return
    try:
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/chat/completions",
            data=str(_WARM_PROMPT).replace("'", '"').encode(),
            headers={"content-type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=60).read()
    except Exception:
        pass


def bench_stack(
    cfg: Config, stack_name: str, runner: Runner, sampler: Sampler, catalog: Catalog,
    *, ctx_points: list[int], models_dir: str = "/models", port: int = 11590,
    source: str = "measured",
):
    st0 = cfg.models[stack_name]
    device = primary_device(cfg, stack_name)
    dev = cfg.devices[device]
    idx = getattr(dev, "index", 0)
    base_vram = sampler.vram_used_gib(dev.backend.value, idx)
    vram_pts: list[tuple[int, float]] = []
    ram_pts: list[tuple[int, float]] = []

    lc_mounts = [Mount(m.host_path, m.container_path, m.ro) for m in cfg.runtime.mounts]
    # per-backend container knobs (GPU access etc.) — same as the reconciler applies
    dp = cfg.runtime.device_profiles.get(dev.backend.value)
    knobs = DeviceKnobs(
        gpus=dp.gpus, devices=list(dp.devices), group_add=list(dp.group_add),
        security_opt=list(dp.security_opt), ipc_host=dp.ipc_host,
        shm_size=dp.shm_size, env=dict(dp.env),
    ) if dp else DeviceKnobs()
    for ctx in ctx_points:
        st = copy.deepcopy(st0)
        if "ctx" in st.engine.params or st.engine.template.startswith("llamacpp"):
            st.engine.params["ctx"] = ctx
        st.engine.container.name = f"stackd-bench-{stack_name}"
        adapter = adapter_for(st, dev)
        p = port if st.engine.template.startswith("llamacpp") else None
        spec = adapter.launch_spec(LaunchContext(
            host_models_dir=cfg.runtime.host_models_dir or models_dir,
            network=cfg.runtime.network, port=p, device_index=idx,
            images=dict(cfg.runtime.images), extra_mounts=lc_mounts, device=knobs,
        ))
        if hasattr(sampler, "for_ctx"):
            sampler.for_ctx(ctx)  # FakeSampler

        handle = runner.spawn(spec)
        try:
            _wait_ready(runner, spec.health_url, min(spec.ready_timeout_s, 900), handle)
            _warm(adapter.endpoint(p))
            vram_pts.append((ctx, round(sampler.vram_used_gib(dev.backend.value, idx) - base_vram, 2)))
            ram_pts.append((ctx, sampler.proc_ram_gib(-1)))
        finally:
            # honour the model's stop_grace_s (vLLM needs a clean worker shutdown —
            # a 5s SIGKILL mid-teardown is what wedges the RTX); else fast on cuda.
            grace = spec.stop_grace_s if spec.stop_grace_s is not None else (
                5 if dev.backend.value == "cuda" else 20)
            runner.stop(handle, remove=True, timeout=grace)
            time.sleep(0.2)

    return catalog.write_curve(
        curve_key(cfg, stack_name),
        model=st0.engine.model or st0.engine.params.get("active_model", stack_name),
        engine=st0.engine.template, device=device,
        vram_points=vram_pts, ram_points=ram_pts, source=source,
        measured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        notes=f"stackctl bench {stack_name} @ ctx {ctx_points}",
    )


def ingest(cfg: Config, stack_name: str, catalog: Catalog, points: dict, *, notes: str = ""):
    """points = {"vram": [[ctx, gib], ...], "ram": [[ctx, gib], ...]}"""
    st = cfg.models[stack_name]
    return catalog.write_curve(
        curve_key(cfg, stack_name),
        model=st.engine.model or st.engine.params.get("active_model", stack_name),
        engine=st.engine.template, device=primary_device(cfg, stack_name),
        vram_points=points.get("vram", []), ram_points=points.get("ram", []),
        source="measured", measured_at=None,
        notes=notes or f"ingested for {stack_name}",
    )


def verify(cfg: Config, profile: str, catalog: Catalog, measured_pools: dict[str, float],
           *, tol_gib: float = 2.0) -> dict:
    """Diff the catalog prediction against measured per-pool totals (the caller
    spawns the profile live and samples). Design-note P0 done-when: within 2 GiB."""
    pred = validate_profile(cfg, profile, catalog=catalog)
    rows = []
    worst = 0.0
    for p in pred.pools:
        m = measured_pools.get(p.pool)
        if m is None:
            continue
        d = round(m - p.used_gib, 2)
        worst = max(worst, abs(d))
        rows.append({"pool": p.pool, "predicted": p.used_gib, "measured": m,
                     "delta_gib": d, "ok": abs(d) <= tol_gib})
    return {"profile": profile, "tol_gib": tol_gib, "worst_gib": round(worst, 2),
            "pass": worst <= tol_gib, "pools": rows, "unmeasured": pred.unmeasured}
