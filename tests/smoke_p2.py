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


def t_giveup_marks_dead_and_blocks_reactive_entry() -> None:
    """2026-09-12 incident: a broken NVIDIA driver crash-looped `coding` until it
    gave up. Manager.tick() should evict back to the default profile AND mark
    it dead so route() refuses to silently re-run the same doomed spawn cycle on
    the next request; only a manual `use()` (stackctl use / the dashboard Retry
    button) should clear it."""
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("coding", now=0)
    ready_all(m)
    reason = "nvidia-container-cli: initialization error: nvml error: driver/library version mismatch"
    now = 0.0
    for _ in range(6):  # 5 respawns + the crash that exhausts the budget
        if "coding" not in m.state.stacks:
            break
        h = m.state.stacks["coding"].handle
        fake.crash(h, exit_code=128, error=reason)
        now += 1
        m.tick(now=now)
        rt = m.state.stacks.get("coding")
        if rt is not None and rt.backoff_until is not None:
            now = rt.backoff_until + 1
            m.tick(now=now)

    check("coding marked dead", "coding" in m.state.dead)
    check("dead reason captures the real failure", reason in m.state.dead.get("coding").reason)
    check("evicted back to the default profile", m.state.active_profile == "chat")

    rr = m.route("assistant-coder", now=now + 1)
    check("route refuses to re-enter a dead profile", rr.status == "dead")
    check("dead route note names the reason", reason in rr.note)
    check("dead route does not spawn a fresh coding stack",
          "coding" not in m.state.stacks)

    m.use("coding", manual=True, now=now + 2)
    check("manual use clears the dead mark", "coding" not in m.state.dead)


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


def t_catalog_advertises_answering_ctx() -> None:
    """The context advertised for a covered name must be the ANSWERING stack's
    real window — not the covered model's (a stand-in with a smaller ctx would
    overpromise, one with a bigger ctx would hide usable room)."""
    import copy

    from stackd.manager import model_context_length

    m = mgr(FakeRunner(ready_after=1), switch_cooldown_s=0)
    m.use("chat", now=0)
    ready_all(m)
    cat = {e["id"]: e for e in m.models_catalog()}
    check("native: assistant advertises its own ctx",
          cat["assistant"]["context_length"] == 262144
          and cat["assistant"]["context_provider"] == "chat"
          and cat["assistant"]["native_context_length"] == 262144)

    # enter coding (assistant* now stands in) and give it a SMALLER window
    m.route("assistant-coder", now=100)
    ready_all(m, now_start=100)
    ce = m.cfg.models["coding"].engine
    ce.container.cmd_extra[ce.container.cmd_extra.index("--max-model-len") + 1] = "131072"
    cat = {e["id"]: e for e in m.models_catalog()}
    check("covered name advertises the stand-in's ctx, not its own",
          cat["assistant"]["context_length"] == 131072
          and cat["assistant"]["context_provider"] == "coding"
          and cat["assistant"]["native_context_length"] == 262144
          and cat["assistant"]["standin"] is True)
    check("stand-in's own names advertise its ctx",
          cat["assistant-coder"]["context_length"] == 131072
          and cat["assistant-coder"]["context_provider"] == "coding")

    # back home -> the native window returns
    m.use("chat", now=200)
    cat = {e["id"]: e for e in m.models_catalog()}
    check("back on the home profile, ctx reverts to the native model's",
          cat["assistant"]["context_length"] == 262144
          and cat["assistant"]["context_provider"] == "chat")

    # manual_only profile must NOT win the catalog home: reactive entry (route())
    # filters manual_only out, so the advertised ctx has to come from the profile
    # a request would actually load (mirrors coding-long vs coding).
    from stackd.config.models import ProfileSpec, ServedEntry
    m3 = mgr(FakeRunner(ready_after=1), switch_cooldown_s=0)
    xl = copy.deepcopy(m3.cfg.models["coding"])
    xl.name = "coding-xl"
    xl.engine.container.cmd_extra[xl.engine.container.cmd_extra.index("--max-model-len") + 1] = "524288"
    xl.serves = [ServedEntry(api_name="assistant-coder")]
    m3.cfg.models["coding-xl"] = xl
    m3.cfg.profiles["coding-xl"] = ProfileSpec(profile="coding-xl", priority=999,
                                               manual_only=True, models=["coding-xl"])
    m3.state.active_profile = "chat"
    cat = {e["id"]: e for e in m3.models_catalog()}
    check("manual_only profile demoted: coder name advertises `coding` (262144), not manual coding-xl",
          cat["assistant-coder"]["context_length"] == 262144
          and cat["assistant-coder"]["context_provider"] == "coding")
    m3.state.active_profile = "coding-xl"
    cat = {e["id"]: e for e in m3.models_catalog()}
    check("...but when the manual_only profile IS active, it wins",
          cat["assistant-coder"]["context_length"] == 524288
          and cat["assistant-coder"]["context_provider"] == "coding-xl")

    # model_context_length resolution: params, then extra_args, then cmd_extra
    check("helper: llama.cpp params ctx",
          model_context_length(m.cfg, "chat") == 262144)
    c = copy.deepcopy(m.cfg.models["chat"])
    c.engine.params["extra_args"] = ["-c", "4096"]
    check("helper: llama.cpp extra_args -c overrides params ctx",
          model_context_length(_cfg_with(m, "chat", c), "chat") == 4096)
    c.engine.container.cmd_extra.extend(["--ctx-size", "8192"])
    check("helper: cmd_extra ctx-size wins over everything",
          model_context_length(_cfg_with(m, "chat", c), "chat") == 8192)
    check("helper: vllm max_model_len via params OR cmd_extra",
          model_context_length(m.cfg, "coding") == 131072)

    # vision: explicit params.vision is authoritative; heuristic is the fallback
    from stackd.manager import model_supports_vision

    check("vision heuristic: llama.cpp --mmproj in extra_args -> True",
          model_supports_vision(m.cfg, "chat") is True)
    check("vision heuristic: vllm with no flag -> False",
          model_supports_vision(m.cfg, "coding") is False)
    cv = copy.deepcopy(m.cfg.models["coding"])
    cv.engine.params["vision"] = True
    check("vision: explicit params.vision=true overrides the (blind) vllm heuristic",
          model_supports_vision(_cfg_with(m, "coding", cv), "coding") is True)
    ch = copy.deepcopy(m.cfg.models["chat"])
    ch.engine.params["vision"] = False
    check("vision: explicit params.vision=false force-hides despite --mmproj",
          model_supports_vision(_cfg_with(m, "chat", ch), "chat") is False)


