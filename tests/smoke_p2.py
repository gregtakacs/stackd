"""Dependency-free P2 checks with the FakeRunner: `python3 tests/smoke_p2.py`.

Exercises converge / warmup / crash-restart-backoff / idle self-evict / reactive
priority-gated entry / stand-in routing — no GPUs, no docker, no pytest.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

from stackd.config import load_config  # noqa: E402
from stackd.engines.base import EngineState  # noqa: E402
from stackd.engines.registry import adapter_for  # noqa: E402
from stackd.manager import Manager, Outranked  # noqa: E402
from stackd.reconciler import Reconciler  # noqa: E402
from stackd.runner import FakeRunner  # noqa: E402

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"

CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    CHECKS.append((name, bool(cond)))


def mgr(runner=None, **kw) -> Manager:
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    return Manager(CFG, tmp, runner or FakeRunner(ready_after=2), **kw)


def ready_all(m: Manager, now_start: float = 0.0) -> float:
    now = now_start
    for _ in range(8):
        now += 5
        m.tick(now=now)
        stacks_ok = all(s.state == EngineState.ready for s in m.state.stacks.values())
        img_ok = m.state.image is None or m.state.image.state == EngineState.ready
        if stacks_ok and img_ok:
            break
    return now


def t_converge_chat() -> None:
    m = mgr()
    m.use("chat", now=0)
    check("chat spawns 2 LLM stacks", set(m.state.stacks) == {
        "chat", "code-autocomplete"})
    check("all warming after converge",
          all(s.state == EngineState.warming for s in m.state.stacks.values()))
    check("elastic image tier placed dev-turbo on the RTX",
          m.state.image is not None and m.state.image.active_model == "flux2-dev-turbo"
          and m.state.image.device == "cuda0")
    ready_all(m)
    check("all LLM stacks ready after ticks",
          all(s.state == EngineState.ready for s in m.state.stacks.values()))
    check("image slot ready after ticks", m.state.image.state == EngineState.ready)
    check("chat got a process port", m.state.stacks["chat"].port is not None)
    check("image slot has a container name", bool(m.state.image.container))


def t_switch_to_coding_is_a_delta() -> None:
    m = mgr()
    m.use("chat", now=0)
    ready_all(m)
    ac_handle = m.state.stacks["code-autocomplete"].handle
    m.use("coding", now=100)
    check("coding: chat torn down", "chat" not in m.state.stacks)
    check("coding: image tier rebuilt as klein on the iGPU",
          m.state.image is not None and m.state.image.active_model == "flux2-klein"
          and m.state.image.device == "igpu0")
    check("coding: flash spawned", "coding" in m.state.stacks)
    check("coding: autocomplete untouched (same handle)",
          m.state.stacks["code-autocomplete"].handle == ac_handle)
    check("coding: autocomplete now owned by coding (listed there)",
          m.state.stacks["code-autocomplete"].owner_profile == "coding")


def t_cuda_teardown_uses_kill_and_probe() -> None:
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("chat", now=0)
    ready_all(m)
    chat_handle = m.state.stacks["chat"].handle
    probes_before = fake.probe_calls
    m.use("coding", now=100)
    check("cuda0 engine container stopped", fake.containers[chat_handle].running is False)
    check("nvidia-smi probed on cuda teardown", fake.probe_calls > probes_before)


def t_crash_restart_with_backoff() -> None:
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("chat", now=0)
    ready_all(m)
    h = m.state.stacks["chat"].handle
    fake.crash(h)
    ev = m.tick(now=50)
    rt = m.state.stacks["chat"]
    check("crash detected", any(e.action == "crash" for e in ev))
    check("backoff scheduled", rt.backoff_until is not None and rt.backoff_until > 50)
    check("still in error before backoff elapses", rt.state == EngineState.error)
    m.tick(now=rt.backoff_until + 1)
    rt = m.state.stacks["chat"]
    check("respawned after backoff", rt.restarts == 1 and rt.state in (
        EngineState.warming, EngineState.ready))


def t_idle_self_evict() -> None:
    m = mgr(FakeRunner(ready_after=1), min_residency_s=60)
    m.use("chat", now=0)
    ready_all(m)
    m.use("coding", now=1000)
    ready_all(m, now_start=1000)
    m.tick(now=1000 + 30 * 60)  # 30 min < 45m idle_evict -> stays
    check("coding still active at 30m idle", m.state.active_profile == "coding")
    m.tick(now=1000 + 46 * 60)  # > 45m -> self-evict
    check("coding self-evicted to chat at 46m", m.state.active_profile == "chat")
    check("chat reconverged", "chat" in m.state.stacks)


def t_pin_blocks_self_evict() -> None:
    m = mgr(FakeRunner(ready_after=1), min_residency_s=1)
    m.use("chat", now=0)
    m.use("coding", now=10)
    m.pin()
    m.tick(now=10 + 60 * 60)
    check("pinned coding survives 60m idle", m.state.active_profile == "coding")


def t_idle_evict_counts_from_load_not_activation() -> None:
    # a slow warm-up must not eat the idle window — the countdown starts when the
    # profile's stacks go ready, not when it was activated.
    m = mgr(FakeRunner(ready_after=2), min_residency_s=1)
    m.use("chat", now=0)
    ready_all(m)
    m.use("coding", now=1000)
    check("countdown not started while coding warms", m._idle_evict_at(1000) is None)
    ready_all(m, now_start=1000)
    loaded = max(rt.ready_at for rt in m.state.stacks.values())
    check("countdown anchored to load time",
          m._idle_evict_at(loaded) == loaded + 45 * 60)
    m.tick(now=loaded + 44 * 60)
    check("stays at 44m since load", m.state.active_profile == "coding")
    m.tick(now=loaded + 46 * 60)
    check("evicts at 46m since load (not since activation)",
          m.state.active_profile == "chat")


def t_no_timer_profile_autopins_on_entry() -> None:
    m = mgr(FakeRunner(ready_after=1), switch_cooldown_s=0)
    m.use("chat", now=0)
    check("floor profile is never auto-pinned", not m.state.pinned)
    m.cfg.profiles["coding"].idle_evict = None      # a profile with no timer
    m.use("coding", now=10)
    check("no-timer profile auto-pins on entry", m.state.pinned)
    m.tick(now=10 + 6 * 60 * 60)
    check("auto-pinned profile is not idle-evicted", m.state.active_profile == "coding")
    m.evict(now=20 + 6 * 60 * 60)
    check("evicting back to the floor clears the pin", not m.state.pinned)


def t_reactive_entry_and_standin() -> None:
    m = mgr(FakeRunner(ready_after=1), switch_cooldown_s=0)
    m.use("chat", now=0)
    ready_all(m)

    r = m.route("assistant-coder", now=100)  # coding-owned, outranks chat
    check("agentic request enters coding", m.state.active_profile == "coding")
    check("route reports warming", r.status == "warming" and r.profile == "coding")

    ready_all(m, now_start=100)
    r2 = m.route("assistant", now=200)  # chat-owned, coding is active + masks assistant*
    check("chat request served by stand-in (mask)", r2.status == "ok")
    check("stand-in did NOT switch down", m.state.active_profile == "coding")
    check("stand-in endpoint is coding",
          r2.endpoint == m.state.stacks["coding"].endpoint)

    r3 = m.route("coding-autocomplete", now=210)  # keep@igpu0 survivor
    check("autocomplete served by kept igpu0 stack", r3.status == "ok")

    # an chat reasoning name answered by the vLLM stand-in -> vLLM dialect
    r_nt = m.route("assistant-nothink", now=212)
    check("stand-in: reasoning:off -> vLLM enable_thinking=false",
          r_nt.preset.get("chat_template_kwargs", {}).get("enable_thinking") is False
          and "reasoning_budget" not in r_nt.preset)
    r_lo = m.route("assistant-low", now=213)
    check("stand-in: effort kept, llama.cpp budget dropped for vLLM",
          r_lo.preset.get("reasoning_effort") == "low" and "reasoning_budget" not in r_lo.preset)


def t_preset_translation() -> None:
    from stackd.manager import _translate_preset
    check("llamacpp reasoning:off",
          _translate_preset({"reasoning": "off"}, "llamacpp-cuda")
          == {"reasoning_effort": "none", "reasoning_budget": 0})
    check("llamacpp effort+budget",
          _translate_preset({"reasoning": {"effort": "low", "budget": 4096}}, "llamacpp-cuda")
          == {"reasoning_effort": "low", "reasoning_budget": 4096})
    check("llamacpp bare-string effort",
          _translate_preset({"reasoning": "xhigh"}, "llamacpp-vulkan")
          == {"reasoning_effort": "xhigh"})
    check("vllm reasoning:off -> enable_thinking:false",
          _translate_preset({"reasoning": "off"}, "vllm-cuda")
          == {"chat_template_kwargs": {"enable_thinking": False}})
    check("vllm effort kept, budget dropped",
          _translate_preset({"reasoning": {"effort": "medium", "budget": 8192}}, "vllm-cuda")
          == {"reasoning_effort": "medium"})
    check("no reasoning key -> passthrough",
          _translate_preset({"temperature": 0.2}, "vllm-cuda") == {"temperature": 0.2})
    check("raw reasoning_effort -> passthrough (no double-translate)",
          _translate_preset({"reasoning_effort": "high"}, "vllm-cuda")
          == {"reasoning_effort": "high"})
    check("vllm off merges into existing chat_template_kwargs",
          _translate_preset({"reasoning": "off", "chat_template_kwargs": {"foo": 1}}, "vllm-cuda")
          == {"chat_template_kwargs": {"foo": 1, "enable_thinking": False}})


def t_cuda_drain_barrier() -> None:
    """Switching chat->coding tears down the 27B on cuda0 then allocates
    coding on top. The drain barrier waits for free VRAM to settle first,
    and aborts the spawn if nvidia-smi stops responding (card wedging)."""
    # healthy: free VRAM ramps as the scrubber releases, then plateaus -> proceed
    fake = FakeRunner(ready_after=1)
    fake.gpu_free_values = [6000, 20000, 55000, 60000, 60000, 60000]
    m = mgr(fake)
    m.use("chat", now=0)
    ready_all(m)
    ev = m.use("coding", now=100)
    check("drain ran on the cuda switch", any(e.action == "drain" for e in ev))
    check("coding spawned after drain settled", "coding" in m.state.stacks)
    check("27B torn down", "chat" not in m.state.stacks)

    # wedge: nvidia-smi stops answering -> abort the spawn, don't pile on
    fake2 = FakeRunner(ready_after=1)
    m2 = mgr(fake2)
    m2.use("chat", now=0)
    ready_all(m2)
    fake2.gpu_free_values = [8000, None, None, None]
    ev2 = m2.use("coding", now=100)
    check("wedge -> degraded event", any(e.action == "degraded" for e in ev2))
    check("wedge -> coding NOT spawned on the stuck card", "coding" not in m2.state.stacks)


def t_shutdown_engines_tears_down_everything() -> None:
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("chat", now=0)
    ready_all(m)
    had = set(m.state.stacks)
    check("engines + image up before shutdown", len(had) >= 1 and m.state.image is not None)
    cns = [rt.handle or rt.container for rt in m.state.stacks.values()]
    cns.append(m.state.image.handle or m.state.image.container)
    removed = m.shutdown_engines(now=500)
    check("shutdown_engines reports every stack + the image tier",
          set(n for n in removed if not n.startswith("image:")) == had
          and any(n.startswith("image:") for n in removed))
    check("state is empty afterwards", not m.state.stacks and m.state.image is None)
    check("containers were actually removed via the runner",
          all(cn in fake.removed for cn in cns))
    # idempotent — a second call on an already-clean state is a no-op
    check("second shutdown_engines is a no-op", m.shutdown_engines(now=600) == [])


def t_tick_sweeps_stray_stacks() -> None:
    """A stack left in state that the active profile doesn't want (e.g. a switch
    whose converge threw mid-teardown) is stopped by the next tick, not
    crash-restarted forever."""
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("chat", now=0)
    ready_all(m)
    m.use("coding", now=100)
    ready_all(m, now_start=100)
    # simulate a half-done switch back: active=chat but coding lingers
    from stackd.solver import solve
    m.state.active_profile = "chat"
    check("coding lingering in state", "coding" in m.state.stacks)
    m.tick(now=200)
    check("tick swept the stray coding", "coding" not in m.state.stacks)
    check("chat reconverged after sweep",
          {"chat", "code-autocomplete"} <= set(m.state.stacks))


def t_image_tier_is_stackd_created() -> None:
    """The elastic image tier's per-backend containers are stackd-created (no
    adopt) — image tag, GPU wiring, /dev/kfd, shm, shared scratch all flow through
    the synthesized ModelSpec + the ComfyUI adapter."""
    cfg = load_config(CFG)
    rec = Reconciler(cfg, FakeRunner())
    tier = cfg.media["image"]

    synth = rec._synth_image_model(tier, "cuda", "flux2-dev-turbo", ["generate", "edit"])
    check("cuda container not adopt", synth.engine.container.adopt is False)
    spec = adapter_for(synth, cfg.devices["cuda0"]).launch_spec(rec._lc("cuda0", 8188))
    check("cuda: stock ComfyUI image", spec.image and "yanwk/comfyui-boot" in spec.image)
    check("cuda: generic config ships no reverse-proxy labels", spec.labels == {})
    check("cuda: stock image has no build recipe", synth.engine.container.build is None)

    synthv = rec._synth_image_model(tier, "vulkan", "flux2-klein", ["generate", "edit"])
    specv = adapter_for(synthv, cfg.devices["igpu0"]).launch_spec(rec._lc("igpu0", 8188))
    check("vulkan: local comfyui-rocm image", specv.image and "comfyui-rocm" in specv.image)
    check("vulkan: /dev/kfd device path", "/dev/kfd" in specv.device_paths)
    check("vulkan: renderD128 from the vulkan device_profile",
          "/dev/dri/renderD128" in specv.device_paths)
    check("vulkan: shm_size 8g override", specv.shm_size == "8g")
    check("vulkan: seccomp from the vulkan device_profile",
          "seccomp:unconfined" in specv.security_opt)
    check("vulkan: build recipe present",
          synthv.engine.container.build is not None
          and synthv.engine.container.build.dockerfile == "Dockerfile.igpu")

    def _scratch(s):
        return sorted(x.host_path for x in s.mounts if "/scratch/" in x.host_path)
    check("cuda + vulkan share scratch host paths", _scratch(spec) == _scratch(specv))

    tier.containers["cuda"].labels = {"traefik.enable": "true"}
    lspec = adapter_for(rec._synth_image_model(tier, "cuda", "flux2-klein", []),
                        cfg.devices["cuda0"]).launch_spec(rec._lc("cuda0", 8188))
    check("container.labels pass through to the LaunchSpec",
          lspec.labels.get("traefik.enable") == "true")
    tier.containers["cuda"].labels = {}


def t_image_identity_covers_container_spec() -> None:
    """A ${VAR}/.env edit landing in the image tier's container block changes the
    slot identity -> the next converge rebuilds the ComfyUI even with no LLM
    change, no `docker rm -f`."""
    cfg = load_config(CFG)
    rec = Reconciler(cfg, FakeRunner())
    tier = cfg.media["image"]
    base = rec._image_identity(tier, "cuda", "flux2-dev-turbo", "cuda0")
    tier.containers["cuda"].env["RELOAD_TEST"] = "1"
    check("container env change -> new image identity",
          rec._image_identity(tier, "cuda", "flux2-dev-turbo", "cuda0") != base)

    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("chat", now=0)
    ready_all(m)
    cn = m.state.image.container
    m.cfg.media["image"].containers["cuda"].env["RELOAD_TEST"] = "1"   # simulate a reload edit
    m.rec.converge(m.state, "chat", now=200)
    check("image container removed on spec change", cn in fake.removed)
    check("image slot respawned fresh",
          m.state.image is not None and m.state.image.container == cn
          and m.state.image.state != EngineState.ready)


def t_reload_config() -> None:
    """Manager.reload_config swaps in a re-read Config; a broken edit is rejected
    and leaves the live config intact."""
    import shutil
    src = pathlib.Path(CFG)
    d = pathlib.Path(tempfile.mkdtemp()) / "config"
    shutil.copytree(src, d)
    m = Manager(d, d.parent / "state.json", FakeRunner(ready_after=1))
    before = m.cfg.pools["host_unified"].host_reserve_gib

    pf = d / "pools.yaml"
    pf.write_text(pf.read_text().replace("host_reserve_gib: ${HOST_RESERVE_GIB}",
                                         "host_reserve_gib: 12"))
    changed = m.reload_config()
    check("reload picks up the edit", m.cfg.pools["host_unified"].host_reserve_gib == 12
          and m.cfg.pools["host_unified"].host_reserve_gib != before)
    check("reload reports the change", any("pool host_unified changed" in c for c in changed))
    check("rec.cfg is the new cfg", m.rec.cfg is m.cfg)

    pf.write_text("pools:\n  cuda_vram:\n    total_gib: not-a-number\n")   # broken
    try:
        m.reload_config()
        check("broken reload rejected", False)
    except Exception:  # noqa: BLE001
        check("broken reload rejected", True)
    check("live config untouched after a bad reload",
          m.cfg.pools["host_unified"].host_reserve_gib == 12)


def t_image_tier_scheduler() -> None:
    """The elastic image tier: sticky within a profile, rebuilt from headroom on a
    switch, torn down before the LLMs spawn, and never at an LLM's expense."""
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("chat", now=0)
    ready_all(m)
    slot0 = m.state.image
    check("chat -> dev-turbo on cuda0", slot0.active_model == "flux2-dev-turbo"
          and slot0.device == "cuda0")
    h0 = slot0.handle

    # sticky: a no-op converge on the same profile leaves the slot alone
    m.rec.converge(m.state, "chat", now=50)
    check("no-op converge doesn't touch the image slot", m.state.image.handle == h0)

    # switch: image torn down BEFORE coding spawns, then rebuilt from the
    # new headroom (klein on the iGPU — vLLM claims the RTX)
    ev = m.use("coding", now=100)
    order = [e.action for e in ev if e.stack.startswith("image:") or e.stack == "coding"]
    check("image teardown emitted on the switch", "teardown" in order)
    check("image rebuilt as klein on the iGPU",
          m.state.image.active_model == "flux2-klein" and m.state.image.device == "igpu0")
    ready_all(m, now_start=100)
    check("coding LLM + image both ready",
          m.state.stacks["coding"].state == EngineState.ready
          and m.state.image.state == EngineState.ready)

    # crash: tick clears the dead slot and the same tick refills it
    m.state.image.handle = None
    m.state.image.state = EngineState.error
    m.tick(now=500)
    check("tick respawned the crashed image slot",
          m.state.image is not None and m.state.image.handle is not None)

    # boot_reset drops a handle-less slot; the next tick brings it back
    m.state.image.handle = None
    dropped = m.state.boot_reset()
    check("boot_reset drops the dead image slot",
          m.state.image is None and any(d.startswith("image:") for d in dropped))
    m.tick(now=600)
    check("tick refills the slot after boot_reset", m.state.image is not None)


