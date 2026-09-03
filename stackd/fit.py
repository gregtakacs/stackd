"""The pool / device balance math — one place, used by both the solver (to test
'does adding this model fit') and the validator (to report a full breakdown)."""

from __future__ import annotations

from stackd.config.models import Config

_EPS = 1e-6


def device_ceiling(cfg: Config, device: str) -> float:
    d = cfg.devices[device]
    if d.vram_budget_gib is not None:
        return d.vram_budget_gib
    return cfg.pools[d.vram_pool].total_gib


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
        ceiling = d.vram_budget_gib if d.vram_budget_gib is not None else p.total_gib
        demand = dev_vram.get(d.name, 0.0)
        counted = min(demand + d.vram_headroom_gib, ceiling)
        bd[f"vram:{d.name} (≤{round(ceiling)})"] = round(counted, 2)
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
