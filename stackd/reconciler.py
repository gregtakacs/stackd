"""Drive the running set toward a profile's *resolved placement* (from the
solver) and keep it there. Models whose identity is unchanged are never touched;
only the delta is rebuilt. Teardown before spawn (a changed model implies a gap
anyway, and the box is memory-tight).

`tick()` polls health, restarts crashed engines with exponential backoff.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from stackd.config.models import Config
from stackd.engines.base import EngineState
from stackd.engines.registry import adapter_for
from stackd.runner import LaunchContext, Mount, Runner
from stackd.solver import Placed, solve
from stackd.state import RuntimeState, StackRuntime

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
    events: list[ReconcileEvent] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers ---
    def _emit(self, stack: str, action: str, detail: str = "") -> None:
        self.events.append(ReconcileEvent(stack, action, detail))

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

    # ---------------------------------------------------------------- start/stop ---
    def _start(self, state: RuntimeState, p: Placed, owner: str, now: float) -> None:
        m = self.cfg.models[p.model]
        adapter = adapter_for(m, self.cfg.devices[p.device])
        spec = adapter.launch_spec(self._lc(p.device, p.port))
        handle = self.runner.spawn(spec)
        state.stacks[p.model] = StackRuntime(
            name=p.model, owner_profile=owner, kind="container", device=p.device,
            identity=p.identity, state=EngineState.warming, handle=handle,
            container=spec.name, port=p.port, health_url=spec.health_url,
            endpoint=adapter.endpoint(p.port), started_at=now,
            ready_timeout=spec.ready_timeout_s,
            stop_grace=spec.stop_grace_s,
        )
        self._emit(p.model, "spawn", f"{spec.name} on {p.device}")

    def _stop(self, state: RuntimeState, name: str, now: float, *, remove: bool = False) -> None:
        rt = state.stacks.get(name)
        if rt is None:
            return
        rt.intentional_stop = True
        is_cuda = self.cfg.devices[rt.device].backend.value == "cuda"
        # R1: fast SIGKILL on cuda0 to clear the driver quickly, unless the model
        # sets its own stop_grace_s (vLLM needs a graceful shutdown of its workers).
        grace = rt.stop_grace if rt.stop_grace is not None else (5 if is_cuda else 30)
        self.runner.stop(rt.handle or rt.container, remove=remove, timeout=grace)
        self._emit(name, "teardown",
                   ("remove" if remove else "stop") + f" (t={grace}s)")
        if is_cuda and not self.runner.probe_accelerator("cuda"):
            self._emit(name, "degraded", "nvidia-smi did not respond after teardown")
        state.stacks.pop(name, None)

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

    # ------------------------------------------------------------------ converge ---
    def converge(self, state: RuntimeState, target: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.events.clear()
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
        # `workflow_templates`, surfaced via /capabilities, not baked into the container):
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
                return

        for n in adopt_changed:
            rt = state.stacks[n]
            cn = rt.container or (self.cfg.models[n].engine.container.name or f"stackd-{n}")
            if self.runner.poll(cn) is None:  # already running — just re-tag
                rt.identity = want[n].identity
                rt.owner_profile = owner_of(n)
                self._emit(n, "reload", "adopt identity updated in place")
            else:
                self._start(state, want[n], owner_of(n), now)
        for n in spawn + reload_:
            self._start(state, want[n], owner_of(n), now)

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

            healthy = bool(rt.health_url) and self.runner.http_ok(rt.health_url)
            if rt.state == EngineState.warming:
                if healthy:
                    rt.state = EngineState.ready
                    rt.ready_at = now
                    rt.unhealthy_ticks = 0
                    self._emit(name, "ready")
                elif rt.started_at and now - rt.started_at > (rt.ready_timeout or 300.0):
                    rt.state = EngineState.error
                    self._schedule_restart(rt, now, "warmup timeout")
            elif rt.state == EngineState.ready:
                if healthy:
                    rt.unhealthy_ticks = 0
                else:
                    rt.unhealthy_ticks += 1
                    if rt.unhealthy_ticks >= _UNHEALTHY_LIMIT:
                        self._emit(name, "crash", "health flatlined")
                        self._schedule_restart(rt, now, "unhealthy")
                        rt.state = EngineState.error

        return list(self.events)

    # ----------------------------------------------------------------- restart ---
    def _backoff(self, restarts: int) -> float:
        return min(self.backoff_base_s * (2 ** max(0, restarts - 1)), self.backoff_cap_s)

    def _schedule_restart(self, rt: StackRuntime, now: float, why: str) -> None:
        if rt.restarts >= self.max_restarts:
            rt.backoff_until = None
            self._emit(rt.name, "give-up", f"{why}; {rt.restarts} restarts exhausted")
            return
        rt.backoff_until = now + self._backoff(rt.restarts + 1)
        self._emit(rt.name, "restart", f"{why}; retry in {rt.backoff_until - now:.0f}s")

    def _handle_exit(self, rt: StackRuntime, code: int, now: float) -> None:
        self._emit(rt.name, "crash", f"exit {code}")
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
        rt.restarts += 1
        if rt.health_url:
            getattr(self.runner, "reset_health", lambda *_: None)(rt.health_url)
        self._emit(rt.name, "restart", f"respawn #{rt.restarts}")
