"""Dependency-free checks for the sd.cpp image engine as a SELECTABLE PEER of ComfyUI
on the elastic image tier: `python3 tests/smoke_sdcpp.py`.

The shipped config/ tier is ComfyUI-only, so this injects an sd.cpp container + ladder
row into the loaded tier (what AI-STACK/config.local does on the real box) and drives
the selection spine: ladder pick -> engine/container resolution -> synth ModelSpec ->
adapter -> sd-server argv, plus the config cross-check against sdcpp_pipelines.json and
the comfyui<->sdcpp identity/in-place guard. No GPUs, docker, pytest, httpx, anyio.
"""
from __future__ import annotations
import pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import _env  # noqa: F401,E402
from stackd.config import load_config
from stackd.config.models import ContainerSpec, MountSpec, MediaLoadable, _check_media_tier
from stackd.engines.comfyui import ComfyuiAdapter
from stackd.engines.registry import TEMPLATES, adapter_for
from stackd.engines.sdcpp import SdcppAdapter, SdcppParams
from stackd.engines.base import EngineState
from stackd.imagegen import sdcpp_pipelines
from stackd.manager import Manager
from stackd.reconciler import Reconciler
from stackd.runner import FakeRunner
from stackd.state import ImageSlot

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"
CHECKS: list[tuple[str, bool]] = []

def check(name, cond): CHECKS.append((name, bool(cond)))

SDCPP_CONTAINER = {
    "name": "stackd-sdcpp", "image": "sdcpp-rocm:local", "devices": ["/dev/kfd"],
    "shm_size": "4g", "mem_limit_gib": 24.0, "ulimits": {"nofile": 65536},
    "env": {"GGML_ENABLE_LORA": "1"},
    "mounts": [{"host_path": "/srv/stackd/appdata/comfyui/models", "container_path": "/models", "ro": True}],
}

def _augment(cfg):
    tier = cfg.media["image"]
    tier.containers["sdcpp_vulkan"] = ContainerSpec(**{
        **SDCPP_CONTAINER, "mounts": [MountSpec(**m) for m in SDCPP_CONTAINER["mounts"]]})
    tier.prefer.append(MediaLoadable(
        active_model="flux2-dev-turbo-sdcpp", capabilities=["generate", "stylize", "edit"],
        backends=["vulkan"], footprint_gib={"vulkan": 16.0}, host_ram_gib={"vulkan": 24.0},
        engine="sdcpp", container="sdcpp_vulkan"))
    return tier


def t_engine_is_registered():
    check("sdcpp in the engine TEMPLATES registry", "sdcpp" in TEMPLATES)
    check("sdcpp template is SdcppAdapter", TEMPLATES.get("sdcpp") is SdcppAdapter)
    check("comfyui still registered (peer, not fork)", "comfyui" in TEMPLATES and TEMPLATES["comfyui"] is ComfyuiAdapter)
    check("SdcppParams defaults port 8188", SdcppParams(active_model="x").port == 8188)
    check("SdcppParams stages weights to disk", SdcppParams(active_model="x").params_backend == "diffusion=disk,te=disk")


def t_ladder_selects_sdcpp():
    cfg = load_config(CFG); tier = _augment(cfg); rec = Reconciler(cfg, FakeRunner())
    pick = rec._pick_image(tier, {"igpu0": 80.0, "cuda0": 0.0}, want_model="flux2-dev-turbo-sdcpp", host_ram_avail=1e9, want_backend="vulkan")
    check("ladder pick returns a candidate", pick is not None)
    if pick:
        dev, backend, ld = pick
        check("picked model is the sdcpp row", ld.active_model == "flux2-dev-turbo-sdcpp")
        check("picked entry declares engine=sdcpp", ld.engine == "sdcpp")
        check("picked entry overrides container=sdcpp_vulkan", ld.container == "sdcpp_vulkan")
        check("lands on the iGPU device", dev == "igpu0")
        check("device backend is vulkan (shared with comfyui-rocm)", backend == "vulkan")