def _cfg_with(m, name, model_spec):
    """A shallow clone of m.cfg with one model swapped in (for helper tests)."""
    import dataclasses

    return dataclasses.replace(m.cfg, models={**m.cfg.models, name: model_spec})


def t_preset_translation() -> None:
    from stackd.manager import _translate_preset
    check("llamacpp reasoning:off -> enable_thinking:false (+ native knobs)",
          _translate_preset({"reasoning": "off"}, "llamacpp-cuda")
          == {"reasoning_effort": "none", "reasoning_budget": 0,
              "chat_template_kwargs": {"enable_thinking": False}})
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
    # sglang-pennyroyal shares vLLM's Qwen3 request dialect
    check("sglang reasoning:off -> enable_thinking:false",
          _translate_preset({"reasoning": "off"}, "sglang-pennyroyal")
          == {"chat_template_kwargs": {"enable_thinking": False}})
    check("sglang effort kept, per-request budget dropped",
          _translate_preset({"reasoning": {"effort": "medium", "budget": 8192}}, "sglang-pennyroyal")
          == {"reasoning_effort": "medium"})


def t_sglang_pennyroyal_adapter() -> None:
    """The sglang-pennyroyal adapter is vLLM-shaped: cmd_extra is the whole
    command, stackd only injects --served-model-name and the health URL."""
    from stackd.config._build import build
    from stackd.config.models import ModelSpec
    from stackd.runner import LaunchContext

    spec = build(ModelSpec, {
        "name": "coding-next",
        "engine": {
            "template": "sglang-pennyroyal",
            "model": "Qwen3.8-Flash-Next-NVFP4-hf",
            "params": {"port": 8001},
            "container": {
                "image": "sglang-pennyroyal:local",
                "security_opt": ["seccomp:unconfined"],   # NIXL io_uring on Docker 29
                "cmd_extra": ["--model-path", "/models/x",
                              "--served-model-name", "pennyroyal",  # stackd must drop this copy
                              "--port", "8001"],
            },
        },
        "budget": {"vram_gib": 93.8, "ram_gib": 40},
        "placement": {"devices": ["cuda0"]},
        "serves": [{"api_name": "TakacsAI-Coding-Next-med"}],
    })
    cfg = load_config(CFG)
    ls = adapter_for(spec, cfg.devices["cuda0"]).launch_spec(LaunchContext(port=8001))
    check("sglang: image passthrough", ls.image == "sglang-pennyroyal:local")
    check("sglang: stackd owns one --served-model-name == the stack name",
          ls.cmd.count("--served-model-name") == 1
          and ls.cmd[ls.cmd.index("--served-model-name") + 1] == "coding-next")
    check("sglang: cmd_extra flags preserved", "--model-path" in ls.cmd and "/models/x" in ls.cmd)
    check("sglang: health on :8001/health", (ls.health_url or "").endswith(":8001/health"))
    check("sglang: long cold-boot ready timeout", ls.ready_timeout_s >= 1800.0)
    check("container.security_opt merges into the LaunchSpec",
          "seccomp:unconfined" in ls.security_opt)


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


