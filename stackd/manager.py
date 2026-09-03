"""High-level operations behind the `stackctl` verbs. Loads config + state, acts,
persists. Priority gating and stand-in routing live here; mechanical start/stop
lives in the reconciler; placement lives in the solver."""

from __future__ import annotations

import fnmatch
import pathlib
import time
from dataclasses import dataclass, field

from stackd.config.loader import load_config
from stackd.config.models import Config
from stackd.engines.base import EngineState
from stackd.engines.registry import adapter_for
from stackd.reconciler import Reconciler
from stackd.runner import Runner, default_runner
from stackd.solver import solve
from stackd.state import RuntimeState
from stackd.util import parse_duration
from stackd.validator import validate_profile


class ManagerError(RuntimeError):
    pass


class Outranked(ManagerError):
    pass


@dataclass
class RouteResult:
    status: str  # ok | warming | outranked | unknown
    model: str
    endpoint: str | None = None
    profile: str | None = None
    stack: str | None = None
    served_model_name: str | None = None   # set -> the HTTP front rewrites body["model"]
    preset: dict = field(default_factory=dict)
    route_kind: str = "native"             # native | standin
    note: str = ""


def _serves(cfg: Config, model_name: str, api_name: str) -> tuple[bool, bool]:
    """(matches, exact) — does this model serve `api_name`, and is it an exact entry."""
    exact = matches = False
    for se in cfg.models[model_name].serves:
        if se.api_name == api_name:
            return True, True
        if fnmatch.fnmatch(api_name, se.api_name):
            matches = True
    return matches, exact


def _profiles_serving(cfg: Config, api_name: str) -> list[str]:
    hits = []
    for pr in cfg.profiles.values():
        if any(_serves(cfg, mn, api_name)[0] for mn in pr.models):
            hits.append(pr.profile)
    return sorted(hits, key=lambda n: -cfg.profiles[n].priority)


