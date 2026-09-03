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
    for _ in range(6):
        now += 5
        m.tick(now=now)
        if all(s.state == EngineState.ready for s in m.state.stacks.values()):
            break
    return now


def t_converge_everyday() -> None:
    m = mgr()
    m.use("everyday", now=0)
    check("everyday spawns 3 stacks", set(m.state.stacks) == {
        "everyday-chat", "everyday-autocomplete", "everyday-image"})
    check("all warming after converge",
          all(s.state == EngineState.warming for s in m.state.stacks.values()))
    ready_all(m)
    check("all ready after ticks",
          all(s.state == EngineState.ready for s in m.state.stacks.values()))
    check("chat got a process port", m.state.stacks["everyday-chat"].port is not None)
    check("image is a container", m.state.stacks["everyday-image"].kind == "container")


def t_switch_to_coding_is_a_delta() -> None:
    m = mgr()
    m.use("everyday", now=0)
    ready_all(m)
    ac_handle = m.state.stacks["everyday-autocomplete"].handle
    m.use("coding", now=100)
    check("coding: chat torn down", "everyday-chat" not in m.state.stacks)
    check("coding: image torn down", "everyday-image" not in m.state.stacks)
    check("coding: flash spawned", "coding-flash" in m.state.stacks)
    check("coding: autocomplete untouched (same handle)",
          m.state.stacks["everyday-autocomplete"].handle == ac_handle)
    check("coding: autocomplete now owned by coding (listed there)",
          m.state.stacks["everyday-autocomplete"].owner_profile == "coding")


def t_cuda_teardown_uses_kill_and_probe() -> None:
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("everyday", now=0)
    ready_all(m)
    chat_handle = m.state.stacks["everyday-chat"].handle
    probes_before = fake.probe_calls
    m.use("coding", now=100)
    check("cuda0 engine container stopped", fake.containers[chat_handle].running is False)
    check("nvidia-smi probed on cuda teardown", fake.probe_calls > probes_before)


def t_crash_restart_with_backoff() -> None:
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("everyday", now=0)
    ready_all(m)
    h = m.state.stacks["everyday-chat"].handle
    fake.crash(h)
    ev = m.tick(now=50)
    rt = m.state.stacks["everyday-chat"]
    check("crash detected", any(e.action == "crash" for e in ev))
    check("backoff scheduled", rt.backoff_until is not None and rt.backoff_until > 50)
    check("still in error before backoff elapses", rt.state == EngineState.error)
    m.tick(now=rt.backoff_until + 1)
    rt = m.state.stacks["everyday-chat"]
    check("respawned after backoff", rt.restarts == 1 and rt.state in (
        EngineState.warming, EngineState.ready))


def t_idle_self_evict() -> None:
    m = mgr(FakeRunner(ready_after=1), min_residency_s=60)
    m.use("everyday", now=0)
    ready_all(m)
    m.use("coding", now=1000)
    ready_all(m, now_start=1000)
    m.tick(now=1000 + 30 * 60)  # 30 min < 45m idle_evict -> stays
    check("coding still active at 30m idle", m.state.active_profile == "coding")
    m.tick(now=1000 + 46 * 60)  # > 45m -> self-evict
    check("coding self-evicted to everyday at 46m", m.state.active_profile == "everyday")
    check("everyday reconverged", "everyday-chat" in m.state.stacks)


def t_pin_blocks_self_evict() -> None:
    m = mgr(FakeRunner(ready_after=1), min_residency_s=1)
    m.use("everyday", now=0)
    m.use("coding", now=10)
    m.pin()
    m.tick(now=10 + 60 * 60)
    check("pinned coding survives 60m idle", m.state.active_profile == "coding")


def t_reactive_entry_and_standin() -> None:
    m = mgr(FakeRunner(ready_after=1), switch_cooldown_s=0)
    m.use("everyday", now=0)
    ready_all(m)

    r = m.route("assistant-coder", now=100)  # coding-owned, outranks everyday
    check("agentic request enters coding", m.state.active_profile == "coding")
    check("route reports warming", r.status == "warming" and r.profile == "coding")

    ready_all(m, now_start=100)
    r2 = m.route("assistant", now=200)  # everyday-owned, coding is active + masks assistant*
    check("chat request served by stand-in (mask)", r2.status == "ok")
    check("stand-in did NOT switch down", m.state.active_profile == "coding")
    check("stand-in endpoint is coding-flash",
          r2.endpoint == m.state.stacks["coding-flash"].endpoint)

    r3 = m.route("coding-autocomplete", now=210)  # keep@igpu0 survivor
    check("autocomplete served by kept igpu0 stack", r3.status == "ok")


def t_cuda_drain_barrier() -> None:
    """Switching everyday->coding tears down the 27B on cuda0 then allocates
    coding-flash on top. The drain barrier waits for free VRAM to settle first,
    and aborts the spawn if nvidia-smi stops responding (card wedging)."""
    # healthy: free VRAM ramps as the scrubber releases, then plateaus -> proceed
    fake = FakeRunner(ready_after=1)
    fake.gpu_free_values = [6000, 20000, 55000, 60000, 60000, 60000]
    m = mgr(fake)
    m.use("everyday", now=0)
    ready_all(m)
    ev = m.use("coding", now=100)
    check("drain ran on the cuda switch", any(e.action == "drain" for e in ev))
    check("coding-flash spawned after drain settled", "coding-flash" in m.state.stacks)
    check("27B torn down", "everyday-chat" not in m.state.stacks)

    # wedge: nvidia-smi stops answering -> abort the spawn, don't pile on
    fake2 = FakeRunner(ready_after=1)
    m2 = mgr(fake2)
    m2.use("everyday", now=0)
    ready_all(m2)
    fake2.gpu_free_values = [8000, None, None, None]
    ev2 = m2.use("coding", now=100)
    check("wedge -> degraded event", any(e.action == "degraded" for e in ev2))
    check("wedge -> coding-flash NOT spawned on the stuck card", "coding-flash" not in m2.state.stacks)


