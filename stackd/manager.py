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
    artifact: str | None = None            # the checkpoint on disk (engine.model) — for the ledger
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


def _translate_preset(raw: dict, template: str) -> dict:
    """Expand stackd's engine-agnostic ``reasoning:`` preset key into the request
    dialect of the engine that will actually serve the call. Everything else
    passes through untouched, so raw ``reasoning_effort`` / ``chat_template_kwargs``
    presets still work.

      reasoning: off                        -> llamacpp: reasoning_effort=none, reasoning_budget=0
                                               vllm/sglang: chat_template_kwargs.enable_thinking=false
      reasoning: low | {effort: low}        -> all:      reasoning_effort=low
      reasoning: {effort: low, budget: N}   -> llamacpp: + reasoning_budget=N   (vllm/sglang have no per-request budget)

    vLLM (Qwen3 reasoning parser) and the sglang-pennyroyal fork (also launched
    with --reasoning-parser qwen3 + --default-chat-template-kwargs) share the same
    Qwen3 request dialect, so they're translated identically.
    """
    if "reasoning" not in raw:
        return dict(raw)
    out = {k: v for k, v in raw.items() if k != "reasoning"}
    spec = raw["reasoning"]
    is_qwen3_dialect = template.startswith(("vllm", "sglang"))
    if spec in ("off", False, None):
        if is_qwen3_dialect:
            ctk = dict(out.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = False
            out["chat_template_kwargs"] = ctk
        else:
            out["reasoning_effort"] = "none"
            out["reasoning_budget"] = 0
        return out
    if isinstance(spec, str):
        spec = {"effort": spec}
    if isinstance(spec, dict):
        if spec.get("effort"):
            out["reasoning_effort"] = spec["effort"]
        if spec.get("budget") is not None and not is_qwen3_dialect:
            out["reasoning_budget"] = spec["budget"]
    return out


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
            ("media", self.cfg.media, new.media),
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
            pr = self.cfg.profiles[name]
            # A profile with no idle-evict timer sits resident indefinitely
            # anyway; pin it on entry so reactive routing can't quietly swap it
            # out either. The floor profile is never auto-pinned — reactive entry
            # to the real profiles has to stay possible.
            self.state.pinned = not pr.idle_evict and not pr.default
            self.state.entered_at = now
            self.state.last_switch_at = now
        self.state.last_served_at = now
        self.rec.converge(self.state, name, now=now)

    def _profile_loaded_at(self) -> float | None:
        """Epoch time the active profile finished loading — the latest ``ready_at``
        across the stacks it wants. ``None`` while any wanted stack is still
        warming: the idle-evict countdown only starts once the profile is up."""
        pr = self.cfg.profiles[self.state.active_profile]
        wanted = [self.state.stacks[mn] for mn in pr.models if mn in self.state.stacks]
        if not wanted or any(rt.state == EngineState.warming for rt in wanted):
            return None
        ready_ats = [rt.ready_at for rt in wanted if rt.ready_at is not None]
        return max(ready_ats) if ready_ats else None

    def _idle_evict_at(self, now: float) -> float | None:
        """Epoch time idle-evict will fire for the active profile, or ``None``
        (floor / pinned / no timer / still loading). The countdown runs from when
        the profile finished loading, bumped forward by the last served request —
        not from activation, so a slow warm-up doesn't eat the idle window."""
        pr = self.cfg.profiles[self.state.active_profile]
        if pr.default or not pr.idle_evict or self.state.pinned:
            return None
        loaded = self._profile_loaded_at()
        if loaded is None:
            return None
        since = max(loaded, self.state.last_served_at or 0.0)
        return since + parse_duration(pr.idle_evict)

    def use(self, name: str, *, manual: bool = True, now: float | None = None) -> list:
        now = time.time() if now is None else now
        if name not in self.cfg.profiles:
            raise ManagerError(f"unknown profile {name!r}")
        active = self.cfg.profiles[self.state.active_profile]
        target = self.cfg.profiles[name]
        if not manual:
            if target.manual_only:
                raise Outranked(f"{name} is manual-only — load it by hand")
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

    def shutdown_engines(self, *, now: float | None = None) -> list[str]:
        """Tear down every engine container stackd spawned (all LLM stacks + the
        elastic image tier) through the Docker socket, then persist. For a clean
        whole-stack stop: `stackctl down`, or a SIGTERM when
        STACKD_TEARDOWN_ON_SIGTERM is set. Returns the names removed."""
        now = time.time() if now is None else now
        removed = self.rec.teardown_all(self.state, now)
        self._save()
        return removed

    def pin(self) -> None:
        self.state.pinned = True
        self._save()

    def unpin(self) -> None:
        self.state.pinned = False
        self._save()

    # -------------------------------------------------------------------- route ---
    def _preset_for(self, api_name: str, serving_template: str | None = None) -> dict:
        """The preset wherever this exact name is defined (so a glob-matched
        request still lands in the right reasoning mode), translated into the
        dialect of the engine that will serve it (``serving_template``); falls
        back to the defining model's own template."""
        for m in self.cfg.models.values():
            for se in m.serves:
                if se.api_name == api_name:
                    return _translate_preset(
                        dict(se.preset), serving_template or m.engine.template
                    )
        return {}

    def _served_name(self, model_name: str) -> str | None:
        e = self.cfg.models[model_name].engine
        # vLLM validates body["model"] against its --served-model-name; stackd
        # rewrites the body to this. Defaults to the stack name (see VllmCudaAdapter).
        return (e.params.get("served_model_name") or model_name) if e.template == "vllm-cuda" else None

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
                    stack=mn, artifact=self.cfg.models[mn].engine.model,
                    served_model_name=self._served_name(mn),
                    preset=self._preset_for(api_name, self.cfg.models[mn].engine.template),
                    route_kind="native" if exact else "standin",
                    note="" if status == "ok" else f"{mn} is {rt.state.value}",
                )

        # manual_only profiles are invisible to reactive entry — they load only on
        # an explicit `stackctl use` / activate.
        owners = [o for o in _profiles_serving(self.cfg, api_name)
                  if not self.cfg.profiles[o].manual_only]
        if not owners:
            return RouteResult("unknown", api_name, note="no model serves this name")
        top = owners[0]
        if self.cfg.profiles[top].priority > pr.priority:
            # `top` outranks the active profile — normally we'd auto-switch. But if
            # the active profile is PINNED the switch will never happen, so fail
            # fast with a clear reason instead of letting the caller poll a
            # warming stack that never comes (a manual profile like `chat-next`
            # that scopes its serves stays pinned and would otherwise hang any
            # request for a name only a higher-priority profile serves).
            if self.state.pinned:
                return RouteResult(
                    "outranked", api_name, profile=top,
                    note=(f"{api_name!r} is served by profile {top!r}, but the active "
                          f"profile {pr.profile!r} is pinned — unpin it or switch by hand"),
                )
            try:
                self.use(top, manual=False, now=now)
            except Outranked as e:
                # transient refusal (e.g. min-residency window) — the caller's
                # readiness poll re-routes each second and recovers on its own.
                return RouteResult("warming", api_name, profile=top, note=str(e))
            return RouteResult("warming", api_name, profile=top, note=f"entered {top}; warming")
        return RouteResult("outranked", api_name, profile=top,
                           note=f"{top} is not higher priority than active {pr.profile}")

    # --------------------------------------------------------------------- tick ---
    def tick(self, *, now: float | None = None) -> list:
        now = time.time() if now is None else now
        events = self.rec.tick(self.state, now=now)
        pr = self.cfg.profiles[self.state.active_profile]
        deadline = self._idle_evict_at(now)
        if (deadline is not None and now > deadline
                and now - (self.state.entered_at or now) > self.min_residency_s):
            evicted = self.state.active_profile
            self.evict(now=now)
            events.append(_Evt(evicted, "idle-evict", f"no traffic for {pr.idle_evict}"))
        self._save()
        return events

    # ----------------------------------------------------- image / video caps ---
    def _codevice_warming(self, model: str) -> bool:
        dev = self.state.stacks[model].device
        return any(o != model and ort.device == dev and ort.state == EngineState.warming
                   for o, ort in self.state.stacks.items())

    def _slot_entry(self, slot) -> dict:
        codev = any(rt.device == slot.device and rt.state == EngineState.warming
                    for rt in self.state.stacks.values())
        return {
            "stack": f"image:{slot.active_model}", "kind": slot.kind,
            "active_model": slot.active_model,
            "capabilities": list(slot.capabilities),
            "state": slot.state.value,
            "serveable": slot.state == EngineState.ready and not codev,
            "co_device_warming": codev, "endpoint": slot.endpoint,
            "device": slot.device, "backend": slot.backend,
            "since": slot.since, "pinned_by": slot.pinned_by,
        }

    def capabilities(self) -> dict:
        slot = self.state.image
        entry = self._slot_entry(slot) if slot is not None else None
        by_kind = {entry["kind"]: entry} if entry else {}
        return {"active_profile": self.state.active_profile,
                "engines": [entry] if entry else [],
                "image": by_kind.get("image"), "video": by_kind.get("video")}

    def image_status(self) -> dict:
        """State of the elastic image tier: what's resident, the free VRAM it has
        to work with under the active profile, and the load-order catalog."""
        from stackd.solver import headroom
        tier = self.cfg.media.get("image")
        slot = self.state.image
        hr = (headroom(self.cfg, self.state.active_profile, self.catalog,
                       reserve_gib=tier.margin_gib) if tier else {})
        return {
            "resident": self._slot_entry(slot) if slot is not None else None,
            "headroom_gib": {k: round(v, 1) for k, v in hr.items()},
            "prefer": [
                {"active_model": l.active_model, "capabilities": list(l.capabilities),
                 "backends": list(l.backends), "footprint_gib": dict(l.footprint_gib)}
                for l in (tier.prefer if tier else [])
            ],
        }

    def set_image(self, *, model: str | None = None, need_capability: str | None = None,
                  backend: str | None = None, unsafe: bool = False,
                  now: float | None = None) -> dict:
        """Request an image-tier swap — by explicit model or by a capability the
        caller needs. `backend`/`unsafe` are the deliberate-measurement escape
        hatch (see reconciler.py::swap_image) — forcing a specific backend and/or
        bypassing the device-VRAM and real-host-RAM fit checks, for benching a
        model whose own current footprint_gib/host_ram_gib estimate is what's
        wrongly blocking it. Returns swap_image()'s result dict ({ok, active_model,
        downgraded_from, note, ...} or {ok:False, error})."""
        now = time.time() if now is None else now
        caps = [need_capability] if need_capability else None
        pinned = "user" if model else ("capability" if need_capability else "auto")
        res = self.rec.swap_image(self.state, self.state.active_profile,
                                  model=model, need_caps=caps, now=now, pinned_by=pinned,
                                  backend=backend, unsafe=unsafe)
        if res.get("ok"):
            self.state.last_served_at = now
        self._save()
        return res

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
        slot = self.state.image
        if slot is not None:
            out.append({"stack": f"image:{slot.active_model}", "owner_profile": "media:image",
                        "state": slot.state.value, "active_model": slot.active_model,
                        "served": list(slot.capabilities), "endpoint": slot.endpoint,
                        "extra": {"kind": slot.kind, "device": slot.device,
                                  "backend": slot.backend, "since": slot.since,
                                  "pinned_by": slot.pinned_by}})
        return out

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
        if kind in self.cfg.media:
            return None, (f"no {kind} engine resident — no device headroom under "
                          f"profile {self.state.active_profile!r}")
        return None, f"no {kind} tier configured (config/media/{kind}.yaml)"

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
                    # native  = the ACTIVE profile serves this name via an EXACT
                    #           `serves` entry (its home model).
                    # standin = only a glob match answers for it in the active
                    #           profile (a cover model, e.g. coding's
                    #           `TakacsAI*` standing in for chat).
                    exact_here = any(_serves(self.cfg, x, se.api_name)[1] for x in pr_active.models)
                    glob_here = any(_serves(self.cfg, x, se.api_name)[0] for x in pr_active.models)
                    up = any(x in running for x in pr_active.models
                             if _serves(self.cfg, x, se.api_name)[0])
                    seen[se.api_name] = {
                        "id": se.api_name, "context_length": ctx,
                        # who actually answers this right now: the active profile
                        # if it serves the name, else its highest-priority home.
                        "owner_profile": self.state.active_profile if glob_here else pr.profile,
                        "native": exact_here,
                        "standin": glob_here and not exact_here,
                        "ready": (exact_here or glob_here) and up,
                    }
        return list(seen.values())

    # ------------------------------------------------------------------- status ---
    def status(self) -> dict:
        report = validate_profile(self.cfg, self.state.active_profile, self.catalog)
        running = set(self.state.stacks)
        pr = self.cfg.profiles[self.state.active_profile]
        # seconds until idle-evict fires (None = never: floor / pinned / no timer /
        # still loading). Counts from profile-loaded, not activation. Can go
        # negative during the min-residency hold.
        now = time.time()
        deadline = self._idle_evict_at(now)
        idle_evict_in = None if deadline is None else round(deadline - now)
        return {
            "active_profile": self.state.active_profile,
            "pinned": self.state.pinned,
            "idle_evict": pr.idle_evict,
            "idle_evict_in_s": idle_evict_in,
            "placement": report.placement,
            "unplaced": report.unplaced,
            "stacks": {
                n: {"state": rt.state.value, "device": rt.device, "kind": rt.kind,
                    "owner": rt.owner_profile, "port": rt.port, "container": rt.container,
                    "endpoint": rt.endpoint, "restarts": rt.restarts}
                for n, rt in self.state.stacks.items()
            },
            "image": (
                {"active_model": self.state.image.active_model,
                 "state": self.state.image.state.value,
                 "device": self.state.image.device,
                 "backend": self.state.image.backend,
                 "container": self.state.image.container,
                 "pinned_by": self.state.image.pinned_by}
                if self.state.image is not None else None
            ),
            "missing": [n for n in report.resident if n not in running],
            "pools": [{"pool": p.pool, "used": p.used_gib, "limit": p.limit_gib,
                       "headroom": p.headroom_gib, "ok": p.ok, "breakdown": p.breakdown}
                      for p in report.pools],
            "devices": [{"device": d.device, "budget": d.budget_gib, "used": d.used_gib,
                         "headroom": d.headroom_gib, "ok": d.ok, "models": d.models}
                        for d in report.devices],
            "sources": report.sources,
            "deltas": {m: list(v) for m, v in report.deltas.items()},
            "model_vram": report.model_vram,
            "model_ram": report.model_ram,
            "flags": report.flags,
        }


@dataclass
class _Evt:
    stack: str
    action: str
    detail: str = ""