def t_image_tier_no_headroom() -> None:
    """A profile that fills every device leaves no image tier — and the MCP path
    says why instead of pointing at a nonexistent engine."""
    m = mgr(FakeRunner(ready_after=1))
    # coding: vLLM claims ~90/96 on cuda0; shrink the iGPU budget so klein won't fit
    m.cfg.media["image"].prefer[1].footprint_gib["vulkan"] = 999.0
    m.use("coding", now=0)
    ready_all(m)
    check("no image slot when nothing fits", m.state.image is None)
    check("capabilities.image is None", m.capabilities()["image"] is None)
    ep, why = m.comfyui_target()
    check("comfyui_target explains the lack of headroom",
          ep is None and "headroom" in why)


def t_image_swap() -> None:
    """Manager.set_image — explicit model swap, capability request, auto-downgrade
    with a surfaced note, and a 409 when nothing capable fits."""
    m = mgr(FakeRunner(ready_after=1))
    m.use("chat", now=0)
    ready_all(m)
    check("resident starts as dev-turbo", m.state.image.active_model == "flux2-dev-turbo")

    h0 = m.state.image.handle
    r = m.set_image(model="flux2-klein", now=10)
    check("explicit swap ok", r["ok"] and r["active_model"] == "flux2-klein")
    check("swap marks the slot user-pinned", m.state.image.pinned_by == "user")
    check("same-container swap is in-place (no bounce)",
          r.get("in_place") is True and m.state.image.handle == h0)
    ready_all(m, now_start=10)

    r = m.set_image(model="flux2-klein", now=20)
    check("swap to the resident model is a no-op", r["ok"] and "already resident" in (r.get("note") or ""))

    # a capability the resident model already covers -> no-op
    r = m.set_image(need_capability="edit", now=25)
    check("capability already covered -> no-op", r["ok"] and "already provides" in (r.get("note") or ""))

    # downgrade: resident is klein; ask for dev-turbo but make it not fit
    m2 = mgr(FakeRunner(ready_after=1))
    m2.use("chat", now=0)
    ready_all(m2)
    m2.set_image(model="flux2-klein", now=5)          # resident := klein
    ready_all(m2, now_start=5)
    m2.cfg.media["image"].prefer[0].footprint_gib["cuda"] = 999.0
    r = m2.set_image(model="flux2-dev-turbo", now=30)
    check("downgrade: served a smaller capable model",
          r["ok"] and r["active_model"] == "flux2-klein"
          and r["downgraded_from"] == "flux2-dev-turbo")
    check("downgrade: note explains it", "did not fit" in (r.get("note") or ""))

    # 409: nothing capable fits at all
    m2.cfg.media["image"].prefer[1].footprint_gib["cuda"] = 999.0
    m2.cfg.media["image"].prefer[1].footprint_gib["vulkan"] = 999.0
    r = m2.set_image(model="flux2-dev-turbo", now=40)
    check("no fit -> not ok + headroom reported",
          r["ok"] is False and "headroom" in r and "fit" in r["error"])

    r = m2.set_image(need_capability="teleport", now=45)
    check("unknown capability -> not ok", r["ok"] is False)


