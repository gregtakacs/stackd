"""The pool / device balance math — one place, used by both the solver (to test
'does adding this model fit') and the validator (to report a full breakdown)."""

from __future__ import annotations

import os
import time

from stackd.config.models import Config

_EPS = 1e-6

# ---------------------------------------------------------------- effective ceiling
# `IGPU_VRAM_BUDGET_GIB` is a config *wish*, not a measurement. Nothing checked it
# against the driver, so a typo'd 90 on a box whose amdgpu driver exposes a
# 62.2 GiB GTT window sailed through `stackctl validate` and had the solver book
# min(demand + headroom, 90) — the dashboard already clamped to the real window,
# so the UI said "you may claim 62" while the server blessed a plan that claimed
# up to 90 out of a 128 GiB pool. Every reader of a device's ceiling now goes
# through budget_facts() below, so the clamp exists exactly once.
#
# The window is a DRIVER limit, not silicon: `mem_info_gtt_total` is
# `ttm.pages_limit` expressed in bytes, and the kernel auto-sets that to half of
# MemTotal (130493404 kB / 2 = 62.224 GiB = 66812620800 B here). A budget above
# half of RAM is therefore not a hardware impossibility, just an un-booted knob —
# see gpu_discovery.gtt_window_gib() for the GRUB arg that raises it.
_WINDOW_TTL_S = 300.0          # GTT total is fixed at driver load; re-probe rarely
_windows: dict[int, tuple[float, float | None]] = {}   # index -> (at, GiB | None)


def _gtt_probe_enabled() -> bool:
    """Same kill-switch as the loader's render-node autodetect: the smoke suite
    sets STACKD_DISABLE_GPU_AUTODETECT=1 so expectations are machine-independent
    (see config/loader._gpu_autodetect_enabled). Unknown window -> no clamp."""
    return os.environ.get("STACKD_DISABLE_GPU_AUTODETECT", "").lower() not in ("1", "true", "yes")


def gtt_window_gib(index: int = 0) -> float | None:
    """Measured GTT window of `igpu<index>` in GiB, or None when it can't be known.

    Cached (it doesn't move without a reboot) and safe everywhere: no amdgpu card,
    no /sys, CI, or a non-AMD box all return None, which the caller must read as
    UNKNOWN — never as 0 — so the configured budget stands. Tests pin the value
    with STACKD_GTT_WINDOW_GIB rather than faking sysfs."""
    over = os.environ.get("STACKD_GTT_WINDOW_GIB", "").strip()
    if over:
        try:
            v = float(over)
        except ValueError:
            return None
        return v if v > 0 else None
    if not _gtt_probe_enabled():
        return None
    now = time.time()
    hit = _windows.get(index)
    if hit and now - hit[0] < _WINDOW_TTL_S:
        return hit[1]
    try:
        from stackd.gpu_discovery import gtt_window_gib as probe
        val = probe(index=index)
    except OSError:      # sysfs unreadable mid-call — treat as unknown
        val = None
    _windows[index] = (now, val)
    return val


def _device_index(name: str) -> int:
    """`igpu1` -> 1. A budgeted device with no trailing digit is the first AMD
    card: a device with `vram_budget_gib` set is by definition a shared-pool
    (GTT) device, and on an AMD box there is exactly one such thing."""
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


def cap_str(x: float) -> str:
    """Ceiling as printed in labels: 90 not 90.0, but 62.2 keeps its decimal."""
    return f"{int(x)}" if float(x).is_integer() else f"{x:g}"


