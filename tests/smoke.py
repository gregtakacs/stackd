"""Dependency-free sanity check: `python3 tests/smoke.py` from the repo root.
The full suite is tests/test_validator.py (needs pytest)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

from stackd.config.loader import load_config  # noqa: E402
from stackd.planner import plan_transition  # noqa: E402
from stackd.validator import validate_profile  # noqa: E402

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"


def _approx(a: float, b: float, tol: float = 0.5) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    cfg = load_config(CFG)
    checks: list[tuple[str, bool]] = []

    checks.append(("pools", set(cfg.pools) == {"cuda_vram", "host_unified"}))
    checks.append(("profiles", set(cfg.profiles) == {"chat", "coding"}))

    ev = validate_profile(cfg, "chat")
    checks.append(("chat fits", ev.ok))
    cuda = next(p for p in ev.pools if p.pool == "cuda_vram")
    # image generation is no longer a profile member — just the 27B chat here
    checks.append(("chat cuda_vram=30", _approx(cuda.used_gib, 30.0)))
    uni = next(p for p in ev.pools if p.pool == "host_unified")
    # 28 reserve + 4 slack + (autocomplete 2 + headroom 3) + chat 3
    checks.append(("chat host_unified=39", _approx(uni.used_gib, 39.3, 0.6)))
    checks.append(
        ("chat: no contention flag (image tier is separate)",
         not any("contention on cuda0" in f for f in ev.flags))
    )

    co = validate_profile(cfg, "coding")
    checks.append(("coding fits", co.ok))
    checks.append(("coding keeps autocomplete", "chat-autocomplete" in co.resident))
    checks.append(("coding drops chat", "chat" not in co.resident))

    cuda = next(p for p in co.pools if p.pool == "cuda_vram")
    checks.append(("coding fills cuda0 (tight but ok)", co.ok and cuda.headroom_gib < 10))
    checks.append(("coding-flash stands in for chat (serves assistant*)",
                   any(se.api_name == "assistant*" for se in cfg.models["coding-flash"].serves)))

    plan = plan_transition(cfg, "chat", "coding")
    checks.append(("plan teardown", plan.teardown == ["chat"]))
    checks.append(("plan spawn", sorted(plan.spawn) == ["coding-flash"]))
    # chat-autocomplete is NOT a plain "keep" across this switch: it's the
    # second llamacpp port-consumer behind `chat` (11501) but the FIRST behind
    # `coding-flash`, a non-llamacpp vLLM model that never consumes a port slot
    # (11500) -- a genuine port reassignment, correctly caught as `reload`
    # since the port-identity fix (2026-09-04). Before that fix this silently
    # showed up as `keep`, which was the exact bug: an old container kept
    # running on its stale port while stackd's health check followed the new
    # (wrong) one.
    checks.append(("plan reload (port reassigned, not silently kept)",
                   plan.reload == ["chat-autocomplete"]))
    checks.append(("plan keep is empty", plan.keep == []))

    # --- llamacpp's dynamically-assigned port must be part of the identity
    # converge() diffs on -- otherwise a port reassignment (e.g. a profile
    # switch that changes which llamacpp model claims 11500 first) leaves an
    # old container running on its stale --port forever, while stackd's OWN
    # health check follows the newly-computed (wrong) port: permanent
    # "warming", never crashing, never healing (chat-autocomplete got stuck
    # exactly this way after a bench-image -> chat switch, 2026-09-04). ---
    from stackd.solver import model_identity, solve

    dev = "igpu0"
    id_11500 = model_identity(cfg, "chat-autocomplete", dev, port=11500)
    id_11501 = model_identity(cfg, "chat-autocomplete", dev, port=11501)
    checks.append(("model_identity: different port -> different identity", id_11500 != id_11501))
    checks.append(("model_identity: same port -> same identity",
                   model_identity(cfg, "chat-autocomplete", dev, port=11500) == id_11500))

    # end-to-end: solve() with chat-autocomplete FIRST in the profile's model
    # list (as if it were the only llamacpp model, like AI-STACK's real
    # bench-image profile) assigns it 11500; with it SECOND (chat's real
    # order) it gets 11501 -- these must carry different identities.
    pr = cfg.profiles["chat"]
    original_models = list(pr.models)
    try:
        pr.models = ["chat-autocomplete"]
        pl_alone = solve(cfg, "chat", None)
        pr.models = original_models
        pl_with_chat = solve(cfg, "chat", None)
    finally:
        pr.models = original_models
    checks.append(("solve(): chat-autocomplete alone -> port 11500",
                   pl_alone.placed["chat-autocomplete"].port == 11500))
    checks.append(("solve(): chat-autocomplete behind chat -> port 11501",
                   pl_with_chat.placed["chat-autocomplete"].port == 11501))
    checks.append(("solve(): the port shift changes the diffed identity",
                   pl_alone.placed["chat-autocomplete"].identity
                   != pl_with_chat.placed["chat-autocomplete"].identity))

    # ${VAR} / ${VAR:-default} interpolation + .env-file override
    import tempfile

    from stackd.config._build import ConfigError
    from stackd.config.loader import _interpolate, _parse_env_file
    E = {"STACKD_SMOKE_V": "xyz", "EMPTYVAR": ""}
    checks.append(("interp: set var", _interpolate("a: ${STACKD_SMOKE_V}/m", E) == "a: xyz/m"))
    checks.append(("interp: default used", _interpolate("${NOPE_X:-def}", E) == "def"))
    checks.append(("interp: empty env falls to default", _interpolate("${EMPTYVAR:-d}", E) == "d"))
    checks.append(("interp: bare $ untouched", _interpolate("p$w", E) == "p$w"))
    try:
        _interpolate("${NOPE_X}", E)
        checks.append(("interp: unset+no default raises", False))
    except ConfigError as e:
        checks.append(("interp: unset+no default raises", "NOPE_X" in str(e)))

    ef = pathlib.Path(tempfile.mkdtemp()) / "x.env"
    ef.write_text('# c\nFOO=bar\nQUX="q q"  # trailing\n\nBLANK\n')
    parsed = _parse_env_file(ef)
    checks.append(("_parse_env_file", parsed == {"FOO": "bar", "QUX": "q q"}))

    # --- gpu_discovery: PCI-sysfs render-node detection, on a fake sysfs tree ---
    from stackd.gpu_discovery import enumerate_gpus, render_nodes_by_vendor

    fake_drm = pathlib.Path(tempfile.mkdtemp()) / "drm"

    def _mk_card(card: str, render: str | None, vendor: str, cls: str, pci: str) -> None:
        # device/ must resolve (via realpath) to a path whose basename is the PCI
        # address, matching real /sys/class/drm/cardN/device -> ../../../<pci>.
        pci_dir = fake_drm.parent / "pci_devices" / pci
        pci_dir.mkdir(parents=True)
        (pci_dir / "vendor").write_text(vendor + "\n")
        (pci_dir / "class").write_text(cls + "\n")
        (fake_drm / card).mkdir(parents=True)
        (fake_drm / card / "device").symlink_to(pci_dir, target_is_directory=True)
        if render:
            (pci_dir / "drm" / render).mkdir(parents=True)

    _mk_card("card0", "renderD129", "0x10de", "0x030000", "0000:33:00.0")   # nvidia, higher PCI addr
    _mk_card("card1", "renderD128", "0x1002", "0x030000", "0000:be:00.0")   # amd
    _mk_card("card2", None, "0x1002", "0x030002", "0000:aa:00.0")           # amd, no render node (3D ctrl class)

    gpus = enumerate_gpus(str(fake_drm))
    checks.append(("gpu_discovery: finds all 3 display-class cards", len(gpus) == 3))
    checks.append(("gpu_discovery: sorted by PCI address", [g["pci"] for g in gpus]
                   == ["0000:33:00.0", "0000:aa:00.0", "0000:be:00.0"]))
    checks.append(("gpu_discovery: vendor decoded", gpus[0]["vendor"] == "nvidia"
                   and gpus[2]["vendor"] == "amd"))
    checks.append(("gpu_discovery: render node resolved", gpus[0]["render_node"] == "/dev/dri/renderD129"))
    checks.append(("gpu_discovery: no-render card handled, not crashed", gpus[1]["render_node"] is None))
    checks.append(("gpu_discovery: render_nodes_by_vendor amd, PCI order",
                   render_nodes_by_vendor("amd", str(fake_drm)) == ["/dev/dri/renderD128"]))
    checks.append(("gpu_discovery: unknown sysfs path -> [] not crash",
                   enumerate_gpus("/no/such/path") == []))

    # --- _apply_dynamic_gpu_env: detected value overrides a stale static one ---
    from stackd.config import loader as _loader_mod
    import stackd.gpu_discovery as _gd
    _orig = _gd.render_nodes_by_vendor
    _gd.render_nodes_by_vendor = lambda vendor: ["/dev/dri/renderD128"] if vendor == "amd" else []
    try:
        env_probe = {"IGPU_RENDER_NODE": "/dev/dri/renderD999"}   # simulated stale/drifted value
        _loader_mod._apply_dynamic_gpu_env(env_probe)
        checks.append(("_apply_dynamic_gpu_env overrides stale value",
                       env_probe["IGPU_RENDER_NODE"] == "/dev/dri/renderD128"))
    finally:
        _gd.render_nodes_by_vendor = _orig
    # a .env-file value overrides os.environ for config interpolation
    import os
    os.environ["CUDA_VRAM_GIB"] = "999"
    ef2 = pathlib.Path(tempfile.mkdtemp()) / "y.env"
    ef2.write_text("CUDA_VRAM_GIB=42\n")
    cfg2 = load_config(CFG, env_file=str(ef2))
    checks.append((".env file overrides os.environ", cfg2.pools["cuda_vram"].total_gib == 42.0))
    os.environ["CUDA_VRAM_GIB"] = "95.6"

    # --- config overlay: per-file replace / add / delete on top of the base ---
    ov = pathlib.Path(tempfile.mkdtemp()) / "overlay"
    (ov / "models").mkdir(parents=True)
    (ov / "profiles").mkdir()
    (ov / "profiles" / "coding.yaml").write_text(       # REPLACE: bump priority, drop autocomplete
        "profile: coding\npriority: 999\nmodels: [coding-flash]\n")
    (ov / "profiles" / "chat.yaml").write_text(     # REPLACE: also drop autocomplete
        "profile: chat\npriority: 50\ndefault: true\nmodels: [chat]\n")
    (ov / "models" / "chat-autocomplete.yaml").write_text("")   # DELETE (empty overlay file)
    (ov / "models" / "brandnew.yaml").write_text(          # ADD
        "model: brandnew\n"
        "engine: { template: llamacpp-cuda, model: foo, params: { ctx: 4096, parallel: 1 } }\n"
        "budget: { vram_gib: 1 }\nplacement: { devices: [cuda0] }\n"
        "serves: [{ api_name: brandnew }]\n")
    ovc = load_config(CFG, overlay=str(ov))
    checks.append(("overlay: replaced file wins", ovc.profiles["coding"].priority == 999))
    checks.append(("overlay: new file adds", "brandnew" in ovc.models))
    checks.append(("overlay: empty file deletes", "chat-autocomplete" not in ovc.models))
    checks.append(("overlay: base files untouched", "chat" in ovc.models
                   and load_config(CFG).profiles["chat"].priority == 50))

    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in checks)}/{len(checks)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