class Manager:
    def __init__(self, config_dir, state_path, runner: Runner | None = None, *,
                 models_dir: str = "/models", min_residency_s: float = 120.0,
                 switch_cooldown_s: float = 30.0) -> None:
        self.config_dir = pathlib.Path(config_dir)
        self.models_dir = models_dir
        self.cfg: Config = load_config(config_dir)
        self.state_path = pathlib.Path(state_path)
        self.runner = runner or default_runner()
        self.min_residency_s = min_residency_s
        self.switch_cooldown_s = switch_cooldown_s
        self._default = next(p.profile for p in self.cfg.profiles.values() if p.default)
        self.state = RuntimeState.load(self.state_path, self._default)
        self.catalog = self._load_catalog()
        self.rec = Reconciler(self.cfg, self.runner, models_dir=models_dir, catalog=self.catalog)
        # FakeRunner (tests/demos) — keep the cuda drain logic exercised but drop the
        # real sleeps between polls.
        if hasattr(self.runner, "gpu_free_values"):
            self.rec.cuda_drain_poll_s = 0.0

    def _load_catalog(self):
        import os
        cdir = self.config_dir / "catalog"
        ov = os.environ.get("STACKD_CONFIG_OVERLAY")
        ovc = (pathlib.Path(ov) / "catalog") if ov else None
        if cdir.is_dir() or (ovc and ovc.is_dir()):
            from stackd.catalog import Catalog
            return Catalog.load(cdir, overlay=ovc)
        return None

    def reload_config(self) -> list[str]:
        """Re-read config/*.yaml (+ the bind-mounted .env) into a fresh, VALIDATED
        Config and swap it in. Raises ConfigError on a bad edit — the live config
        is untouched. Does NOT converge; the caller re-runs `use(active_profile)`.
        Returns a human diff of what changed."""
        new = load_config(self.config_dir)          # validates; raises on error
        diff: list[str] = []
        for label, old_map, new_map in (
            ("model", self.cfg.models, new.models),
            ("profile", self.cfg.profiles, new.profiles),
            ("pool", self.cfg.pools, new.pools),
            ("device", self.cfg.devices, new.devices),
        ):
            for k in sorted(set(old_map) | set(new_map)):
                if k not in new_map:
                    diff.append(f"{label} {k} removed")
                elif k not in old_map:
                    diff.append(f"{label} {k} added")
                elif old_map[k] != new_map[k]:
                    diff.append(f"{label} {k} changed")
        if self.cfg.runtime != new.runtime:
            diff.append("runtime changed")
        self.cfg = new
        self.rec.cfg = new
        self.catalog = self._load_catalog()
        self.rec.catalog = self.catalog
        self._default = next(p.profile for p in new.profiles.values() if p.default)
        return diff or ["no changes"]

    def _save(self) -> None:
        self.state.save(self.state_path)

    # -------------------------------------------------------------- transitions ---
    def _enter(self, name: str, now: float, *, switching: bool) -> None:
        self.state.active_profile = name
        if switching:
            self.state.pinned = False
            self.state.entered_at = now
            self.state.last_switch_at = now
        self.state.last_served_at = now
        self.rec.converge(self.state, name, now=now)

    def use(self, name: str, *, manual: bool = True, now: float | None = None) -> list:
        now = time.time() if now is None else now
        if name not in self.cfg.profiles:
            raise ManagerError(f"unknown profile {name!r}")
        active = self.cfg.profiles[self.state.active_profile]
        target = self.cfg.profiles[name]
        if not manual:
            if target.priority <= active.priority:
                raise Outranked(
                    f"{name} (pri {target.priority}) does not outrank active "
                    f"{active.profile} (pri {active.priority})")
            if self.state.pinned:
                raise Outranked(f"active profile {active.profile} is pinned")
            if (self.state.last_switch_at is not None
                    and now - self.state.last_switch_at < self.switch_cooldown_s):
                raise Outranked("switch cooldown in effect")
        self._enter(name, now, switching=(name != self.state.active_profile))
        self._save()
        return list(self.rec.events)

    def evict(self, *, now: float | None = None) -> list:
        now = time.time() if now is None else now
        if self.state.active_profile == self._default:
            self._save()
            return []
        self._enter(self._default, now, switching=True)
        self._save()
        return list(self.rec.events)

    def pin(self) -> None:
        self.state.pinned = True
        self._save()

    def unpin(self) -> None:
        self.state.pinned = False
        self._save()

    # -------------------------------------------------------------------- route ---
    def _preset_for(self, api_name: str) -> dict:
        """The preset wherever this exact name is defined (so a glob-matched
        request still lands in the right reasoning mode)."""
        for m in self.cfg.models.values():
            for se in m.serves:
                if se.api_name == api_name:
                    return dict(se.preset)
        return {}

    def _served_name(self, model_name: str) -> str | None:
        e = self.cfg.models[model_name].engine
        return e.params.get("served_model_name") if e.template == "vllm-cuda" else None

    def route(self, api_name: str, *, now: float | None = None) -> RouteResult:
        now = time.time() if now is None else now
        pr = self.cfg.profiles[self.state.active_profile]
        running = set(self.state.stacks)

        for mn in pr.models:                      # priority order
            matches, exact = _serves(self.cfg, mn, api_name)
            if matches and mn in running:
                rt = self.state.stacks[mn]
                self.state.last_served_at = now
                self._save()
                status = "ok" if rt.state == EngineState.ready else "warming"
                return RouteResult(
                    status, api_name, rt.endpoint or None, self.state.active_profile,
                    stack=mn, served_model_name=self._served_name(mn),
                    preset=self._preset_for(api_name),
                    route_kind="native" if exact else "standin",
                    note="" if status == "ok" else f"{mn} is {rt.state.value}",
                )

        owners = _profiles_serving(self.cfg, api_name)
        if not owners:
            return RouteResult("unknown", api_name, note="no model serves this name")
        top = owners[0]
        if self.cfg.profiles[top].priority > pr.priority:
            try:
                self.use(top, manual=False, now=now)
            except Outranked as e:
                return RouteResult("warming", api_name, profile=top, note=str(e))
            return RouteResult("warming", api_name, profile=top, note=f"entered {top}; warming")
        return RouteResult("outranked", api_name, profile=top,
                           note=f"{top} is not higher priority than active {pr.profile}")

    # --------------------------------------------------------------------- tick ---
    def tick(self, *, now: float | None = None) -> list:
        now = time.time() if now is None else now
        events = self.rec.tick(self.state, now=now)
        pr = self.cfg.profiles[self.state.active_profile]
        if (pr.idle_evict and not pr.default and not self.state.pinned
                and self.state.last_served_at is not None
                and now - self.state.last_served_at > parse_duration(pr.idle_evict)
                and now - (self.state.entered_at or now) > self.min_residency_s):
            self.evict(now=now)
            events.append(_Evt(self.state.active_profile, "idle-evict",
                               f"no traffic for {pr.idle_evict}"))
        self._save()
        return events

    # ----------------------------------------------------- image / video caps ---
    def _codevice_warming(self, model: str) -> bool:
        dev = self.state.stacks[model].device
        return any(o != model and ort.device == dev and ort.state == EngineState.warming
                   for o, ort in self.state.stacks.items())

    def capabilities(self) -> dict:
        engines: list[dict] = []
        by_kind: dict[str, dict] = {}
        for name, rt in self.state.stacks.items():
            m = self.cfg.models.get(name)
            if not m or m.engine.template != "comfyui":
                continue
            p = m.engine.params
            codev = self._codevice_warming(name)
            entry = {
                "stack": name, "kind": p.get("kind", "image"),
                "active_model": p.get("active_model"),
                "workflow_templates": list(p.get("workflow_templates") or []),
                "state": rt.state.value,
                "serveable": rt.state == EngineState.ready and not codev,
                "co_device_warming": codev, "endpoint": rt.endpoint,
                "device": rt.device, "owner_profile": rt.owner_profile,
            }
            engines.append(entry)
            k = entry["kind"]
            if k not in by_kind or (entry["serveable"] and not by_kind[k]["serveable"]):
                by_kind[k] = entry
        return {"active_profile": self.state.active_profile, "engines": engines,
                "image": by_kind.get("image"), "video": by_kind.get("video")}

    def engines(self) -> list[dict]:
        out = []
        for name, rt in self.state.stacks.items():
            m = self.cfg.models.get(name)
            if not m:
                continue
            d = adapter_for(m, self.cfg.devices[rt.device or next(iter(self.cfg.devices))]).describe(
                state=rt.state, port=rt.port)
            out.append({"stack": name, "owner_profile": rt.owner_profile, "state": d.state.value,
                        "active_model": d.active_model, "served": d.served,
                        "endpoint": rt.endpoint or d.endpoint, "extra": d.extra})
        return out

    def _profile_with_comfyui(self, kind: str | None = None) -> str | None:
        for pr in sorted(self.cfg.profiles.values(), key=lambda p: -p.priority):
            for mn in pr.models:
                m = self.cfg.models[mn]
                if m.engine.template == "comfyui" and (
                        kind is None or m.engine.params.get("kind", "image") == kind):
                    return pr.profile
        return None

    def comfyui_target(self, kind: str = "image", *, now: float | None = None):
        now = time.time() if now is None else now
        caps = self.capabilities()
        entry = caps.get(kind) or caps.get("image")
        if entry and entry["serveable"] and entry["endpoint"]:
            self.state.last_served_at = now
            self._save()
            return entry["endpoint"], ""
        if entry and not entry["serveable"]:
            why = "co-device stack warming" if entry["co_device_warming"] else entry["state"]
            return None, f"image engine {entry['stack']} not serveable ({why})"
        owner = self._profile_with_comfyui(kind)
        return None, (f"no {kind} engine resident — activate profile {owner!r}"
                      if owner else f"no profile provides a {kind} engine")

    # ------------------------------------------------------------------ catalog ---
    def models_catalog(self) -> list[dict]:
        pr_active = self.cfg.profiles[self.state.active_profile]
        running = set(self.state.stacks)
        seen: dict[str, dict] = {}
        for pr in sorted(self.cfg.profiles.values(), key=lambda p: -p.priority):
            for mn in pr.models:
                m = self.cfg.models[mn]
                ctx = m.engine.params.get("ctx") or m.engine.params.get("max_model_len")
                for se in m.serves:
                    if se.api_name in seen or "*" in se.api_name:
                        continue
                    ready = any(_serves(self.cfg, x, se.api_name)[0] and x in running
                                for x in pr_active.models)
                    seen[se.api_name] = {"id": se.api_name, "context_length": ctx,
                                         "owner_profile": pr.profile, "ready": ready}
        return list(seen.values())

    # ------------------------------------------------------------------- status ---
    def status(self) -> dict:
        report = validate_profile(self.cfg, self.state.active_profile, self.catalog)
        running = set(self.state.stacks)
        return {
            "active_profile": self.state.active_profile,
            "pinned": self.state.pinned,
            "placement": report.placement,
            "unplaced": report.unplaced,
            "stacks": {
                n: {"state": rt.state.value, "device": rt.device, "kind": rt.kind,
                    "owner": rt.owner_profile, "port": rt.port, "container": rt.container,
                    "endpoint": rt.endpoint, "restarts": rt.restarts}
                for n, rt in self.state.stacks.items()
            },
            "missing": [n for n in report.resident if n not in running],
            "pools": [{"pool": p.pool, "used": p.used_gib, "limit": p.limit_gib, "ok": p.ok}
                      for p in report.pools],
            "flags": report.flags,
        }


@dataclass
class _Evt:
    stack: str
    action: str
    detail: str = ""