def t_same_device_spawn_is_staggered() -> None:
    """2026-09-12: code-autocomplete (igpu0/vulkan) and the image tier both cold-
    spawning onto igpu0 in the same converge is exactly the boot-time collision
    that exit-0'd code-autocomplete once (self-healed by the crash-restart backoff,
    but a known contention hazard shouldn't need a crash to recover from). converge()
    should stagger the second spawn onto a shared device instead of firing both in
    the same instant."""
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    # Manager() zeroes this for FakeRunner (tests shouldn't pay real sleeps) — put a
    # small real one back so the stagger path actually executes here.
    m.rec.same_device_spawn_stagger_s = 0.05
    m.use("coding", now=0)   # cold entry: code-autocomplete + the image tier both
                              # want igpu0 from a standing start
    check("code-autocomplete spawned", "code-autocomplete" in m.state.stacks)
    check("image tier landed on igpu0 (the shared device)",
          m.state.image is not None and m.state.image.device == "igpu0")
    check("stagger fired for the shared device",
          any(e.action == "stagger" for e in m.rec.events))


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

    # A catalog-only change — exactly what `stackctl bench --ingest` writes — has
    # to be reported. The curves are not config/*.yaml, and answering "no changes"
    # straight after an ingest reads as though the ingest had failed.
    import json as _json
    ck = lambda n, c, i=None: check(n if c else n + f"   [{i}]", c)
    key = "vllm-cuda|cuda0|ReloadTest|pNone"
    curve = d / "catalog" / "reloadtest.json"
    curve.write_text(_json.dumps({
        "key": key, "model": "ReloadTest", "engine": "vllm-cuda", "device": "cuda0",
        "source": "measured", "measured_at": "2026-01-01",
        "points": {"vram": [[262144.0, 42.0]], "ram": [[262144.0, 4.0]]},
        "notes": "reload probe", "timings": {}}))
    changed = m.reload_config()
    ck("a catalog-only edit is reported, not swallowed as 'no changes'",
       any("catalog refreshed" in c for c in changed), changed)
    ck("and the fresh curve is the live one",
       m.catalog.curves[key].estimate(262144)[0] == 42.0, m.catalog.curves[key])
    changed = m.reload_config()                       # nothing touched since
    ck("an untouched catalog is not re-reported",
       not any("catalog" in c for c in changed), changed)
    curve.unlink()

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
    m2.set_image(model="flux2-klein", backend="vulkan", now=5)          # resident := klein
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