def t_synth_routes_to_sdcpp_adapter():
    cfg = load_config(CFG); tier = _augment(cfg); rec = Reconciler(cfg, FakeRunner())
    synth = rec._synth_image_model(tier, "vulkan", "flux2-dev-turbo-sdcpp", ["generate", "stylize", "edit"])
    check("synth ModelSpec engine template is sdcpp", synth.engine.template == "sdcpp")
    adapter = adapter_for(synth, cfg.devices["igpu0"])
    check("adapter_for(synth) is SdcppAdapter", isinstance(adapter, SdcppAdapter))
    check("container image is sdcpp-rocm, NOT comfyui-rocm", synth.engine.container.image == "sdcpp-rocm:local")
    check("container name is the sdcpp override", synth.engine.container.name == "stackd-sdcpp")
    check("mem_limit_gib backstop carried", synth.engine.container.mem_limit_gib == 24.0)
    spec = adapter.launch_spec(rec._lc("igpu0", 8188)); cmd = spec.cmd
    check("argv has --diffusion-model fp8mixed", "--diffusion-model" in cmd and any("flux2_dev_fp8mixed" in c for c in cmd))
    check("argv has --vae", "--vae" in cmd and any("flux2-vae" in c for c in cmd))
    check("argv has --llm (bf16 TE)", "--llm" in cmd and any("mistral_3_small" in c for c in cmd))
    check("argv has --lora-model-dir", "--lora-model-dir" in cmd)
    check("argv stages weights to disk", "--params-backend" in cmd and "diffusion=disk,te=disk" in cmd)
    check("argv passes flash-attention", "--diffusion-fa" in cmd)
    check("argv sets --listen-port 8188", "--listen-port" in cmd and cmd[cmd.index("--listen-port") + 1] == "8188")
    check("health_url is the cheap capabilities probe", (spec.health_url or "").endswith("/sdcpp/v1/capabilities"))
    check("ready_timeout covers the minutes-long TE cold load", spec.ready_timeout_s >= 1800.0)
    check("/dev/kfd from the sdcpp container block", "/dev/kfd" in spec.device_paths)
    check("render node from the vulkan device_profile", "/dev/dri/renderD128" in spec.device_paths)
    cspec = adapter_for(rec._synth_image_model(tier, "vulkan", "flux2-klein", ["generate"]), cfg.devices["igpu0"]).launch_spec(rec._lc("igpu0", 8188))
    check("comfyui launch has no sd-server argv (peer separation)", "--diffusion-fa" not in cspec.cmd and "--diffusion-model" not in cspec.cmd)


def t_identity_covers_engine():
    cfg = load_config(CFG); tier = _augment(cfg); rec = Reconciler(cfg, FakeRunner())
    c_id = rec._image_identity(tier, "vulkan", "flux2-klein", "igpu0")
    s_id = rec._image_identity(tier, "vulkan", "flux2-dev-turbo-sdcpp", "igpu0")
    check("sdcpp identity records engine=sdcpp", "sdcpp" in s_id)
    check("comfyui identity records engine=comfyui", "comfyui" in c_id)
    check("identities differ across engines", c_id != s_id)
    check("sdcpp identity records the container-override key", "sdcpp_vulkan" in s_id)


def t_slot_engine_default():
    check("ImageSlot defaults to comfyui engine", ImageSlot(active_model="flux2-klein").engine == "comfyui")
    check("ImageSlot round-trips sdcpp engine", ImageSlot(active_model="x", engine="sdcpp").engine == "sdcpp")


def t_capabilities_advertise_engine():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "state.json"
    m = Manager(CFG, tmp, FakeRunner(ready_after=2))
    slot = ImageSlot(active_model="flux2-dev-turbo-sdcpp", kind="image", backend="vulkan", device="igpu0", engine="sdcpp", endpoint="http://stackd-sdcpp:8188")
    slot.state = EngineState.ready; m.state.image = slot
    check("capabilities()['image'] carries engine=sdcpp", m.capabilities()["image"].get("engine") == "sdcpp")
    cslot = ImageSlot(active_model="flux2-klein", kind="image", backend="vulkan", device="igpu0", endpoint="http://comfyui-rocm:8188")
    cslot.state = EngineState.ready; m.state.image = cslot
    check("comfyui slot advertises engine=comfyui", m.capabilities()["image"].get("engine") == "comfyui")


