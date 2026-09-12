"""Drive the running set toward a profile's *resolved placement* (from the
solver) and keep it there. Models whose identity is unchanged are never touched;
only the delta is rebuilt. Teardown before spawn (a changed model implies a gap
anyway, and the box is memory-tight).

`tick()` polls health, restarts crashed engines with exponential backoff.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from dataclasses import asdict, dataclass, field

from stackd.config.models import Budget, Config, EngineSpec, ImageSupport, MediaTier, ModelSpec, Placement
from stackd.engines.base import EngineState
from stackd.engines.registry import adapter_for
from stackd.runner import LaunchContext, Mount, Runner
from stackd.solver import Placed, headroom, host_ram_headroom, solve
from stackd.state import ImageSlot, RuntimeState, StackRuntime

_UNHEALTHY_LIMIT = 3


@dataclass
class ReconcileEvent:
    stack: str
    action: str
    detail: str = ""


@dataclass
class Reconciler:
    cfg: Config
    runner: Runner
    models_dir: str = "/models"
    catalog: object | None = None
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0
    max_restarts: int = 5
    # After tearing down a cuda0 engine, wait for the driver's VRAM scrubber to
    # finish before allocating on top (concurrent big-free + big-alloc wedges the
    # RTX on a Thunderbolt / ReBAR-off link). See _drain_cuda.
    cuda_drain_enabled: bool = True
    cuda_drain_timeout_s: float = 90.0
    cuda_drain_poll_s: float = 3.0
    cuda_drain_settle_reads: int = 2
    # Two GPU processes cold-starting on the SAME device at the same instant is a
    # real contention hazard on this box — code-autocomplete (Vulkan) exit-0'd
    # within a second of comfyui-rocm both landing on igpu0 during a boot converge
    # (2026-09-12, self-healed by the crash-restart backoff 20s later, but a known
    # condition doesn't need to page through a crash to recover). A short gap
    # between same-device spawns costs nothing when nothing shares a device, and
    # only ever fires more than once per converge on a box that puts more than one
    # engine on one physical GPU.
    same_device_spawn_stagger_s: float = 3.0
    events: list[ReconcileEvent] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers ---
    def _emit(self, stack: str, action: str, detail: str = "") -> None:
        self.events.append(ReconcileEvent(stack, action, detail))

    def _record_timing(self, name: str, device: str, field_: str, secs: float) -> None:
        """Fold a measured swap-phase duration (load_s / teardown_s) into the
        model's catalog entry, EMA-smoothed and persisted to the overlay dir.
        This is the passive half of "automated benchmarking" — every real switch
        refines the numbers `stackctl bench` seeds. Best-effort, never raises.

        Set STACKD_PASSIVE_TIMINGS=0 to freeze the catalog `timings` blocks
        (they then only change via an explicit `stackctl bench`) — avoids the
        git churn on the tracked overlay dir from routine profile switches."""
        if os.getenv("STACKD_PASSIVE_TIMINGS", "1").strip().lower() in ("0", "false", "no", "off", ""):
            return
        try:
            if not getattr(self, "catalog", None) or secs is None or secs <= 0 or secs > 3600:
                return
            if name not in self.cfg.models:
                return
            from stackd.catalog import curve_key
            m = self.cfg.models[name]
            self.catalog.update_timings(
                curve_key(self.cfg, name, device or None),
                model=m.engine.model or name, engine=m.engine.template, device=device,
                **{field_: secs},
            )
        except Exception:  # noqa: BLE001 — telemetry must not break a converge
            pass

    def _lc(self, device: str, port: int | None) -> LaunchContext:
        from stackd.runner import DeviceKnobs
        rt = self.cfg.runtime
        backend = self.cfg.devices[device].backend.value
        dp = rt.device_profiles.get(backend)
        knobs = DeviceKnobs(
            gpus=dp.gpus, devices=list(dp.devices), group_add=list(dp.group_add),
            security_opt=list(dp.security_opt), ipc_host=dp.ipc_host,
            shm_size=dp.shm_size, env=dict(dp.env),
        ) if dp else DeviceKnobs()
        return LaunchContext(
            host_models_dir=rt.host_models_dir or self.models_dir,
            network=rt.network, port=port,
            device_index=getattr(self.cfg.devices[device], "index", 0),
            images=dict(rt.images),
            extra_mounts=[Mount(m.host_path, m.container_path, m.ro) for m in rt.mounts],
            device=knobs,
        )

    def _solve(self, profile: str):
        return solve(self.cfg, profile, self.catalog)

    def refresh_specs(self, state: RuntimeState) -> None:
        """Re-derive spec-driven fields (health URLs, timeouts, endpoint) for
        stacks kept across a daemon restart, so an engine-adapter or config
        change takes effect without a full stack rebuild. Best-effort."""
        for name, rt in state.stacks.items():
            try:
                m = self.cfg.models.get(name)
                if not m or rt.device not in self.cfg.devices:
                    continue
                adapter = adapter_for(m, self.cfg.devices[rt.device])
                spec = adapter.launch_spec(self._lc(rt.device, rt.port))
                rt.health_url = spec.health_url
                rt.live_health_url = spec.live_health_url
                rt.deep_health_every = spec.deep_health_every
                rt.ready_timeout = spec.ready_timeout_s
                rt.stop_grace = spec.stop_grace_s
                if adapter.endpoint(rt.port):
                    rt.endpoint = adapter.endpoint(rt.port)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- start/stop ---
    def _start(self, state: RuntimeState, p: Placed, owner: str, now: float) -> None:
        m = self.cfg.models[p.name]
        adapter = adapter_for(m, self.cfg.devices[p.device])
        spec = adapter.launch_spec(self._lc(p.device, p.port))
        handle = self.runner.spawn(spec)
        state.stacks[p.name] = StackRuntime(
            name=p.name, owner_profile=owner, kind="container", device=p.device,
            identity=p.identity, state=EngineState.warming, handle=handle,
            container=spec.name, port=p.port, health_url=spec.health_url,
            live_health_url=spec.live_health_url, deep_health_every=spec.deep_health_every,
            endpoint=adapter.endpoint(p.port), started_at=now,
            ready_timeout=spec.ready_timeout_s,
            stop_grace=spec.stop_grace_s,
        )
        self._emit(p.name, "spawn", f"{spec.name} on {p.device}")

    def _stop(self, state: RuntimeState, name: str, now: float, *, remove: bool = False) -> None:
        rt = state.stacks.get(name)
        if rt is None:
            return
        rt.intentional_stop = True
        is_cuda = self.cfg.devices[rt.device].backend.value == "cuda"
        # R1: fast SIGKILL on cuda0 to clear the driver quickly, unless the model
        # sets its own stop_grace_s (vLLM needs a graceful shutdown of its workers).
        grace = rt.stop_grace if rt.stop_grace is not None else (5 if is_cuda else 30)
        _t0 = time.time()
        self.runner.stop(rt.handle or rt.container, remove=remove, timeout=grace)
        _dt = time.time() - _t0
        rt.stopped_at = time.time()
        self._emit(name, "teardown",
                   ("remove" if remove else "stop") + f" (t={grace}s) — {_dt:.0f}s")
        self._record_timing(name, rt.device, "teardown_s", _dt)
        if is_cuda and not self.runner.probe_accelerator("cuda"):
            self._emit(name, "degraded", "nvidia-smi did not respond after teardown")
        state.stacks.pop(name, None)

    def teardown_all(self, state: RuntimeState, now: float, *, why: str = "shutdown") -> list[str]:
        """Stop + remove every engine this reconciler manages — the elastic image
        tier and all LLM stacks — via the runner (i.e. through the Docker socket).
        For a deliberate whole-stack stop so nothing stackd spawned is left
        orphaned holding VRAM after the daemon exits. Best-effort: one engine that
        won't die doesn't abort the rest. Returns the names it removed."""
        removed: list[str] = []
        if state.image is not None:
            removed.append(f"image:{state.image.active_model}")
            try:
                self._teardown_image(state, now, why=why)
            except Exception as e:  # noqa: BLE001
                self._emit("image", "teardown-error", repr(e))
                state.image = None
        for n in list(state.stacks):
            try:
                self._stop(state, n, now, remove=True)
            except Exception as e:  # noqa: BLE001
                self._emit(n, "teardown-error", repr(e))
                state.stacks.pop(n, None)
            removed.append(n)
        return list(dict.fromkeys(removed))

    def _drain_cuda(self, freed: list[str]) -> bool:
        """Poll free VRAM after a cuda0 teardown until it has settled (stopped
        climbing), then allow the spawn. Returns False — abort the spawn — if
        nvidia-smi stops responding (the driver is wedging; piling a big alloc on
        top makes it a hard wedge that needs a reboot).

        Trusting `memory.free` to *rise then plateau* is the signal the async
        scrubber has finished releasing what `freed` held."""
        self._emit("-", "drain", f"waiting for cuda VRAM to settle after {', '.join(freed)}")
        deadline = time.time() + self.cuda_drain_timeout_s
        peak = -1
        stable = 0
        misses = 0
        while time.time() < deadline:
            free = self.runner.gpu_free_mib()
            if free is None:
                misses += 1
                if misses >= 2:
                    self._emit("-", "degraded",
                               "nvidia-smi unresponsive during cuda drain — aborting spawn (card wedging)")
                    return False
                time.sleep(self.cuda_drain_poll_s)
                continue
            misses = 0
            if free >= peak and free - max(peak, 0) < 512:
                stable += 1
                if stable >= self.cuda_drain_settle_reads:
                    self._emit("-", "drain", f"cuda VRAM settled — {free} MiB free")
                    return True
            else:
                stable = 0
            peak = max(peak, free)
            time.sleep(self.cuda_drain_poll_s)
        self._emit("-", "drain", f"cuda drain timed out after {self.cuda_drain_timeout_s:.0f}s — proceeding")
        return True

    def _stagger_device(self, device: str, avoid_devices: set[str]) -> None:
        """Sleep briefly if `device` already had something spawned onto it earlier
        in this same converge — see same_device_spawn_stagger_s. A no-op (and free)
        whenever nothing shares a device, which is most boxes most of the time."""
        if device in avoid_devices:
            self._emit("-", "stagger",
                       f"{device} already spawning this converge — "
                       f"{self.same_device_spawn_stagger_s:.0f}s gap before piling on")
            time.sleep(self.same_device_spawn_stagger_s)

    # -------------------------------------------------------------- image tier ---
    def _image_identity(self, tier: MediaTier, backend: str, active_model: str, device: str) -> list:
        return [active_model, backend, device, asdict(tier.containers[backend])]

    def _synth_image_model(self, tier: MediaTier, backend: str, active_model: str,
                           caps: list[str]) -> ModelSpec:
        """A throwaway ModelSpec so the ComfyUI engine adapter builds the LaunchSpec
        the same way it does for any container — the checkpoint is chosen at
        runtime, not declared."""
        return ModelSpec(
            name=f"image:{active_model}",
            engine=EngineSpec(
                template="comfyui",
                model=active_model,
                params={"active_model": active_model, "kind": tier.kind,
                        "capabilities": list(caps), "port": 8188},
                container=copy.deepcopy(tier.containers[backend]),
            ),
            budget=Budget(),
            placement=Placement(),
        )

    def _dev_is_cuda(self, dev: str) -> bool:
        d = self.cfg.devices.get(dev)
        return bool(d) and d.backend.value == "cuda"

    def _backend_fit(self, ld, backend: str, hr: dict[str, float], host_avail: float, *,
                     margin_gib: float = 0.0, mem_total_gib: float | None = None):
        """(ok, device, reason) for ONE (candidate, backend), in the order that
        matters: the shared host-RAM pool first, then the device's own VRAM. This
        is the single implementation of "would it fit" — `_pick_image()` and the
        `/image` ladder verdicts both call it, so the dashboard can never claim a
        model is loadable when the scheduler would refuse it (2026-09-08: the
        ladder checked device VRAM only, so flux2-dev-turbo advertised itself on
        the iGPU while its host_ram_gib figure alone exceeded the box).

        `host_avail` is what the candidate may actually use, credit already added
        in (see `_teardown_credit`). The reason is deliberately a short clause the
        UI can show verbatim."""
        fp = ld.footprint_gib.get(backend)
        if fp is None:
            return False, None, f"no {backend} footprint on record"
        host_fp = ld.host_ram_gib.get(backend)
        if host_fp is not None and host_fp > host_avail + 1e-6:
            note = (f" (unreachable: {host_fp:.0f} + {margin_gib:.0f} margin exceeds "
                    f"MemTotal {mem_total_gib:.1f})") if (mem_total_gib
                    and host_fp + margin_gib > mem_total_gib) else ""
            return False, None, (f"host RAM {host_fp:.0f} > {host_avail:.1f} free ({backend})"
                                 + note)
        devs = [d for d in self.cfg.devices
                if self.cfg.devices[d].backend.value == backend]
        for dev in sorted(devs, key=lambda d: -hr.get(d, 0.0)):
            if hr.get(dev, 0.0) + 1e-6 >= fp:
                return True, dev, ""
        if not devs:
            return False, None, f"no {backend} device configured"
        best = max(devs, key=lambda d: hr.get(d, 0.0))
        return False, None, f"VRAM {fp:.0f} > {hr.get(best, 0.0):.1f} free on {best}"

    def _teardown_credit(self, state: RuntimeState, tier: MediaTier):
        """(GiB of host RAM freed, backend it is pinned to) if a swap destroys the
        RESIDENT image model's container first — see `_pick_image` for why a
        same-backend swap earns nothing. One helper because the ladder verdicts
        must credit exactly what `swap_image()` will."""
        slot = state.image
        if slot is None or slot.state not in (EngineState.warming, EngineState.ready):
            return 0.0, None
        rl = self._loadable(tier, slot.active_model)
        rh = (rl.host_ram_gib or {}).get(slot.backend) if rl else None
        return (float(rh), slot.backend) if rh else (0.0, None)

    def image_verdicts(self, state: RuntimeState, profile: str) -> dict:
        """The `prefer:` ladder as a click would experience it RIGHT NOW: same
        headroom, same live host-RAM reading, same teardown credit the scheduler
        uses. Consumed by GET /image; every row carries fits + why-not."""
        tier = self.cfg.media.get("image")
        if tier is None:
            return {"headroom_gib": {}, "prefer": []}
        hr = headroom(self.cfg, profile, self.catalog, reserve_gib=tier.margin_gib)
        avail = self._real_host_ram_avail(tier, profile)
        credit, credit_backend = self._teardown_credit(state, tier)
        from stackd.telemetry import host_stats
        total = host_stats().get("mem_total_gib")
        prefer = []
        for ld in tier.prefer:
            fit, any_fit, lands_on = {}, False, None
            for backend in ld.backends:
                credit_b = credit if (credit > 0 and backend != credit_backend) else 0.0
                ok, dev, reason = self._backend_fit(
                    ld, backend, hr, avail + credit_b,
                    margin_gib=tier.margin_gib, mem_total_gib=total)
                fit[backend] = {"device": dev, "fits": ok, "reason": reason,
                                "credit_gib": round(credit_b, 1)}
                if ok:
                    any_fit = True
                    lands_on = lands_on or (dev, backend)
            prefer.append({"active_model": ld.active_model,
                           "capabilities": list(ld.capabilities),
                           "backends": list(ld.backends), "fit": fit,
                           "fits": any_fit,
                           "would_load_on": list(lands_on) if lands_on else None})
        return {"headroom_gib": {k: round(v, 1) for k, v in hr.items()},
                "host_ram_avail_gib": round(avail, 1),
                "mem_total_gib": total,
                "teardown_credit_gib": round(credit, 1) if credit else 0.0,
                "prefer": prefer}

    def _pick_image(self, tier: MediaTier, hr: dict[str, float], *,
                    need_caps: list[str] | None = None, want_model: str | None = None,
                    host_ram_avail: float = float("inf"), want_backend: str | None = None,
                    freed_gib: float = 0.0, freed_backend: str | None = None,
                    reasons: list[str] | None = None):
        """(device, backend, loadable) — the first `prefer:` entry that fits a
        device with a matching backend, most-free device first. `want_model` /
        `need_caps` narrow the candidates first. `want_backend` forces trying
        only that backend instead of iterating the entry's listed order (still
        under full fit checks — a SAFE preference hint, unlike `unsafe=True` on
        swap_image(), which skips fit checks altogether). `host_ram_avail`
        additionally guards the shared system-RAM pool a device-VRAM check alone
        can't see (see solver.py::host_ram_headroom) — a candidate whose known
        host_ram_gib would exceed it is skipped even if its device VRAM/GTT
        fits, so this also protects an explicit manual swap, not just
        auto-pick. None if nothing fits.

        `freed_gib`/`freed_backend` credit the host RAM the RESIDENT image model
        will return when this swap destroys its container — credited only for a
        candidate on a DIFFERENT backend, since a same-backend swap relabels the
        slot in place (backend -> container is 1:1) and ComfyUI keeps the old
        checkpoint cached, freeing nothing. See swap_image().

        `reasons` (optional) collects why each rejected candidate was refused, so
        the caller can name the wall it actually hit — the old note quoted
        "needs ~56 GiB", the CUDA *VRAM* figure, when the refusal was really the
        122 GiB vulkan *host-RAM* estimate (2026-09-08)."""
        want = set(need_caps or [])
        for ld in tier.prefer:
            if want_model is not None and ld.active_model != want_model:
                continue
            if want and not want <= set(ld.capabilities):
                if reasons is not None:
                    reasons.append(f"{ld.active_model} lacks "
                                   f"{'+'.join(sorted(want - set(ld.capabilities)))}")
                continue
            backends = [want_backend] if want_backend else ld.backends
            for backend in backends:
                if backend not in ld.backends:
                    continue
                credit = freed_gib if (freed_gib > 0 and backend != freed_backend) else 0.0
                ok, dev, reason = self._backend_fit(ld, backend, hr, host_ram_avail + credit,
                                                    margin_gib=tier.margin_gib)
                if ok:
                    return dev, backend, ld
                if reasons is not None:
                    reasons.append(reason + (f" (+{credit:.0f} freeing)" if credit else ""))
        return None

    def _refusal_detail(self, reasons: list[str]) -> str:
        """Why a wanted model was refused, for the user-facing note."""
        return "; ".join(reasons[:3]) if reasons else "device VRAM and/or host RAM"

    def _loadable(self, tier: MediaTier, active_model: str):
        return next((l for l in tier.prefer if l.active_model == active_model), None)

    def _real_host_ram_avail(self, tier: MediaTier, profile: str) -> float:
        """Real currently-free host RAM (GiB), minus the tier's margin — NOT
        solver.py::host_ram_headroom()'s budget figure, which bakes in the
        host_unified pool's host_reserve/load_slack PLANNING constants (meant
        for deciding where an LLM CAN go before it's resident) rather than
        reflecting memory that's actually free right now. Those constants
        made the image-tier safety check far too conservative in practice
        (2026-09-04: it refused every candidate, including ones well under
        half of what was genuinely free) — checking real availability is both
        more accurate and more honest about what this guard actually protects
        against. Falls back to the budget figure only if /proc/meminfo can't
        be read (some non-host test/dev environment)."""
        from stackd.telemetry import host_stats

        avail = host_stats().get("mem_available_gib")
        if avail is None:
            return host_ram_headroom(self.cfg, profile, self.catalog, reserve_gib=tier.margin_gib)
        return max(0.0, avail - tier.margin_gib)

    def swap_image(self, state: RuntimeState, profile: str, *, model: str | None = None,
                   need_caps: list[str] | None = None, now: float, pinned_by: str,
                   backend: str | None = None, unsafe: bool = False) -> dict:
        """Programmatic image-tier swap — by explicit `model` or by `need_caps`
        (the capability the caller needs resident). Auto-downgrades to the next
        capable entry that fits and SAYS so; 409-style {ok:False} when nothing
        capable fits. Never touches an LLM engine.

        `backend` forces a specific backend instead of the prefer-ladder's auto
        pick. `unsafe=True` (requires `backend`) skips `_pick_image()` and its
        device-VRAM / real-host-RAM checks entirely — for deliberately measuring
        a model's TRUE footprint when the CURRENT footprint_gib/host_ram_gib
        estimate is itself what's wrongly blocking it (the exact case that
        estimate exists to be corrected from — 2026-09-04, benching
        flux2-dev-turbo kept failing because its own conservative estimate
        exceeded every real-available-memory reading on the box, even fully
        idle). The container's mem_limit_gib hard ceiling still applies
        regardless; this bypasses only the soft, estimate-based checks."""
        self.events.clear()
        tier = self.cfg.media.get("image")
        if tier is None:
            return {"ok": False, "error": "no image tier configured (config/media/image.yaml)"}
        hr = headroom(self.cfg, profile, self.catalog, reserve_gib=tier.margin_gib)
        hr_r = {k: round(v, 1) for k, v in hr.items()}
        host_ram_avail = self._real_host_ram_avail(tier, profile)
        slot = state.image
        live = slot is not None and slot.state in (EngineState.warming, EngineState.ready)

        # already satisfied? (an unsafe request still re-checks backend below —
        # "already resident on the WRONG backend" must not short-circuit here)
        if live and not unsafe:
            if model and slot.active_model == model:
                return {"ok": True, "active_model": model, "device": slot.device,
                        "warming": slot.state == EngineState.warming, "note": "already resident"}
            if need_caps and set(need_caps) <= set(slot.capabilities):
                return {"ok": True, "active_model": slot.active_model, "device": slot.device,
                        "warming": slot.state == EngineState.warming,
                        "note": f"resident model {slot.active_model!r} already provides "
                                f"{', '.join(need_caps)}"}

        if unsafe:
            if not model or not backend:
                return {"ok": False, "error": "--unsafe requires both a model and a backend"}
            ld = self._loadable(tier, model)
            if ld is None:
                return {"ok": False, "error": f"unknown image model {model!r}; "
                        f"prefer: {[l.active_model for l in tier.prefer]}"}
            if backend not in ld.backends:
                return {"ok": False, "error": f"{model!r} doesn't support backend {backend!r} "
                        f"(supports: {ld.backends})"}
            dev = next((d for d in self.cfg.devices
                       if self.cfg.devices[d].backend.value == backend), None)
            if dev is None:
                return {"ok": False, "error": f"no device configured for backend {backend!r}"}
            if live and slot.active_model == model and slot.device == dev:
                return {"ok": True, "active_model": model, "device": dev, "backend": backend,
                        "warming": slot.state == EngineState.warming,
                        "note": "already resident on the requested backend", "unsafe": True}
            self._emit(f"image:{ld.active_model}", "unsafe-load",
                       f"BYPASSING fit checks (device VRAM + real host RAM) — forced onto {dev}/{backend}")
            was_cuda = slot is not None and self._dev_is_cuda(slot.device)
            if slot is not None:
                self._teardown_image(state, now, why=f"unsafe swap to {ld.active_model}")
            if self.cuda_drain_enabled and was_cuda and self._dev_is_cuda(dev):
                self._drain_cuda([f"image:{model}"])
            self._spawn_image(state, tier, dev, backend, ld, now, pinned_by=pinned_by)
            return {"ok": True, "active_model": ld.active_model, "device": dev, "backend": backend,
                    "warming": True, "unsafe": True,
                    "note": "fit checks bypassed — NOT verified against device VRAM or real host "
                            "RAM availability; watch it yourself",
                    "events": [f"{e.action} {e.stack}" for e in self.events]}

        # Teardown credit: this swap destroys the resident container whenever it
        # lands on a different backend, returning that model's host RAM *before*
        # the new one spawns — so refusing a candidate over memory this very swap
        # is about to free is wrong (2026-09-08: a resident klein blocked
        # dev-turbo's cuda route on host RAM while klein was the thing being
        # replaced). Shared with `image_verdicts()` via `_teardown_credit()` so the
        # dashboard's verdict and this decision can never disagree.
        freed_gib, freed_backend = self._teardown_credit(state, tier)
        reasons: list[str] = []
        pick = self._pick_image(tier, hr, need_caps=need_caps, want_model=model,
                                host_ram_avail=host_ram_avail, want_backend=backend,
                                freed_gib=freed_gib, freed_backend=freed_backend,
                                reasons=reasons)
        # Keep WHY THE REQUESTED MODEL was refused: the ladder scan below runs
        # cap-wide and would otherwise overwrite it with other models' reasons.
        wanted_reasons = list(reasons)
        downgraded_from = None
        if pick is None and model is not None:
            req = self._loadable(tier, model)
            if req is None:
                return {"ok": False, "error": f"unknown image model {model!r}; "
                        f"prefer: {[l.active_model for l in tier.prefer]}"}
            reasons = []
            pick = self._pick_image(tier, hr, need_caps=need_caps or list(req.capabilities),
                                    host_ram_avail=host_ram_avail, want_backend=backend,
                                    freed_gib=freed_gib, freed_backend=freed_backend,
                                    reasons=reasons)
            if pick is not None:
                downgraded_from = model
        if pick is None:
            what = (f"a model providing {', '.join(need_caps)}" if need_caps
                    else f"model {model!r}" if model else "an image model")
            return {"ok": False, "headroom": hr_r, "host_ram_headroom_gib": round(host_ram_avail, 1),
                    "error": f"no {what} fits the headroom under profile {profile!r} "
                             f"(device VRAM and/or real host-RAM budget)"
                             + (f" — {model}: {self._refusal_detail(wanted_reasons)}"
                                if model and wanted_reasons else "")}

        dev, backend, ld = pick
        note = None
        if downgraded_from:
            dl = self._loadable(tier, downgraded_from)
            fp = min(dl.footprint_gib.values()) if dl and dl.footprint_gib else None
            # Name the wall actually hit. The old note quoted min(footprint_gib)
            # — the CUDA *VRAM* figure — for refusals that were really the
            # vulkan *host-RAM* estimate, pointing at the one axis that wasn't
            # the problem; the fallback keeps that wording if no reason was
            # recorded, so the note can never come out empty.
            note = (f"{downgraded_from} did not fit under profile {profile!r}"
                    + (f": {self._refusal_detail(wanted_reasons)}" if wanted_reasons
                       else (f" (needs ~{fp:.0f} GiB)" if fp else ""))
                    + f" — using {ld.active_model} instead")

        if live and slot.active_model == ld.active_model and slot.device == dev:
            return {"ok": True, "active_model": ld.active_model, "device": dev,
                    "warming": slot.state == EngineState.warming,
                    "downgraded_from": downgraded_from,
                    "note": note or "already resident", "headroom": hr_r}

        # Same container, different checkpoint -> no bounce. One ComfyUI image runs
        # every pipeline for its backend; the MCP picks the graph per request from
        # `active_model`, and ComfyUI swaps the checkpoint itself. Just relabel the
        # slot (the fit was already checked by _pick_image).
        if (live and slot.backend == backend and slot.device == dev
                and slot.container == tier.containers[backend].name):
            slot.active_model = ld.active_model
            slot.capabilities = list(ld.capabilities)
            slot.since = now
            slot.pinned_by = pinned_by
            slot.identity = self._image_identity(tier, backend, ld.active_model, dev)
            self._emit(f"image:{ld.active_model}", "reload", "pipeline swapped in place (same container)")
            return {"ok": True, "active_model": ld.active_model, "device": dev, "backend": backend,
                    "warming": slot.state == EngineState.warming, "in_place": True,
                    "downgraded_from": downgraded_from, "note": note, "headroom": hr_r,
                    "events": [f"{e.action} {e.stack}" for e in self.events]}

        was_cuda = slot is not None and self._dev_is_cuda(slot.device)
        if slot is not None:
            self._teardown_image(state, now, why=f"swap to {ld.active_model}")
        if self.cuda_drain_enabled and was_cuda and self._dev_is_cuda(dev):
            self._drain_cuda([f"image:{model or ld.active_model}"])
        self._spawn_image(state, tier, dev, backend, ld, now, pinned_by=pinned_by)

        return {"ok": True, "active_model": ld.active_model, "device": dev, "backend": backend,
                "warming": True, "downgraded_from": downgraded_from, "note": note,
                "headroom": hr_r,
                "events": [f"{e.action} {e.stack}" for e in self.events]}

    def _spawn_image(self, state: RuntimeState, tier: MediaTier, device: str, backend: str,
                     ld, now: float, *, pinned_by: str = "auto") -> None:
        synth = self._synth_image_model(tier, backend, ld.active_model, ld.capabilities)
        adapter = adapter_for(synth, self.cfg.devices[device])
        spec = adapter.launch_spec(self._lc(device, 8188))
        handle = self.runner.spawn(spec)
        since = now
        if state.image is not None and state.image.active_model == ld.active_model:
            since = state.image.since or now
        state.image = ImageSlot(
            active_model=ld.active_model, kind=tier.kind, backend=backend, device=device,
            container=spec.name, handle=handle, endpoint=adapter.endpoint(8188),
            health_url=spec.health_url, state=EngineState.warming,
            capabilities=list(ld.capabilities), started_at=now,
            ready_timeout=spec.ready_timeout_s, since=since, pinned_by=pinned_by,
            identity=self._image_identity(tier, backend, ld.active_model, device),
        )
        self._emit(f"image:{ld.active_model}", "spawn", f"{spec.name} on {device} ({backend})")

    def _teardown_image(self, state: RuntimeState, now: float, *, why: str = "") -> None:
        slot = state.image
        if slot is None:
            return
        slot.intentional_stop = True
        d = self.cfg.devices.get(slot.device)
        grace = 5 if (d and d.backend.value == "cuda") else 30
        if slot.handle or slot.container:
            try:
                self.runner.stop(slot.handle or slot.container, remove=True, timeout=grace)
            except Exception as e:  # noqa: BLE001
                self._emit(f"image:{slot.active_model}", "teardown-error", repr(e))
        self._emit(f"image:{slot.active_model}", "teardown",
                   f"remove ({slot.container}){' — ' + why if why else ''}")
        state.image = None

    def _reconcile_image(self, state: RuntimeState, profile: str, *, now: float,
                         repick: bool, avoid_devices: set[str] = frozenset()) -> None:
        tier = self.cfg.media.get("image")
        if tier is None:
            if state.image is not None:
                self._teardown_image(state, now, why="no image tier configured")
            return

        pr = self.cfg.profiles.get(profile)
        if pr is not None and pr.image == ImageSupport.none:
            # A user-pinned slot survives `image: none` -- this policy means
            # "don't AUTO-fill the tier here", not "actively fight a deliberate
            # manual load". Without this carve-out, `bench-image` (the profile
            # THIS policy exists for) tore down its own bench's model on every
            # ~20s reconciler tick, a few seconds after `stackctl image bench`
            # spawned it for measurement -- a self-inflicted thrash loop
            # discovered 2026-09-04 trying to bench flux2-dev-turbo on it.
            if state.image is not None and state.image.pinned_by != "user":
                self._teardown_image(state, now, why=f"image: none under {profile}")
            return

        slot = state.image
        healthy = bool(slot and slot.handle
                       and slot.state in (EngineState.warming, EngineState.ready))
        if healthy and not repick:
            want_id = self._image_identity(tier, slot.backend, slot.active_model, slot.device)
            if slot.identity == want_id:
                return
            self._emit(f"image:{slot.active_model}", "reload", "image container spec changed")
            self._teardown_image(state, now)
            slot, healthy = None, False

        # A user-pinned slot that lost its container (force-removed for a clean bench
        # baseline, or crashed) must RESTORE the pinned model — not revert to the
        # auto-preferred tier. Re-spawn the pinned model on its original backend/device.
        # This runs only while the slot is unhealthy (handle is None), so it self-limits:
        # once the respawned container is warming/ready, `healthy` is True and the
        # early-return above takes over. If the pinned model no longer fits, fall through
        # to the auto-pick below.
        if slot is not None and not healthy and slot.pinned_by == "user":
            ld = self._loadable(tier, slot.active_model)
            if ld is not None and slot.backend in ld.backends:
                pin_hr = headroom(self.cfg, profile, self.catalog, reserve_gib=tier.margin_gib)
                pin_avail = self._real_host_ram_avail(tier, profile)
                pin_pick = self._pick_image(tier, pin_hr, host_ram_avail=pin_avail,
                                            want_model=slot.active_model, want_backend=slot.backend)
                if pin_pick is not None:
                    dev, backend, ld = pin_pick
                    self._stagger_device(dev, avoid_devices)
                    self._spawn_image(state, tier, dev, backend, ld, now, pinned_by="user")
                    return
            # Pinned model no longer fits (or is gone from the tier) -> fall through.

        hr = headroom(self.cfg, profile, self.catalog, reserve_gib=tier.margin_gib)
        host_ram_avail = self._real_host_ram_avail(tier, profile)
        pick = self._pick_image(tier, hr, host_ram_avail=host_ram_avail)
        if pick is None:
            if slot is not None:
                self._teardown_image(state, now, why="no headroom under the active profile")
            else:
                self._emit("image", "idle", "no device headroom for an image engine")
            return

        dev, backend, ld = pick
        if (slot is not None and healthy and slot.active_model == ld.active_model
                and slot.device == dev):
            return
        if slot is not None:
            self._teardown_image(state, now)
        self._stagger_device(dev, avoid_devices)
        self._spawn_image(state, tier, dev, backend, ld, now)

    def _image_tick(self, state: RuntimeState, now: float) -> None:
        slot = state.image
        if slot is None or slot.handle is None:
            return
        code = self.runner.poll(slot.handle)
        if code is not None and not slot.intentional_stop:
            self._emit(f"image:{slot.active_model}", "crash", f"exit {code}")
            if slot.pinned_by == "user":
                # A user-pinned slot whose container died (a force `docker rm -f` for a
                # clean bench baseline, or a real crash) must RESTORE the pinned model —
                # not revert to the auto-preferred tier. Keep the slot (clearing the dead
                # handle, going `down`) so _reconcile_image's user-pin carve-out re-spawns
                # it; only an auto-picked slot drops to `state.image = None` here.
                # (2026-09-08: a force-removed image container dropped the pin, so the
                # next tick re-converged to the auto tier — turbo/cuda — and a
                # `stackctl image bench` on the vulkan backend had no container to hit.)
                slot.handle = None
                slot.container = None
                slot.state = EngineState.down
            else:
                state.image = None
            return
        if not slot.health_url:
            return
        healthy = self.runner.http_ok(slot.health_url)
        if slot.state == EngineState.warming:
            if healthy:
                slot.state = EngineState.ready
                slot.ready_at = now
                slot.unhealthy_ticks = 0
                self._emit(f"image:{slot.active_model}", "ready")
            elif slot.started_at and now - slot.started_at > (slot.ready_timeout or 300.0):
                self._emit(f"image:{slot.active_model}", "crash", "warmup timeout")
                self._teardown_image(state, now)
        elif slot.state == EngineState.ready:
            if healthy:
                slot.unhealthy_ticks = 0
            else:
                slot.unhealthy_ticks += 1
                if slot.unhealthy_ticks >= _UNHEALTHY_LIMIT:
                    self._emit(f"image:{slot.active_model}", "crash", "health flatlined")
                    self._teardown_image(state, now)

    # ------------------------------------------------------------------ converge ---
    def converge(self, state: RuntimeState, target: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.events.clear()
        state.warmed_for = None   # every entry re-arms the `image: warm` trigger
        pl = self._solve(target)
        want = pl.placed
        target_models = set(self.cfg.profiles[target].models)

        def owner_of(n: str) -> str:
            if n in target_models:
                return target
            return state.stacks[n].owner_profile if n in state.stacks else target

        current = set(state.stacks)
        spawn = [n for n in want if n not in current]
        changed = [n for n in want if n in current and state.stacks[n].identity != want[n].identity]
        teardown = [n for n in current if n not in want]

        # An `adopt` container is compose-defined — stackd only start/stops it and never
        # holds its full create spec, so it cannot be recreated. An identity change on
        # one is almost always stackd-side metadata (comfyui `active_model` /
        # `capabilities`, surfaced via /capabilities, not baked into the container):
        # adopt the new identity in place, and only (re)start if it isn't running.
        adopt_changed = [n for n in changed if self.cfg.models[n].engine.container.adopt]
        reload_ = [n for n in changed if n not in adopt_changed]

        def _is_cuda(dev: str) -> bool:
            d = self.cfg.devices.get(dev)
            return bool(d) and d.backend.value == "cuda"

        # Capture cuda0 devices BEFORE _stop pops the stacks (for the drain barrier).
        freed_cuda = [n for n in teardown + reload_
                      if n in state.stacks and _is_cuda(state.stacks[n].device)]
        spawns_on_cuda = any(_is_cuda(want[n].device) for n in spawn + reload_)

        # LLM topology is changing -> the elastic image tier must give its VRAM
        # back first (the new profile may need all of it; we can't know ahead of
        # a real free). It rebuilds from headroom after the LLM stacks are placed.
        llm_changed = bool(spawn or reload_ or teardown)
        if llm_changed and state.image is not None:
            if _is_cuda(state.image.device):
                freed_cuda.append(f"image:{state.image.active_model}")
                spawns_on_cuda = True
            self._teardown_image(state, now, why=f"profile change to {target}")

        for n in current:  # re-tag kept
            if n in want and state.stacks[n].identity == want[n].identity:
                state.stacks[n].owner_profile = owner_of(n)

        # Teardown is best-effort: one engine that won't die must not abort the
        # rest of the switch (the stray-sweep in tick() is the backstop).
        for n in teardown:
            try:
                self._stop(state, n, now)
            except Exception as e:  # noqa: BLE001
                self._emit(n, "teardown-error", repr(e))
                state.stacks.pop(n, None)
        for n in reload_:
            self._stop(state, n, now, remove=True)

        # Drain barrier: let the RTX's VRAM scrubber finish freeing what we just
        # tore down before we allocate on top of it. If the card looks wedged,
        # abort the spawn phase — tick() retries once it recovers.
        if self.cuda_drain_enabled and freed_cuda and spawns_on_cuda:
            if not self._drain_cuda(freed_cuda):
                if pl.unplaced:
                    self._emit("-", "unplaced", ", ".join(pl.unplaced))
                return   # card wedging — don't pile the image tier on top either

        # Cold-starting two GPU processes onto the SAME device in the same instant
        # is a real contention hazard, not just a theoretical one — see
        # same_device_spawn_stagger_s. Tracked per-converge (not persisted): a
        # crash-restart's own backoff already staggers a later respawn plenty.
        started_devices: set[str] = set()

        for n in adopt_changed:
            rt = state.stacks[n]
            cn = rt.container or (self.cfg.models[n].engine.container.name or f"stackd-{n}")
            if self.runner.poll(cn) is None:  # already running — just re-tag
                rt.identity = want[n].identity
                rt.owner_profile = owner_of(n)
                self._emit(n, "reload", "adopt identity updated in place")
            else:
                self._stagger_device(want[n].device, started_devices)
                started_devices.add(want[n].device)
                self._start(state, want[n], owner_of(n), now)
        for n in spawn + reload_:
            self._stagger_device(want[n].device, started_devices)
            started_devices.add(want[n].device)
            self._start(state, want[n], owner_of(n), now)

        # The elastic image tier fills whatever VRAM is left. repick when the LLM
        # set moved (choose afresh for the new headroom); otherwise only act if the
        # slot is missing/unhealthy or its container spec changed under a reload.
        self._reconcile_image(state, target, now=now, repick=llm_changed,
                              avoid_devices=started_devices)

        # Backstop: any adopt-model container from *another* profile that is not
        # wanted here — stop it (compose may have auto-started it; e.g. the two
        # ComfyUIs). Adopt containers are never removed, only stopped.
        for mn, m in self.cfg.models.items():
            if m.engine.container.adopt and mn not in want and mn not in state.stacks:
                cn = m.engine.container.name or f"stackd-{mn}"
                if self.runner.poll(cn) is None:  # running
                    self.runner.stop(cn, remove=False)
                    self._emit(mn, "teardown", f"{cn} (idle adopt, not in {target})")

        if pl.unplaced:
            self._emit("-", "unplaced", ", ".join(pl.unplaced))

    # ---------------------------------------------------------------------- tick ---
    def tick(self, state: RuntimeState, *, now: float | None = None) -> list[ReconcileEvent]:
        now = time.time() if now is None else now
        self.events.clear()

        pl = self._solve(state.active_profile)
        want = set(pl.placed)
        for n, p in pl.placed.items():
            if n not in state.stacks:
                self._start(state, p, state.active_profile, now)

        # Strays: a stack in state that the active profile doesn't want — e.g. a
        # profile switch whose converge threw mid-teardown. Stop it (don't let the
        # crash-restart path below resurrect it forever) and drop it from state.
        for name in [n for n in state.stacks if n not in want]:
            self._stop(state, name, now)
            self._emit(name, "teardown", f"stray — not in active profile {state.active_profile}")

        for name, rt in list(state.stacks.items()):
            if rt.handle is not None:
                code = self.runner.poll(rt.handle)
                if code is not None and not rt.intentional_stop:
                    self._handle_exit(rt, code, now)
                    continue
            if rt.state == EngineState.error and rt.backoff_until and now >= rt.backoff_until:
                self._respawn(state, rt, now)
                continue
            if rt.handle is None:
                continue

            if rt.state == EngineState.warming:
                # readiness always uses the real (generative) health_url so we
                # don't flip `ready` before the model can actually serve
                healthy = bool(rt.health_url) and self.runner.http_ok(rt.health_url)
                if healthy:
                    rt.state = EngineState.ready
                    rt.ready_at = now
                    rt.unhealthy_ticks = 0
                    rt.health_ticks = 0
                    if rt.started_at:
                        self._record_timing(name, rt.device, "load_s", now - rt.started_at)
                    self._emit(name, "ready")
                elif rt.started_at and now - rt.started_at > (rt.ready_timeout or 300.0):
                    rt.state = EngineState.error
                    self._schedule_restart(rt, now, "warmup timeout")
            elif rt.state == EngineState.ready:
                # steady state: poll the cheap endpoint when the engine gave us
                # one (SGLang-Pennyroyal — its /health runs a forward pass and
                # would fight --sleep-on-idle every tick)
                probe = rt.live_health_url or rt.health_url
                healthy = bool(probe) and self.runner.http_ok(probe)
                deep_bad = False
                if healthy and rt.live_health_url and rt.deep_health_every and rt.health_url:
                    rt.health_ticks += 1
                    if rt.health_ticks >= rt.deep_health_every:
                        rt.health_ticks = 0
                        # generative check catches a wedged scheduler still
                        # serving the cheap endpoint; retry once for a transient
                        deep_bad = not (self.runner.http_ok(rt.health_url, timeout=15.0)
                                        or self.runner.http_ok(rt.health_url, timeout=15.0))
                if healthy and not deep_bad:
                    rt.unhealthy_ticks = 0
                else:
                    rt.unhealthy_ticks += 1
                    if deep_bad or rt.unhealthy_ticks >= _UNHEALTHY_LIMIT:
                        self._emit(name, "crash",
                                   "health flatlined (deep)" if deep_bad else "health flatlined")
                        self._schedule_restart(rt, now, "unhealthy")
                        rt.state = EngineState.error

        # Image tier: advance its health, then refill the slot if it died or was
        # never placed (e.g. boot_reset dropped it). No repick — sticky.
        self._image_tick(state, now)
        self._reconcile_image(state, state.active_profile, now=now, repick=False)
        self._maybe_warm_image(state, now)

        return list(self.events)

    def _maybe_warm_image(self, state: RuntimeState, now: float) -> None:
        """`image: warm` profiles force one throwaway generation as soon as both
        this profile's own LLM stacks and the image tier are ready, so the first
        real request doesn't pay ComfyUI's model-load latency. Fire-and-forget:
        the generation itself runs in ComfyUI's own queue, so we only need the
        submit call (a fast POST) off the tick thread, not the full render.

        Keyed on (profile, resident active_model) — not just the profile — so a
        manual `/image/model` swap (dashboard "Load" button, not a real
        generation request) re-arms this too: the newly-picked model gets
        warmed the same way a fresh profile activation does, instead of only
        the very first image model a profile ever lands on."""
        pr = self.cfg.profiles.get(state.active_profile)
        if pr is None or pr.image != ImageSupport.warm:
            return
        slot = state.image
        if slot is None or slot.state != EngineState.ready or not slot.endpoint:
            return
        warm_key = f"{state.active_profile}:{slot.active_model}"
        if state.warmed_for == warm_key:
            return
        if not all(state.stacks.get(n) and state.stacks[n].state == EngineState.ready
                   for n in pr.models):
            return
        state.warmed_for = warm_key   # arm before firing — never resubmit mid-flight
        model, endpoint = slot.active_model, slot.endpoint
        self._emit(f"image:{model}", "warm-fire", f"forcing a generation under {state.active_profile}")
        threading.Thread(target=self._fire_warm_generation, args=(model, endpoint),
                         daemon=True).start()

    @staticmethod
    def _fire_warm_generation(model: str, endpoint: str) -> None:
        # Everything -- imports included -- lives inside the try/except: this
        # runs on a daemon thread with nothing watching it, so an unhandled
        # exception here (e.g. the optional stackd[imagegen] extra isn't
        # installed) would otherwise just spew a raw traceback to stderr
        # instead of the same best-effort "never crash" swallow as the rest
        # of this best-effort warm-up.
        try:
            import asyncio

            from stackd.imagegen import workflows
            from stackd.imagegen.bench import DEFAULT_PROMPT
            from stackd.imagegen.comfyui_client import submit_workflow

            async def _go() -> None:
                _name, graph, nodes, _entry = workflows.load_model("generate", model)
                nid = nodes.get("positive") or nodes.get("prompt")
                if nid:
                    workflows.set_node(graph, nid, "positive", DEFAULT_PROMPT)
                await submit_workflow(graph, endpoint)

            asyncio.run(_go())
        except Exception:  # noqa: BLE001 — best-effort warm-up, never crash the tick loop
            pass

    # ----------------------------------------------------------------- restart ---
    def _backoff(self, restarts: int) -> float:
        return min(self.backoff_base_s * (2 ** max(0, restarts - 1)), self.backoff_cap_s)

    def _schedule_restart(self, rt: StackRuntime, now: float, why: str) -> None:
        if rt.restarts >= self.max_restarts:
            rt.backoff_until = None
            # The rich OCI/runtime error (e.g. an nvidia-container-cli NVML mismatch)
            # only matters once we're done retrying — it's what a human needs to fix
            # the actual cause, and Manager.tick() folds it into the profile's dead
            # mark so a caller sees WHY, not just "still warming" forever.
            detail = f"{why}; {rt.restarts} restarts exhausted"
            if rt.last_error:
                detail += f" — {rt.last_error}"
            self._emit(rt.name, "give-up", detail)
            return
        rt.backoff_until = now + self._backoff(rt.restarts + 1)
        self._emit(rt.name, "restart", f"{why}; retry in {rt.backoff_until - now:.0f}s")

    def _handle_exit(self, rt: StackRuntime, code: int, now: float) -> None:
        # Grab the OCI/runtime error (if any) BEFORE emitting "crash" — a bare
        # "exit 0" told us nothing about the 2026-09-12 code-autocomplete boot
        # race; whatever it actually was belongs in the log the instant it
        # happens, not just once (if ever) the restart budget is exhausted.
        rt.last_error = self.runner.last_error(rt.handle) if rt.handle else None
        detail = f"exit {code}" + (f" — {rt.last_error}" if rt.last_error else "")
        self._emit(rt.name, "crash", detail)
        rt.state = EngineState.error
        rt.handle = None
        self._schedule_restart(rt, now, f"exit {code}")

    def _respawn(self, state: RuntimeState, rt: StackRuntime, now: float) -> None:
        m = self.cfg.models[rt.name]
        adapter = adapter_for(m, self.cfg.devices[rt.device])
        spec = adapter.launch_spec(self._lc(rt.device, rt.port))
        rt.handle = self.runner.spawn(spec)
        rt.state = EngineState.warming
        rt.started_at = now
        rt.backoff_until = None
        rt.unhealthy_ticks = 0
        rt.health_ticks = 0
        rt.last_error = None
        rt.live_health_url = spec.live_health_url
        rt.deep_health_every = spec.deep_health_every
        rt.restarts += 1
        if rt.health_url:
            getattr(self.runner, "reset_health", lambda *_: None)(rt.health_url)
        self._emit(rt.name, "restart", f"respawn #{rt.restarts}")