def budget_facts(cfg: Config, device: str) -> dict:
    """THE ceiling for one device: {"configured", "window", "effective"}.

    - a dedicated-pool device (no `vram_budget_gib`) is its pool total, unclamped
      — the GTT window says nothing about a discrete NVIDIA card's GDDR;
    - a shared-pool (GTT) device gets min(configured, measured window), and only
      when the window is actually measured. `IGPU_VRAM_BUDGET_GIB=1` (the
      no-iGPU sentinel, deploy/README) stays 1 either way: the clamp can only
      lower a budget, never raise one, and nothing divides by it.
    """
    d = cfg.devices[device]
    if d.vram_budget_gib is None:
        return {"configured": cfg.pools[d.vram_pool].total_gib,
                "window": None, "effective": cfg.pools[d.vram_pool].total_gib}
    win = gtt_window_gib(_device_index(d.name)) if d.vram_budget_gib else None
    eff = d.vram_budget_gib
    if win is not None and win > 0:
        eff = min(eff, win)
    return {"configured": d.vram_budget_gib, "window": win, "effective": eff}


def device_ceiling(cfg: Config, device: str) -> float:
    """Effective ceiling — clamped to the measured GTT window where known."""
    return budget_facts(cfg, device)["effective"]


def device_configured_ceiling(cfg: Config, device: str) -> float:
    """The raw config number, for labels that must say what the operator asked
    for when the effective value has been clamped below it."""
    return budget_facts(cfg, device)["configured"]


def _is_dedicated(cfg: Config, pool_name: str) -> bool:
    p = cfg.pools[pool_name]
    devs = [d for d in cfg.devices.values() if d.vram_pool == pool_name]
    return (p.host_reserve_gib == 0 and p.load_slack_gib == 0
            and all(d.vram_budget_gib is None for d in devs))


def pool_charge(cfg: Config, pool_name: str, dev_vram: dict[str, float],
                dev_ram: dict[str, float]) -> tuple[float, dict[str, float]]:
    """(total charged to the pool, breakdown)."""
    p = cfg.pools[pool_name]
    pool_devs = [d for d in cfg.devices.values() if d.vram_pool == pool_name]
    bd: dict[str, float] = {}
    if _is_dedicated(cfg, pool_name):
        used = 0.0
        for d in pool_devs:
            if dev_vram.get(d.name):
                bd[f"vram:{d.name}"] = round(dev_vram[d.name], 2)
            used += dev_vram.get(d.name, 0.0)
        return round(used, 2), bd

    used = p.host_reserve_gib + p.load_slack_gib
    if p.host_reserve_gib:
        bd["host_reserve"] = p.host_reserve_gib
    if p.load_slack_gib:
        bd["load_slack"] = p.load_slack_gib
    for d in pool_devs:
        # The ceiling is `budget_facts`' effective one — the configured budget
        # clamped to the GTT window the driver actually exposes. Trusting the
        # config number here is how a mistuned IGPU_VRAM_BUDGET_GIB books pool
        # RAM the hardware can't hand over and then OOMs the host; the label
        # follows the same number so what the breakdown says is what was booked.
        facts = budget_facts(cfg, d.name)
        ceiling = facts["effective"] if d.vram_budget_gib is not None else p.total_gib
        demand = dev_vram.get(d.name, 0.0)
        counted = min(demand + d.vram_headroom_gib, ceiling)
        bd[f"vram:{d.name} (≤{cap_str(ceiling)})"] = round(counted, 2)
        used += counted
    for dname, r in dev_ram.items():
        if r and cfg.devices[dname].vram_pool != pool_name:
            bd[f"ram:{dname}"] = round(r, 2)
            used += r
    return round(used, 2), bd


def check(cfg: Config, dev_vram: dict[str, float], dev_ram: dict[str, float]):
    """Returns (ok, {pool: (used, limit, breakdown)}, {device: (used, ceiling)})."""
    devs = {d: (round(dev_vram.get(d, 0.0), 2), round(device_ceiling(cfg, d), 2))
            for d in cfg.devices}
    pools = {}
    ok = all(u <= c + _EPS for u, c in devs.values())
    for pn in cfg.pools:
        used, bd = pool_charge(cfg, pn, dev_vram, dev_ram)
        pools[pn] = (used, round(cfg.pools[pn].total_gib, 2), bd)
        if used > cfg.pools[pn].total_gib + _EPS:
            ok = False
    return ok, pools, devs
