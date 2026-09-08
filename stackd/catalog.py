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

# A passively-measured load_s/teardown_s beyond this multiple of the established
# EMA is treated as a one-off (cold JIT compile, cache-cold disk) and not folded.
_OUTLIER_FACTOR = 2.5


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
    # measured swap-phase durations (seconds), EMA-smoothed. Filled both by
    # `stackctl bench` and passively by the daemon on every real switch:
    #   {"load_s": <ema>, "teardown_s": <ema>, "last_load_s": .., "last_teardown_s": ..,
    #    "n": <count>, "measured_at": <iso>}
    timings: dict = field(default_factory=dict)

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
            timings=dict(d.get("timings", {})),
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
    # the durable / bind-mounted dir (config overlay) — where the daemon writes
    # passively-measured `timings` so they survive an image rebuild. Falls back
    # to `path` when no overlay is in play.
    overlay_path: pathlib.Path | None = None

    @classmethod
    def load(cls, directory: str | pathlib.Path,
             overlay: str | pathlib.Path | None = None) -> "Catalog":
        d = pathlib.Path(directory)
        cat = cls(path=d, overlay_path=pathlib.Path(overlay) if overlay else None)
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
        measured_at: str | None = None, notes: str = "", timings: dict | None = None,
    ) -> pathlib.Path:
        assert self.path is not None
        self.path.mkdir(parents=True, exist_ok=True)
        out = self.path / f"{key_slug(key)}.json"
        # carry an existing timings block forward unless a fresh one is supplied
        prev_timings = {}
        if timings is None and out.exists():
            try:
                prev_timings = json.loads(out.read_text()).get("timings", {}) or {}
            except Exception:  # noqa: BLE001
                prev_timings = {}
        payload = {
            "key": key, "model": model, "engine": engine, "device": device,
            "source": source, "measured_at": measured_at,
            "points": {
                "vram": [[float(x), float(y)] for x, y in vram_points],
                "ram": [[float(x), float(y)] for x, y in ram_points],
            },
            "notes": notes,
            "timings": timings if timings is not None else prev_timings,
        }
        out.write_text(json.dumps(payload, indent=2))
        self.curves[key] = FootprintCurve.from_dict(payload)
        return out

    def update_timings(
        self, key: str, *, load_s: float | None = None, teardown_s: float | None = None,
        model: str = "", engine: str = "", device: str = "", alpha: float = 0.4,
    ) -> dict:
        """Fold one measured phase duration into the `timings` block of catalog
        entry `key`, EMA-smoothed, and persist it. Writes to the overlay dir so
        the value survives an image rebuild; read-merges so `points` / `notes` /
        `source` are preserved. Best-effort — returns the new timings dict (or {}
        on any failure) and never raises."""
        import datetime
        try:
            samples = {k: v for k, v in (("load_s", load_s), ("teardown_s", teardown_s))
                       if v is not None and 0 < v <= 3600}
            if not samples:
                return {}
            target = self.overlay_path or self.path
            if target is None:
                return {}
            target.mkdir(parents=True, exist_ok=True)
            out = target / f"{key_slug(key)}.json"
            base = {}
            for cand in (out, (self.path / f"{key_slug(key)}.json") if self.path else None):
                if cand and cand.exists():
                    try:
                        base = json.loads(cand.read_text())
                        break
                    except Exception:  # noqa: BLE001
                        pass
            cur = self.curves.get(key)
            base.setdefault("key", key)
            base.setdefault("model", model or (cur.model if cur else ""))
            base.setdefault("engine", engine or (cur.engine if cur else ""))
            base.setdefault("device", device or (cur.device if cur else ""))
            base.setdefault("source", "measured")
            base.setdefault("points", {"vram": [], "ram": []})
            base.setdefault("notes", "")
            t = dict(base.get("timings") or {})
            n_prev = int(t.get("n", 0))
            folded = 0
            for k, v in samples.items():
                t["last_" + k] = round(v, 1)
                # Reject a gross outlier once there's an established EMA: a cold
                # FlashInfer/JIT compile or a cache-cold disk read can be 3-5x a
                # warm load and would otherwise poison the estimate for many
                # subsequent switches (EMA alpha 0.4). Kept visible as last_<k>.
                if k in t and n_prev >= 3 and v > _OUTLIER_FACTOR * t[k]:
                    continue
                t[k] = round(v if k not in t else alpha * v + (1 - alpha) * t[k], 1)
                folded += 1
            if folded:
                t["n"] = n_prev + 1
            t["measured_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            base["timings"] = t
            tmp = out.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(base, indent=2))
            tmp.replace(out)
            if cur is not None:
                cur.timings = t
            else:
                self.curves[key] = FootprintCurve.from_dict(base)
            return t
        except Exception:  # noqa: BLE001 — telemetry must never break a converge
            return {}