def t_image_host_ram_guard() -> None:
    """A candidate whose device-VRAM fit is fine can still be refused when its
    REAL host-RAM cost would exceed the shared host_unified budget -- the gap
    flagged and closed 2026-09-04 after flux2-dev-turbo measured ~121G peak
    host RAM on igpu0 alone, far above what its much smaller footprint_gib
    (VRAM/GTT only) figure alone would suggest is safe."""
    from stackd.solver import headroom, host_ram_headroom

    m = mgr(FakeRunner(ready_after=1))
    m.use("chat", now=0)
    ready_all(m)
    check("resident starts as dev-turbo", m.state.image.active_model == "flux2-dev-turbo")

    tier = m.cfg.media["image"]
    ld = tier.prefer[1]
    check("fixture is flux2-klein", ld.active_model == "flux2-klein")
    hr = headroom(m.cfg, "chat", m.catalog, reserve_gib=tier.margin_gib)
    host_avail = host_ram_headroom(m.cfg, "chat", m.catalog, reserve_gib=tier.margin_gib)
    check("host RAM headroom is finite and positive under chat", 0 < host_avail < 128)

    # klein's device-VRAM fit is fine on EITHER backend -- but an unrealistic
    # host_ram_gib on every backend it supports means no candidate passes the
    # host-RAM check, even though the device-VRAM check alone would accept it.
    ld.host_ram_gib = {"cuda": 500.0, "vulkan": 500.0}
    pick = m.rec._pick_image(tier, hr, want_model="flux2-klein", host_ram_avail=host_avail)
    check("host-RAM overage on every backend -> no pick", pick is None)

    # a modest, genuinely-affordable host_ram_gib is accepted normally
    ld.host_ram_gib = {"cuda": 5, "vulkan": 5}
    pick = m.rec._pick_image(tier, hr, want_model="flux2-klein", host_ram_avail=host_avail)
    check("affordable host_ram_gib on both backends -> picked",
          pick is not None and pick[2].active_model == "flux2-klein")

    # only ONE backend affordable -> swap_image() still succeeds, correctly
    # using the cheaper backend rather than refusing outright
    ld.host_ram_gib = {"cuda": 500.0, "vulkan": 5}
    r = m.set_image(model="flux2-klein", now=10)
    check("swap succeeds via the one affordable backend",
          r["ok"] and r["active_model"] == "flux2-klein" and r.get("backend") == "vulkan")