def t_image_teardown_credit_and_refusals() -> None:
    """Two fixes from the 2026-09-08 flux2-dev-turbo investigation.

    (a) Teardown credit: a swap onto a DIFFERENT container destroys the resident
    image model first, so its host RAM must not count against the candidate —
    refusing over memory this very swap frees made "load the bigger model"
    unsable whenever any smaller model was resident. A SAME-backend swap only
    relabels the slot (ComfyUI keeps the old checkpoint cached) and earns none.
    (b) The refusal note must name the wall actually hit. The old one printed
    min(footprint_gib) — a CUDA *VRAM* number — for a vulkan *host-RAM* refusal."""
    from stackd.solver import headroom

    m = mgr(FakeRunner(ready_after=1))
    m.use("chat", now=0)
    ready_all(m)
    m.set_image(model="flux2-klein", backend="vulkan", now=5)
    ready_all(m, now_start=5)
    tier = m.cfg.media["image"]
    kl = next(l for l in tier.prefer if l.active_model == "flux2-klein")
    dt = next(l for l in tier.prefer if l.active_model == "flux2-dev-turbo")
    check("klein resident on vulkan", m.state.image.active_model == "flux2-klein"
          and m.state.image.backend == "vulkan")

    # dev-turbo reachable on either backend, needing 60 host RAM; only 30 free.
    dt.backends = ["cuda", "vulkan"]
    dt.footprint_gib = {"cuda": 8.0, "vulkan": 8.0}
    dt.host_ram_gib = {"cuda": 60.0, "vulkan": 60.0}
    kl.host_ram_gib = {"cuda": 42.0, "vulkan": 42.0}
    hr = headroom(m.cfg, "chat", m.catalog, reserve_gib=tier.margin_gib)
    reasons: list[str] = []
    p = m.rec._pick_image(tier, hr, want_model="flux2-dev-turbo", host_ram_avail=30.0,
                          reasons=reasons)
    check("no credit -> refused on both backends", p is None)
    check("refusal names the host-RAM wall, per backend",
          len(reasons) == 2 and all("host RAM 60 > 30.0 free" in r for r in reasons))

    p = m.rec._pick_image(tier, hr, want_model="flux2-dev-turbo", host_ram_avail=30.0,
                          freed_gib=42.0, freed_backend="vulkan")
    check("cross-backend credit makes the pick fit",
          p is not None and p[0] == "cuda0" and p[2].active_model == "flux2-dev-turbo")
    p = m.rec._pick_image(tier, hr, want_model="flux2-dev-turbo", host_ram_avail=30.0,
                          freed_gib=42.0, freed_backend="vulkan", want_backend="vulkan")
    check("same-backend swap earns NO credit (relabel frees nothing)", p is None)

    # a VRAM wall gets named with the device and its real headroom, too
    dt.footprint_gib, dt.host_ram_gib = {"cuda": 500.0}, {}
    reasons = []
    m.rec._pick_image(tier, hr, want_model="flux2-dev-turbo", host_ram_avail=1e9,
                      reasons=reasons)
    check("VRAM refusal names device + headroom",
          reasons and f"VRAM 500 > {hr['cuda0']:.1f} free on cuda0" in reasons[0])

    # end-to-end through set_image(): same numbers, credit applied internally
    m2 = mgr(FakeRunner(ready_after=1))
    m2.use("chat", now=0)
    ready_all(m2)
    m2.set_image(model="flux2-klein", backend="vulkan", now=5)
    ready_all(m2, now_start=5)
    m2.rec._real_host_ram_avail = lambda tier_, profile_: 30.0   # deterministic
    kl2 = next(l for l in m2.cfg.media["image"].prefer if l.active_model == "flux2-klein")
    dt2 = next(l for l in m2.cfg.media["image"].prefer if l.active_model == "flux2-dev-turbo")
    dt2.backends, dt2.footprint_gib, dt2.host_ram_gib = ["cuda"], {"cuda": 8.0}, {"cuda": 90.0}
    kl2.host_ram_gib = {"cuda": 65.0, "vulkan": 65.0}
    r = m2.set_image(model="flux2-dev-turbo", now=20)
    check("swap succeeds: the resident model's RAM is credited, not the blocker",
          r["ok"] and r["active_model"] == "flux2-dev-turbo" and r.get("backend") == "cuda")
    kl2.host_ram_gib = {}                       # nothing to credit now
    r = m2.set_image(model="flux2-klein", now=30)
    ready_all(m2, now_start=30)
    r = m2.set_image(model="flux2-dev-turbo", now=40)
    check("without credit the same swap is refused", r["ok"] is not True
          or r.get("downgraded_from") == "flux2-dev-turbo")

    # the downgrade note quotes the refusing axis, never an unrelated footprint
    m3 = mgr(FakeRunner(ready_after=1))
    m3.use("chat", now=0)
    ready_all(m3)
    m3.rec._real_host_ram_avail = lambda tier_, profile_: 30.0
    m3.set_image(model="flux2-klein", backend="vulkan", now=5)   # resident; owes no credit
    ready_all(m3, now_start=5)
    dt3 = next(l for l in m3.cfg.media["image"].prefer if l.active_model == "flux2-dev-turbo")
    dt3.backends, dt3.footprint_gib = ["cuda", "vulkan"], {"cuda": 8.0, "vulkan": 8.0}
    dt3.host_ram_gib = {"cuda": 90.0, "vulkan": 90.0}
    r = m3.set_image(model="flux2-dev-turbo", now=10)
    note = r.get("note") or ""
    check("downgrade still serves klein", r["ok"] and r["active_model"] == "flux2-klein")
    check("note names the host-RAM wall", "did not fit" in note and "host RAM 90" in note)
    check("note no longer quotes the unrelated VRAM footprint", "needs ~" not in note)

    # and when NOTHING fits, the 409-style error leads with the REFUSED model's
    # own wall. Requesting ideogram4 (last in the ladder) while the two ahead of
    # it are unfit on VRAM: wanted_reasons => host-RAM first; a leaked ladder
    # scan => VRAM first.
    dt3.footprint_gib = {k: 999.0 for k in dt3.footprint_gib}
    dt3.host_ram_gib = {}
    kl3 = next(l for l in m3.cfg.media["image"].prefer if l.active_model == "flux2-klein")
    kl3.footprint_gib = {k: 999.0 for k in kl3.footprint_gib}
    kl3.host_ram_gib = {}
    id4 = next(l for l in m3.cfg.media["image"].prefer if l.active_model == "ideogram4")
    id4.footprint_gib, id4.host_ram_gib = {"cuda": 8.0}, {"cuda": 90.0}
    r = m3.set_image(model="ideogram4", now=20)
    err = r.get("error") or ""
    det = err.split(" — ", 1)[-1]        # the appended detail, not the boilerplate
    check("total refusal leads with the refused model's own wall",
          r["ok"] is False and "fits the headroom" in err
          and "host RAM 90" in det
          and (det.index("host RAM") < det.index("VRAM") if "VRAM" in det else True))

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


