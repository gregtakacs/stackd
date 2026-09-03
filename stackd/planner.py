"""Delta between two profiles' resolved placements — what the reconciler acts on.
A model whose placement (device + engine signature) is unchanged is kept."""

from __future__ import annotations

from dataclasses import dataclass, field

from stackd.config.models import Config
from stackd.solver import model_identity, solve  # noqa: F401  (re-export for callers)


@dataclass
class ReconcilePlan:
    frm: str
    to: str
    keep: list[str] = field(default_factory=list)
    reload: list[str] = field(default_factory=list)
    spawn: list[str] = field(default_factory=list)
    teardown: list[str] = field(default_factory=list)


def plan_transition(cfg: Config, frm: str, to: str, catalog=None) -> ReconcilePlan:
    for n in (frm, to):
        if n not in cfg.profiles:
            raise KeyError(n)
    before = {p.model: p.identity for p in solve(cfg, frm, catalog).placed.values()}
    after = {p.model: p.identity for p in solve(cfg, to, catalog).placed.values()}

    plan = ReconcilePlan(frm, to)
    for n, ident in after.items():
        if n not in before:
            plan.spawn.append(n)
        elif before[n] == ident:
            plan.keep.append(n)
        else:
            plan.reload.append(n)
    for n in before:
        if n not in after:
            plan.teardown.append(n)
    return plan
