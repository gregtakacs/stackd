"""Measured two-axis footprint curves (design note D5 / P0).

A stack's VRAM and RAM are each modelled as a line over context length, fitted
from >=2 measured points. The validator and reconciler prefer a curve when one
exists and fall back to the stack's declared `budget:` otherwise, so the catalog
is purely additive — no curve, no behaviour change.

Files: `catalog/<key>.json`, one per engine config-signature:

    {
      "key": "llamacpp-cuda|cuda0|Qwen3.8-27B-UD-Q4_K_XL|p4",
      "model": "...", "engine": "llamacpp-cuda", "device": "cuda0",
      "source": "measured" | "estimate",
      "measured_at": "2026-09-03T..." | null,
      "points": { "vram": [[131072, 33.0], ...], "ram": [[131072, 2.0], ...] },
      "notes": "..."
    }
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from stackd.config.models import Config


def primary_device(cfg: Config, model_name: str) -> str:
    """The device a model's curve is keyed on — its first placement preference,
    else the first device its engine backend supports."""
    m = cfg.models[model_name]
    if m.placement.devices:
        return m.placement.devices[0]
    from stackd.engines.registry import TEMPLATES
    backs = TEMPLATES[m.engine.template].backends
    for d in cfg.devices.values():
        if d.backend.value in backs:
            return d.name
    return next(iter(cfg.devices))


def curve_key(cfg: Config, model_name: str, device: str | None = None) -> str:
    e = cfg.models[model_name].engine
    dev = device or primary_device(cfg, model_name)
    return f"{e.template}|{dev}|{e.model}|p{e.params.get('parallel')}"


def key_slug(key: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in key)


def _fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """(slope, intercept) least-squares. 0/1 point -> flat line."""
    if not points:
        return 0.0, 0.0
    if len(points) == 1:
        return 0.0, points[0][1]
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    return slope, (sy - slope * sx) / n


@dataclass
class Axis:
    points: list[tuple[float, float]]
    slope: float
    intercept: float

    @classmethod
    def from_points(cls, points) -> "Axis":
        pts = [(float(x), float(y)) for x, y in points]
        s, i = _fit(pts)
        return cls(pts, s, i)

    def at(self, x: float | None) -> float:
        return max(0.0, self.intercept + self.slope * (x or 0.0))

    def max_x(self, budget: float) -> float | None:
        """Largest x with at(x) <= budget. None if this axis never binds (flat)."""
        if self.slope <= 1e-9:
            return None
        return max(0.0, (budget - self.intercept) / self.slope)


@dataclass
class FootprintCurve:
    key: str
    model: str
    engine: str
    device: str
    source: str
    vram: Axis
    ram: Axis
    measured_at: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "FootprintCurve":
        pts = d.get("points", {})
        return cls(
            key=d["key"],
            model=d.get("model", ""),
            engine=d.get("engine", ""),
            device=d.get("device", ""),
            source=d.get("source", "estimate"),
            vram=Axis.from_points(pts.get("vram", [])),
            ram=Axis.from_points(pts.get("ram", [])),
            measured_at=d.get("measured_at"),
            notes=d.get("notes", ""),
        )

    def estimate(self, ctx: float | None) -> tuple[float, float]:
        return round(self.vram.at(ctx), 2), round(self.ram.at(ctx), 2)

    def max_ctx(self, vram_budget: float, ram_budget: float | None = None) -> float | None:
        cands = [c for c in (
            self.vram.max_x(vram_budget),
            self.ram.max_x(ram_budget) if ram_budget is not None else None,
        ) if c is not None]
        return min(cands) if cands else None


@dataclass
class Catalog:
    curves: dict[str, FootprintCurve] = field(default_factory=dict)
    path: pathlib.Path | None = None

    @classmethod
    def load(cls, directory: str | pathlib.Path,
             overlay: str | pathlib.Path | None = None) -> "Catalog":
        d = pathlib.Path(directory)
        cat = cls(path=d)
        for src in (d, pathlib.Path(overlay) if overlay else None):
            if src is None or not src.is_dir():
                continue
            for f in sorted(src.glob("*.json")):
                raw = json.loads(f.read_text())
                cat.curves[raw["key"]] = FootprintCurve.from_dict(raw)   # overlay wins by key
        return cat

    def for_model(self, cfg: Config, model_name: str, device: str | None = None) -> FootprintCurve | None:
        return self.curves.get(curve_key(cfg, model_name, device))

    # back-compat alias
    for_stack = for_model

    def estimate(self, cfg: Config, model_name: str, device: str, ctx: float | None):
        """(vram_gib, ram_gib, source) or None."""
        cur = self.for_model(cfg, model_name, device)
        if cur is None:
            return None
        v, r = cur.estimate(ctx)
        return v, r, cur.source

    def write_curve(
        self, key: str, *, model: str, engine: str, device: str,
        vram_points, ram_points, source: str = "measured",
        measured_at: str | None = None, notes: str = "",
    ) -> pathlib.Path:
        assert self.path is not None
        self.path.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key, "model": model, "engine": engine, "device": device,
            "source": source, "measured_at": measured_at,
            "points": {
                "vram": [[float(x), float(y)] for x, y in vram_points],
                "ram": [[float(x), float(y)] for x, y in ram_points],
            },
            "notes": notes,
        }
        out = self.path / f"{key_slug(key)}.json"
        out.write_text(json.dumps(payload, indent=2))
        self.curves[key] = FootprintCurve.from_dict(payload)
        return out