def t_image_unsafe_bench_bypass() -> None:
    """`backend`/`unsafe` on swap_image() — the deliberate-measurement escape
    hatch added 2026-09-04 after flux2-dev-turbo's OWN conservative
    host_ram_gib estimate made it impossible to ever bench a corrected number
    for it (every real-available-memory reading on the box, even fully idle,
    was below the stale estimate). `unsafe` must require `backend`; a safe
    `backend` hint alone still goes through full fit checks; `unsafe=True`
    bypasses them and says so in the result."""
    m = mgr(FakeRunner(ready_after=1))
    m.use("chat", now=0)
    ready_all(m)

    tier = m.cfg.media["image"]
    ld = next(l for l in tier.prefer if l.active_model == "flux2-dev-turbo")
    ld.host_ram_gib = {"cuda": 5000.0, "vulkan": 5000.0}   # impossible on any real box
    # swap off dev-turbo first so the "already resident" fast path (which
    # matches on model name alone, not backend) can't short-circuit the
    # backend-hint checks below before they even run.
    m.set_image(model="flux2-klein", now=4)
    ready_all(m, now_start=4)

    r = m.set_image(model="flux2-dev-turbo", unsafe=True, now=5)
    check("unsafe without backend is rejected", r["ok"] is False and "backend" in r["error"])

    r = m.set_image(model="flux2-dev-turbo", backend="cuda", now=6)
    check("a safe backend hint still enforces the host-RAM check",
          r["ok"] is False or r.get("active_model") != "flux2-dev-turbo")

    r = m.set_image(model="flux2-dev-turbo", backend="cuda", unsafe=True, now=7)
    check("unsafe+backend bypasses the impossible host-RAM estimate",
          r["ok"] and r["active_model"] == "flux2-dev-turbo" and r.get("backend") == "cuda")
    check("result flags the bypass", r.get("unsafe") is True)
    ready_all(m, now_start=7)
    check("model actually resident after the unsafe load",
          m.state.image is not None and m.state.image.active_model == "flux2-dev-turbo"
          and m.state.image.device == "cuda0")

    # re-requesting the same model+backend while already resident there is a no-op, not a re-spawn
    r2 = m.set_image(model="flux2-dev-turbo", backend="cuda", unsafe=True, now=8)
    check("repeat unsafe request on the same backend is a no-op",
          r2["ok"] and "already resident" in (r2.get("note") or ""))