def t_ladder_verdicts_match_scheduler() -> None:
    """GET /image's per-candidate verdict must BE the scheduler's verdict.

    2026-09-08: the dashboard showed flux2-dev-turbo as loadable on the iGPU
    ("would load on igpu0" plus an enabled Load button) because the client judged
    fit from footprint_gib vs device headroom alone. On the real box its
    host_ram_gib (122) exceeds MemTotal (124.4) once the tier margin is added, so
    the scheduler refuses it outright — the UI advertised a load that can never be
    permitted, exactly as every CUDA row correctly says "won't fit" while coding
    owns that VRAM. The verdict is now computed server-side by
    Reconciler._backend_fit, the same call _pick_image makes."""
    from stackd import telemetry
    from stackd.solver import headroom

    m = mgr(FakeRunner(ready_after=1))
    m.use("chat", now=0)
    ready_all(m)
    m.set_image(model="flux2-klein", backend="vulkan", now=5)
    ready_all(m, now_start=5)
    m.rec._real_host_ram_avail = lambda tier_, profile_: 30.0        # deterministic
    real_host_stats = telemetry.host_stats
    telemetry.host_stats = lambda: {"mem_total_gib": 100.0, "mem_available_gib": 34.0}
    try:
        tier = m.cfg.media["image"]
        kl = next(l for l in tier.prefer if l.active_model == "flux2-klein")
        dt = next(l for l in tier.prefer if l.active_model == "flux2-dev-turbo")
        dt.backends = ["cuda", "vulkan"]
        dt.footprint_gib = {"cuda": 8.0, "vulkan": 8.0}
        dt.host_ram_gib = {"cuda": 98.0, "vulkan": 98.0}   # unreachable on a 100-GiB box
        kl.host_ram_gib = {"cuda": 42.0, "vulkan": 42.0}

        v = m.image_status()
        rows = {r["active_model"]: r for r in v["prefer"]}
        dtv = rows["flux2-dev-turbo"]
        check("unsatisfiable candidate is reported refused, not loadable",
              dtv["fits"] is False and dtv["would_load_on"] is None)
        check("reason names host RAM and that the figure is unreachable",
              "host RAM 98" in dtv["fit"]["vulkan"]["reason"]
              and "unreachable" in dtv["fit"]["vulkan"]["reason"])
        check("cross-backend credit shows up in the arithmetic",
              dtv["fit"]["cuda"]["credit_gib"] == 42.0
              and "98 > 72.0 free" in dtv["fit"]["cuda"]["reason"])
        check("same-backend route earns no credit",
              dtv["fit"]["vulkan"]["credit_gib"] == 0.0)
        check("declared host RAM per backend is finally in the payload",
              dtv["host_ram_gib"]["vulkan"] == 98.0)
        check("host RAM + MemTotal published for the ladder header",
              v["host_ram_avail_gib"] == 30.0 and v["mem_total_gib"] == 100.0)

        # verdicts and scheduler must not disagree: first fitting row == the pick,
        # with the same teardown credit and the same host-RAM reading both use
        credit, credit_backend = m.rec._teardown_credit(m.state, tier)
        hr = headroom(m.cfg, "chat", m.catalog, reserve_gib=tier.margin_gib)
        pick = m.rec._pick_image(tier, hr, host_ram_avail=30.0,
                                 freed_gib=credit, freed_backend=credit_backend)
        first_fit = next(r for r in v["prefer"] if r["fits"])
        check("verdicts and scheduler agree on the pick",
              pick is not None and first_fit["active_model"] == pick[2].active_model)

        # an affordable figure flips the same row to fits, device named
        dt.host_ram_gib = {"cuda": 20.0, "vulkan": 20.0}
        v2 = {r["active_model"]: r for r in m.image_status()["prefer"]}
        check("affordable figure flips the row back to fits with a device",
              v2["flux2-dev-turbo"]["fits"] is True
              and v2["flux2-dev-turbo"]["would_load_on"] is not None)
    finally:
        telemetry.host_stats = real_host_stats


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