def t_config_validation_dispatches_manifest():
    cfg = load_config(CFG); tier = _augment(cfg)
    try:
        _check_media_tier(tier); ok = True
    except Exception:
        ok = False
    check("valid sdcpp prefer row passes _check_media_tier", ok)
    cfg2 = load_config(CFG); tier2 = _augment(cfg2); tier2.prefer[-1].capabilities = ["generate", "upscale"]
    r = False
    try:
        _check_media_tier(tier2)
    except Exception:
        r = True
    check("unknown sdcpp capability rejected at load", r)
    cfg3 = load_config(CFG); tier3 = _augment(cfg3); tier3.prefer[-1].engine = "not_a_real_engine"
    r2 = False
    try:
        _check_media_tier(tier3)
    except Exception:
        r2 = True
    check("unknown engine on a prefer row rejected at load", r2)


def t_pipelines_manifest_semantics():
    g = sdcpp_pipelines.sample_params("flux2-dev-turbo-sdcpp", "generate")
    s = sdcpp_pipelines.sample_params("flux2-dev-turbo-sdcpp", "stylize")
    check("generate keeps the turbo custom_sigmas", "custom_sigmas" in g["sample_params"])
    check("generate sigmas are the 8-step turbo schedule", len(g["sample_params"]["custom_sigmas"]) == 8)
    check("stylize OMITS custom_sigmas (trap 14)", "custom_sigmas" not in s["sample_params"])
    check("stylize carries strength 0.75", s.get("strength") == 0.75)
    check("distilled guidance 4.0 on both", g["sample_params"]["guidance"]["distilled_guidance"] == 4.0 and s["sample_params"]["guidance"]["distilled_guidance"] == 4.0)
    check("turbo LoRA threaded through", bool(g["lora"]) and "Turbo" in g["lora"][0]["path"])
    check("tools_for reports the three verbs", sdcpp_pipelines.tools_for("flux2-dev-turbo-sdcpp") == {"generate", "stylize", "edit"})
    r = False
    try:
        sdcpp_pipelines.sample_params("flux2-dev-turbo-sdcpp", "video")
    except sdcpp_pipelines.ToolUnsupported:
        r = True
    check("unsupported verb raises ToolUnsupported", r)


def main():
    for fn in [t_engine_is_registered, t_ladder_selects_sdcpp, t_synth_routes_to_sdcpp_adapter, t_identity_covers_engine, t_slot_engine_default, t_capabilities_advertise_engine, t_config_validation_dispatches_manifest, t_pipelines_manifest_semantics, t_client_image_extraction]:
        fn()
    for name, ok in CHECKS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    failed = sum(1 for _, ok in CHECKS if not ok)
    print(f"\n{'FAILURES' if failed else 'all passed'} ({len(CHECKS) - failed}/{len(CHECKS)})")
    return 1 if failed else 0



def t_client_image_extraction():
    """The spike's core trap: read result.images[] (a LIST of {index,b64_json}), not
    result.b64_json. Tolerate plain strings + a data-URL prefix + a top-level b64_json,
    and return [] (not a silent success) when there is genuinely no image -- which
    wait_and_fetch turns into a clear 'completed but returned no image' error."""
    import base64
    from stackd.imagegen.sdcpp_client import _extract_images as ex
    png = bytes([0x89, 0x50, 0x4E, 0x47]) + b"-fakepng"
    b = base64.b64encode(png).decode()
    check("images[] list of dicts decodes", ex({"images": [{"index": 0, "b64_json": b}]}) == [png])
    check("images[] list of plain strings decodes", ex({"images": [b]}) == [png])
    check("data: URL prefix tolerated", ex({"images": ["data:image/png;base64," + b]}) == [png])
    check("top-level b64_json fallback", ex({"b64_json": b}) == [png])
    check("empty result yields no images", ex({}) == [])
    check("shapeless images entry yields no images", ex({"images": [{"foo": 1}]}) == [])


if __name__ == "__main__":
    raise SystemExit(main())