def t_image_none_survives_pin() -> None:
    """`image: none` means "don't AUTO-fill" — it must not tear down a slot a
    human/bench explicitly pinned there moments ago. Regression for the
    2026-09-04 bench-image thrash loop: `stackctl image bench` spawned a
    model for measurement, and the very next reconciler tick tore it right
    back down because the active profile had image: none, aborting every
    bench attempt run on that profile."""
    from stackd.config.models import ImageSupport

    m = mgr(FakeRunner(ready_after=1))
    m.use("chat", now=0)
    ready_all(m)
    m.set_image(model="flux2-klein", now=5)
    ready_all(m, now_start=5)
    check("klein resident before flipping to image:none", m.state.image is not None
          and m.state.image.active_model == "flux2-klein")

    m.cfg.profiles["chat"].image = ImageSupport.none
    m.tick(now=50)
    check("user-pinned slot survives a tick under image:none",
          m.state.image is not None and m.state.image.active_model == "flux2-klein")

    # an AUTO-picked slot (not user-pinned) IS still torn down under image:none
    m.state.image.pinned_by = "auto"
    m.tick(now=60)
    check("an auto-picked slot IS torn down under image:none", m.state.image is None)

    m.cfg.profiles["chat"].image = ImageSupport.cold   # restore for later checks


