"""Placement solver. A profile is a priority-ordered list of model names; this
resolves each to a device — the first in its preference list that still fits the
pool + device budgets. Models that fit nowhere are reported `unplaced`.

The output is a `Placement`; the reconciler diffs it against what's running by
`identity`, so a model whose resolved placement is unchanged is never touched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from stackd.config.models import Config
from stackd.engines.registry import TEMPLATES
from stackd.fit import check


def model_identity(cfg: Config, model_name: str, device: str) -> list:
    """JSON-native signature — same identity ⇒ same running container, keep it.

    Includes the FULLY-RESOLVED `container:` spec (image, env, mounts, labels,
    devices, shm, build) so an ${VAR}/.env edit that lands in the container is
    seen by converge → the engine is recreated on `stackctl reload`, no
    `docker rm -f` needed."""
    e = cfg.models[model_name].engine
    p = e.params
    return [
        model_name, e.template, device, e.model,
        p.get("ctx"), p.get("parallel"), p.get("kv_dtype"),
        list(p.get("extra_args", []) or []),
        p.get("served_model_name"), p.get("active_model"), p.get("gpu_memory_utilization"),
        e.container.name, e.container.adopt,
        asdict(e.container),
    ]


@dataclass
class Placed:
    model: str
    device: str
    template: str
    port: int | None
    vram_gib: float
    ram_gib: float
    source: str            # measured | estimate | declared
    identity: list


@dataclass
class Placement:
    profile: str
    placed: dict[str, Placed] = field(default_factory=dict)
    unplaced: list[str] = field(default_factory=list)

    @property
    def models(self) -> list[str]:
        return list(self.placed)


def candidate_devices(cfg: Config, model_name: str) -> list[str]:
    m = cfg.models[model_name]
    if m.placement.devices:
        return list(m.placement.devices)
    backs = TEMPLATES[m.engine.template].backends
    return [d.name for d in cfg.devices.values() if d.backend.value in backs]


def _budget(cfg: Config, model_name: str, device: str, catalog):
    m = cfg.models[model_name]
    if catalog is not None:
        est = catalog.estimate(cfg, model_name, device, m.engine.params.get("ctx"))
        if est is not None:
            return est
    return (m.budget.vram_gib, m.budget.ram_gib, "declared")


def solve(cfg: Config, profile: str, catalog=None, *, port_from: int = 11500) -> Placement:
    pr = cfg.profiles[profile]
    dev_vram: dict[str, float] = {d: 0.0 for d in cfg.devices}
    dev_ram: dict[str, float] = {d: 0.0 for d in cfg.devices}
    out = Placement(profile)
    next_port = port_from

    for name in pr.models:                       # list order == priority
        m = cfg.models[name]
        picked = None
        for dev in candidate_devices(cfg, name):
            v, r, src = _budget(cfg, name, dev, catalog)
            trial_v = dict(dev_vram); trial_v[dev] += v
            trial_r = dict(dev_ram); trial_r[dev] += r
            ok, _, _ = check(cfg, trial_v, trial_r)
            if ok:
                picked = (dev, v, r, src)
                break
        if picked is None:
            out.unplaced.append(name)
            continue
        dev, v, r, src = picked
        dev_vram[dev] += v
        dev_ram[dev] += r
        port = m.engine.container.port
        if m.engine.template.startswith("llamacpp") and port is None:
            port = next_port
            next_port += 1
        out.placed[name] = Placed(name, dev, m.engine.template, port, round(v, 2),
                                  round(r, 2), src, model_identity(cfg, name, dev))
    return out


def headroom(cfg: Config, profile: str, catalog=None, *, reserve_gib: float = 0.0) -> dict[str, float]:
    """Free VRAM GiB per device once `profile`'s LLM models are placed — the budget
    the elastic media tiers get. `reserve_gib` (a tier's `margin_gib`) is held back
    on every device. Never negative. This is a *declared-budget* estimate: it can't
    see a vLLM engine that pre-claims the whole card, so keep footprints honest."""
    from stackd.fit import device_ceiling

    pl = solve(cfg, profile, catalog)
    used: dict[str, float] = {d: 0.0 for d in cfg.devices}
    for p in pl.placed.values():
        used[p.device] = used.get(p.device, 0.0) + p.vram_gib
    return {
        d: max(0.0, device_ceiling(cfg, d) - used.get(d, 0.0) - reserve_gib)
        for d in cfg.devices
    }