def t_route_fastfails_when_active_is_pinned() -> None:
    """A request for a name only a higher-priority profile serves, while the
    active (lower) profile is pinned, must return 'outranked' immediately — not
    'warming' (which makes the HTTP front poll a stack that never comes)."""
    m = mgr(FakeRunner(ready_after=1))
    m.use("chat", now=0)
    ready_all(m)
    m.pin()  # chat is now pinned; coding (higher priority) can't reactively enter
    rr = m.route("assistant-coder", now=50)  # a name only the `coding` profile serves
    check("pinned + outranked name -> status 'outranked' (fast fail, no warming poll)",
          rr.status == "outranked")
    check("outranked note explains the pin",
          "pinned" in (rr.note or "").lower())
    check("active profile unchanged (no switch attempted)", m.state.active_profile == "chat")
    m.unpin()
    rr2 = m.route("assistant-coder", now=60)  # now the switch is allowed
    check("unpinned -> reactive entry proceeds (warming)", rr2.status == "warming")


def main() -> int:
    for fn in [
        t_converge_chat,
        t_switch_to_coding_is_a_delta,
        t_cuda_teardown_uses_kill_and_probe,
        t_crash_restart_with_backoff,
        t_giveup_marks_dead_and_blocks_reactive_entry,
        t_idle_self_evict,
        t_pin_blocks_self_evict,
        t_idle_evict_counts_from_load_not_activation,
        t_no_timer_profile_autopins_on_entry,
        t_shutdown_engines_tears_down_everything,
        t_reactive_entry_and_standin,
        t_same_device_spawn_is_staggered,
        t_catalog_advertises_answering_ctx,
        t_preset_translation,
        t_sglang_pennyroyal_adapter,
        t_cuda_drain_barrier,
        t_tick_sweeps_stray_stacks,
        t_image_tier_is_stackd_created,
        t_image_identity_covers_container_spec,
        t_image_tier_scheduler,
        t_image_tier_no_headroom,
        t_image_swap,
        t_image_host_ram_guard,
        t_image_teardown_credit_and_refusals,
        t_image_unsafe_bench_bypass,
        t_ladder_verdicts_match_scheduler,
        t_image_none_survives_pin,
        t_reload_config,
        t_reactive_entry_refused_when_not_outranking,
        t_route_fastfails_when_active_is_pinned,
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