def t_reactive_entry_refused_when_not_outranking() -> None:
    m = mgr(FakeRunner(ready_after=1))
    m.use("coding", now=0)
    ready_all(m)
    try:
        m.use("chat", manual=False, now=100)
        check("reactive downgrade refused", False)
    except Outranked:
        check("reactive downgrade refused", True)


def main() -> int:
    for fn in [
        t_converge_chat,
        t_switch_to_coding_is_a_delta,
        t_cuda_teardown_uses_kill_and_probe,
        t_crash_restart_with_backoff,
        t_idle_self_evict,
        t_pin_blocks_self_evict,
        t_idle_evict_counts_from_load_not_activation,
        t_no_timer_profile_autopins_on_entry,
        t_shutdown_engines_tears_down_everything,
        t_reactive_entry_and_standin,
        t_preset_translation,
        t_cuda_drain_barrier,
        t_tick_sweeps_stray_stacks,
        t_image_tier_is_stackd_created,
        t_image_identity_covers_container_spec,
        t_image_tier_scheduler,
        t_image_tier_no_headroom,
        t_image_swap,
        t_image_host_ram_guard,
        t_image_unsafe_bench_bypass,
        t_image_none_survives_pin,
        t_reload_config,
        t_reactive_entry_refused_when_not_outranking,
    ]:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            CHECKS.append((f"{fn.__name__} raised {e!r}", False))

    ok = True
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
