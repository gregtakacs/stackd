from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from stackd.config.models import Config
from stackd.fit import budget_facts, cap_str, check
from stackd.solver import solve

if TYPE_CHECKING:
    from stackd.catalog import Catalog

# Soft "thin margin" advisory flags (NOT a fit failure — `check()` still hard-fails
# on real overflow). Lowered from 5.0 / 0.05 (2026-09-06): the sglang-pennyroyal
# whole-GPU profiles deliberately run the card near-full (--mem-fraction-static
# 0.981 leaves ~1.8 GiB on a 96 GiB card, ~1.9%), which is fine and intentional.
# 1.5 GiB / 1.5% still flags a genuinely about-to-not-fit placement.
THIN_ABS_GIB = 1.5
THIN_FRAC = 0.015


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
    budget_gib: float               # CONFIGURED ceiling (what the operator asked for)
    used_gib: float
    models: list[str]
    # What `fit` actually booked against: min(configured, measured GTT window).
    # `ok` / `headroom` are judged on THIS, which is the whole point — a
    # IGPU_VRAM_BUDGET_GIB the hardware can't honour must stop being a pass.
    # When the window can't be measured (no amdgpu, no /sys, CI,
    # STACKD_DISABLE_GPU_AUTODETECT) the effective ceiling equals the configured
    # one, so nothing regresses on boxes that can't answer the question.
    budget_effective_gib: float | None = None
    gtt_window_gib: float | None = None

    @property
    def ceiling_gib(self) -> float:
        return self.budget_gib if self.budget_effective_gib is None \
            else self.budget_effective_gib

    @property
    def headroom_gib(self) -> float:
        return round(self.ceiling_gib - self.used_gib, 2)

    @property
    def ok(self) -> bool:
        return self.used_gib <= self.ceiling_gib + 1e-6

    @property
    def clamped(self) -> bool:
        """True when the hardware window binds below the configured budget —
        the case that must be *named*, never silently shrunk."""
        return self.budget_effective_gib is not None \
            and self.budget_effective_gib < self.budget_gib - 1e-6


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
    model_vram: dict[str, float] = field(default_factory=dict)  # model -> planned VRAM GiB on its device
    model_ram: dict[str, float] = field(default_factory=dict)   # model -> planned host-RAM spill GiB

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
        dev_models[p.device].append(p.name)

    ok, pools_raw, devs_raw = check(cfg, dev_vram, dev_ram)
    pools = [PoolBalance(pn, lim, used, bd) for pn, (used, lim, bd) in pools_raw.items()]
    # devs_raw carries the EFFECTIVE ceiling (that's what fit checked); the report
    # keeps the configured one alongside it so a clamped cap can be named rather
    # than silently shrunk.
    devices = []
    for dn, (used, eff) in devs_raw.items():
        facts = budget_facts(cfg, dn)
        devices.append(DeviceBalance(dn, facts["configured"], used, dev_models[dn],
                                     budget_effective_gib=facts["effective"],
                                     gtt_window_gib=facts["window"]))

    flags: list[str] = []
    for p in pools:
        if p.ok and (p.headroom_gib < THIN_ABS_GIB or p.headroom_gib < THIN_FRAC * p.limit_gib):
            flags.append(f"thin margin on pool {p.pool}: {p.headroom_gib} GiB free")
    for d in devices:
        if d.ok and d.models and d.headroom_gib < THIN_ABS_GIB:
            flags.append(f"thin margin on device {d.device}: {d.headroom_gib} GiB free")
        # A budget the hardware can't honour is a config bug, not a fit failure:
        # name it once, loudly, and say which number the math used. Boxes where
        # the window can't be measured (no amdgpu / no /sys / CI) get nothing —
        # this must not read as a warning to the `IGPU_VRAM_BUDGET_GIB=1` no-iGPU
        # sentinel in deploy/README.
        if d.clamped:
            flags.append(
                f"igpu budget clamped to hardware on {d.device}: configured "
                f"{cap_str(d.budget_gib)} GiB > driver GTT window "
                f"{cap_str(d.gtt_window_gib)} GiB — the fit math caps at "
                f"{cap_str(d.budget_effective_gib)} GiB")

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
        [p.name for p in pl.placed.values() if p.source != "measured"]
        if catalog is not None else []
    )
    if unmeasured:
        flags.append(f"unmeasured footprint (declared/estimate): {', '.join(unmeasured)} "
                     f"— run `stackctl bench`")

    deltas: dict[str, tuple[float, float]] = {}
    if catalog is not None:
        for p in pl.placed.values():
            decl = cfg.models[p.name].budget
            if p.source != "declared":
                deltas[p.name] = (round(p.vram_gib - decl.vram_gib, 2),
                                   round(p.ram_gib - decl.ram_gib, 2))

    return FitReport(
        profile, list(pl.placed), pools, devices, flags,
        placement={p.name: p.device for p in pl.placed.values()},
        sources={p.name: p.source for p in pl.placed.values()},
        deltas=deltas, unmeasured=unmeasured, unplaced=list(pl.unplaced),
        model_vram={p.name: p.vram_gib for p in pl.placed.values()},
        model_ram={p.name: p.ram_gib for p in pl.placed.values() if p.ram_gib},
    )