def t_tick_sweeps_stray_stacks() -> None:
    """A stack left in state that the active profile doesn't want (e.g. a switch
    whose converge threw mid-teardown) is stopped by the next tick, not
    crash-restarted forever."""
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("everyday", now=0)
    ready_all(m)
    m.use("coding", now=100)
    ready_all(m, now_start=100)
    # simulate a half-done switch back: active=everyday but coding-flash lingers
    from stackd.solver import solve
    m.state.active_profile = "everyday"
    check("coding-flash lingering in state", "coding-flash" in m.state.stacks)
    m.tick(now=200)
    check("tick swept the stray coding-flash", "coding-flash" not in m.state.stacks)
    check("everyday reconverged after sweep",
          {"everyday-chat", "everyday-image"} <= set(m.state.stacks))


def t_comfyui_is_stackd_created() -> None:
    """Both ComfyUIs are created by stackd from config (no `adopt`) — image, GPU
    wiring, per-container devices/shm and the shared traefik labels come through."""
    cfg = load_config(CFG)
    rec = Reconciler(cfg, FakeRunner())

    for mn, dev, want_img, want_dev in [
        ("everyday-image", "cuda0", "yanwk/comfyui-boot", None),
        ("coding-image", "igpu0", "comfyui-rocm", "/dev/kfd"),
    ]:
        m = cfg.models[mn]
        check(f"{mn}: not adopt", m.engine.container.adopt is False)
        spec = adapter_for(m, cfg.devices[dev]).launch_spec(rec._lc(dev, 8188))
        check(f"{mn}: image set", spec.image and want_img in spec.image)
        check(f"{mn}: traefik label resolved",
              spec.labels.get("traefik.http.services.comfyui-svc.loadbalancer.server.port") == "8188")
        if want_dev:
            check(f"{mn}: {want_dev} device path", want_dev in spec.device_paths)

    ci = cfg.models["coding-image"]
    spec = adapter_for(ci, cfg.devices["igpu0"]).launch_spec(rec._lc("igpu0", 8188))
    check("coding-image: renderD128 from vulkan profile", "/dev/dri/renderD128" in spec.device_paths)
    check("coding-image: shm_size 8g override", spec.shm_size == "8g")
    check("coding-image: seccomp from vulkan profile", "seccomp:unconfined" in spec.security_opt)

    # coding-image carries a `stackctl build` recipe; everyday-image (stock image) doesn't
    check("coding-image: build recipe present", ci.engine.container.build is not None
          and ci.engine.container.build.dockerfile == "Dockerfile.igpu")
    check("everyday-image: no build recipe", cfg.models["everyday-image"].engine.container.build is None)

    # both ComfyUIs mount the SAME host scratch dir (they never run together)
    def _scratch(mn):
        m = cfg.models[mn]
        sp = adapter_for(m, cfg.devices[m.placement.devices[0]]).launch_spec(
            rec._lc(m.placement.devices[0], 8188))
        return sorted(x.host_path for x in sp.mounts if "/scratch/" in x.host_path)
    check("everyday + coding share scratch host paths", _scratch("everyday-image") == _scratch("coding-image"))


def t_identity_covers_container_spec() -> None:
    """A ${VAR}/.env edit that lands in the `container:` block (image, env, mount,
    label, …) changes model_identity -> converge recreates the engine on reload,
    no `docker rm -f`."""
    from stackd.solver import model_identity
    cfg = load_config(CFG)
    base = model_identity(cfg, "everyday-image", "cuda0")
    cfg.models["everyday-image"].engine.container.image = "some/other:tag"
    check("image change -> new identity", model_identity(cfg, "everyday-image", "cuda0") != base)
    cfg.models["everyday-image"].engine.container.image = None
    cfg.models["everyday-image"].engine.container.env["RELOAD_TEST"] = "1"
    check("env change -> new identity", model_identity(cfg, "everyday-image", "cuda0") != base)

    # and the reconciler acts on it: changed identity -> reload_ (stop+remove, respawn)
    fake = FakeRunner(ready_after=1)
    m = mgr(fake)
    m.use("everyday", now=0)
    ready_all(m)
    cn = m.state.stacks["everyday-image"].container            # "comfyui-cuda"
    m.cfg.models["everyday-image"].engine.container.env["RELOAD_TEST"] = "1"   # simulate a reload edit
    ev = m.rec.converge(m.state, "everyday", now=200) or m.rec.events
    check("engine removed on spec change", cn in fake.removed)
    check("engine respawned fresh", m.state.stacks["everyday-image"].container == cn
          and m.state.stacks["everyday-image"].state != EngineState.ready)


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


def t_reactive_entry_refused_when_not_outranking() -> None:
    m = mgr(FakeRunner(ready_after=1))
    m.use("coding", now=0)
    ready_all(m)
    try:
        m.use("everyday", manual=False, now=100)
        check("reactive downgrade refused", False)
    except Outranked:
        check("reactive downgrade refused", True)


def main() -> int:
    for fn in [
        t_converge_everyday,
        t_switch_to_coding_is_a_delta,
        t_cuda_teardown_uses_kill_and_probe,
        t_crash_restart_with_backoff,
        t_idle_self_evict,
        t_pin_blocks_self_evict,
        t_reactive_entry_and_standin,
        t_cuda_drain_barrier,
        t_tick_sweeps_stray_stacks,
        t_comfyui_is_stackd_created,
        t_identity_covers_container_spec,
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
