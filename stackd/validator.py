from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stackd.config.models import Config
from stackd.fit import check
from stackd.solver import solve

if TYPE_CHECKING:
    from stackd.catalog import Catalog

THIN_ABS_GIB = 5.0
THIN_FRAC = 0.05


@dataclass
class PoolBalance:
    pool: str
    limit_gib: float
    used_gib: float
    breakdown: dict[str, float]

    @property
    def headroom_gib(self) -> float:
        return round(self.limit_gib - self.used_gib, 2)

    @property
    def ok(self) -> bool:
        return self.used_gib <= self.limit_gib + 1e-6


@dataclass
class DeviceBalance:
    device: str
    budget_gib: float
    used_gib: float
    models: list[str]

    @property
    def headroom_gib(self) -> float:
        return round(self.budget_gib - self.used_gib, 2)

    @property
    def ok(self) -> bool:
        return self.used_gib <= self.budget_gib + 1e-6


@dataclass
class FitReport:
    profile: str
    resident: list[str]                       # models the solver placed
    pools: list[PoolBalance]
    devices: list[DeviceBalance]
    flags: list[str] = field(default_factory=list)
    placement: dict[str, str] = field(default_factory=dict)   # model -> device
    sources: dict[str, str] = field(default_factory=dict)     # model -> measured|estimate|declared
    deltas: dict[str, tuple[float, float]] = field(default_factory=dict)  # model -> (Δvram, Δram) vs declared
    unmeasured: list[str] = field(default_factory=list)       # placed but source != "measured"
    unplaced: list[str] = field(default_factory=list)         # in the profile but fit nowhere

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.pools) and all(d.ok for d in self.devices)


def validate_profile(cfg: Config, profile: str, catalog: "Catalog | None" = None) -> FitReport:
    if profile not in cfg.profiles:
        raise KeyError(profile)
    pl = solve(cfg, profile, catalog)

    dev_vram: dict[str, float] = {d: 0.0 for d in cfg.devices}
    dev_ram: dict[str, float] = {d: 0.0 for d in cfg.devices}
    dev_models: dict[str, list[str]] = {d: [] for d in cfg.devices}
    for p in pl.placed.values():
        dev_vram[p.device] += p.vram_gib
        dev_ram[p.device] += p.ram_gib
        dev_models[p.device].append(p.model)

    ok, pools_raw, devs_raw = check(cfg, dev_vram, dev_ram)
    pools = [PoolBalance(pn, lim, used, bd) for pn, (used, lim, bd) in pools_raw.items()]
    devices = [DeviceBalance(dn, cel, used, dev_models[dn]) for dn, (used, cel) in devs_raw.items()]

    flags: list[str] = []
    for p in pools:
        if p.ok and (p.headroom_gib < THIN_ABS_GIB or p.headroom_gib < THIN_FRAC * p.limit_gib):
            flags.append(f"thin margin on pool {p.pool}: {p.headroom_gib} GiB free")
    for d in devices:
        if d.ok and d.models and d.headroom_gib < THIN_ABS_GIB:
            flags.append(f"thin margin on device {d.device}: {d.headroom_gib} GiB free")

    tmpl_by_dev: dict[str, set[str]] = {}
    for p in pl.placed.values():
        tmpl_by_dev.setdefault(p.device, set()).add(p.template)
    for dev, ts in tmpl_by_dev.items():
        if "comfyui" in ts and any(t != "comfyui" for t in ts):
            flags.append(
                f"contention on {dev}: image engine co-resident with an LLM engine "
                f"(peak != sum of budgets)"
            )

    if pl.unplaced:
        flags.append(f"won't fit, unavailable in this profile: {', '.join(pl.unplaced)}")

    unmeasured = (
        [p.model for p in pl.placed.values() if p.source != "measured"]
        if catalog is not None else []
    )
    if unmeasured:
        flags.append(f"unmeasured footprint (declared/estimate): {', '.join(unmeasured)} "
                     f"— run `stackctl bench`")

    deltas: dict[str, tuple[float, float]] = {}
    if catalog is not None:
        for p in pl.placed.values():
            decl = cfg.models[p.model].budget
            if p.source != "declared":
                deltas[p.model] = (round(p.vram_gib - decl.vram_gib, 2),
                                   round(p.ram_gib - decl.ram_gib, 2))

    return FitReport(
        profile, list(pl.placed), pools, devices, flags,
        placement={p.model: p.device for p in pl.placed.values()},
        sources={p.model: p.source for p in pl.placed.values()},
        deltas=deltas, unmeasured=unmeasured, unplaced=list(pl.unplaced),
    )
