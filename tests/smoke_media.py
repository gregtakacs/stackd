"""Dependency-free checks for the elastic media tier config (Phase 1):
`python3 tests/smoke_media.py` from the repo root.

Covers config/media/<kind>.yaml loading, the prefer[] catalog + per-backend
containers, the load-time cross-checks, and per-file overlay behaviour. The
scheduler that consumes this lands in Phase 2.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

from stackd.config._build import ConfigError  # noqa: E402
from stackd.config.loader import load_config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
CHECKS: list[tuple[str, bool]] = []


def check(name: str, cond: object) -> None:
    CHECKS.append((name, bool(cond)))


def _write(d: pathlib.Path, rel: str, text: str) -> None:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


_MEDIA_MIN = """\
kind: image
margin_gib: 4
containers:
  cuda:
    name: comfyui-cuda
    image: comfyui:cuda
    mounts:
      - { host_path: /models, container_path: /root/ComfyUI/models, ro: false }
prefer:
  - active_model: flux2-klein
    capabilities: [generate, stylize, edit]
    backends: [cuda]
    footprint_gib: { cuda: 24 }
"""


def _mkcfg(media_image: str | None) -> pathlib.Path:
    """A copy of the shipped config, with config/media/ replaced by just the given
    image.yaml (or removed entirely when None)."""
    d = pathlib.Path(tempfile.mkdtemp()) / "cfg"
    shutil.copytree(CFG, d)
    shutil.rmtree(d / "media", ignore_errors=True)
    if media_image is not None:
        _write(d, "media/image.yaml", media_image)
    return d


def main() -> int:
    # --- the shipped worked example -----------------------------------------------
    cfg = load_config(CFG)
    check("config/media/image.yaml loaded", "image" in cfg.media)
    t = cfg.media["image"]
    check("tier kind = image", t.kind == "image")
    check("margin_gib parsed", t.margin_gib == 4.0)
    check("both backend containers present", set(t.containers) == {"cuda", "vulkan"})
    check("cuda container is a ContainerSpec", t.containers["cuda"].name == "comfyui-cuda")
    check("vulkan container carries the build recipe",
          t.containers["vulkan"].build is not None
          and t.containers["vulkan"].build.dockerfile == "Dockerfile.igpu")
    check("vulkan container keeps /dev/kfd", "/dev/kfd" in t.containers["vulkan"].devices)
    check("prefer[] is ordered dev-turbo then klein",
          [ld.active_model for ld in t.prefer] == ["flux2-dev-turbo", "flux2-klein"])
    check("first entry is cuda-only", t.prefer[0].backends == ["cuda"])
    check("klein runs on either backend", set(t.prefer[1].backends) == {"cuda", "vulkan"})
    check("footprint per backend", t.prefer[1].footprint_gib == {"cuda": 24.0, "vulkan": 20.0})
    check("capabilities carried on the loadable",
          t.prefer[0].capabilities == ["generate", "stylize", "edit"])

    # --- media/ is optional ----------------------------------------------------------
    nomedia = load_config(_mkcfg(None))
    check("config with no media/ dir still loads", nomedia.media == {})

    minimal = load_config(_mkcfg(_MEDIA_MIN))
    check("minimal media/image.yaml loads", list(minimal.media) == ["image"])

    # --- cross-checks reject bad tiers ---------------------------------------------
    bad_cap = _MEDIA_MIN.replace("[generate, stylize, edit]", "[generate, frobnicate]")
    try:
        load_config(_mkcfg(bad_cap))
        check("capability with no workflow graph -> ConfigError", False)
    except ConfigError as e:
        check("capability with no workflow graph -> ConfigError", "frobnicate" in str(e))

    bad_backend = _MEDIA_MIN.replace("backends: [cuda]", "backends: [rocm]")
    try:
        load_config(_mkcfg(bad_backend))
        check("backend with no containers block -> ConfigError", False)
    except ConfigError as e:
        check("backend with no containers block -> ConfigError", "containers.rocm" in str(e))

    no_footprint = _MEDIA_MIN.replace("    footprint_gib: { cuda: 24 }\n", "    footprint_gib: {}\n")
    try:
        load_config(_mkcfg(no_footprint))
        check("missing footprint for a listed backend -> ConfigError", False)
    except ConfigError as e:
        check("missing footprint for a listed backend -> ConfigError", "footprint_gib.cuda" in str(e))

    unknown_key = _MEDIA_MIN.replace("margin_gib: 4", "margin_gib: 4\nwibble: 1")
    try:
        load_config(_mkcfg(unknown_key))
        check("unknown tier key -> ConfigError", False)
    except ConfigError as e:
        check("unknown tier key -> ConfigError", "wibble" in str(e))

    # --- per-file overlay ----------------------------------------------------------
    base = _mkcfg(_MEDIA_MIN)
    ov = pathlib.Path(tempfile.mkdtemp()) / "ov"
    _write(ov, "media/image.yaml", _MEDIA_MIN.replace("margin_gib: 4", "margin_gib: 9")
           .replace("footprint_gib: { cuda: 24 }", "footprint_gib: { cuda: 11 }"))
    over = load_config(base, overlay=str(ov))
    check("overlay media/image.yaml wins", over.media["image"].margin_gib == 9.0)
    check("overlay replaced the whole file", over.media["image"].prefer[0].footprint_gib == {"cuda": 11.0})
    check("base media/image.yaml untouched",
          load_config(base).media["image"].margin_gib == 4.0)

    _write(ov, "media/video.yaml",
           "kind: video\ncontainers:\n  cuda: { name: v, image: v:cuda }\n"
           "prefer:\n  - { active_model: wan-t2v, capabilities: [], backends: [cuda], footprint_gib: { cuda: 30 } }\n")
    added = load_config(base, overlay=str(ov))
    check("overlay adds a new media kind", set(added.media) == {"image", "video"})
    check("non-image tier skips the graph cross-check", added.media["video"].kind == "video")

    _write(ov, "media/image.yaml", "")
    deleted = load_config(base, overlay=str(ov))
    check("empty overlay file deletes the base tier", "image" not in deleted.media)

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
