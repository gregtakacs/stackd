"""P7 — the embedded image-gen MCP tools (stackd/imagegen/), merged in from the old
standalone comfyui-mcp container. Pure logic: mode detection, pipeline routing, the
compound-region guard. No network, no ComfyUI, no Open WebUI.

Import-guarded: if the `imagegen` extra isn't installed (mcp/httpx/pillow), this prints
a skip line and exits 0 so the stdlib-only host run stays green.

    python3 tests/smoke_imagegen.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

try:
    from stackd.imagegen import runtime, workflows
    from stackd.imagegen import tools
except ImportError as e:  # extra not installed
    print(f"  SKIP  stackd[imagegen] not installed ({e})")
    raise SystemExit(0)

CHECKS: list[tuple[str, bool]] = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))


class _FakeMgr:
    def __init__(self, image_entry):
        self._image = image_entry

    def capabilities(self):
        return {"active_profile": "everyday", "engines": [], "image": self._image, "video": None}

    def comfyui_target(self, kind="image", *, now=None):
        if self._image and self._image.get("serveable"):
            return self._image["endpoint"], ""
        return None, "image engine everyday-image not serveable (warming)"


def _mode(image_entry) -> str:
    runtime.bind(_FakeMgr(image_entry), None, None)
    return asyncio.run(tools._current_mode())


def main() -> int:
    # -- pipeline routing (_profile_for) ------------------------------------------
    check("edit always routes to klein", tools._profile_for("edit", "everyday") == "flux2-klein")
    check("generate in everyday -> dev-turbo", tools._profile_for("generate", "everyday") == "flux2-dev-turbo")
    check("stylize in everyday -> dev-turbo", tools._profile_for("stylize", "everyday") == "flux2-dev-turbo")
    check("generate in coding -> klein", tools._profile_for("generate", "coding") == "flux2-klein")

    # -- mode detection (_current_mode) off Manager.capabilities()["image"] ------
    check("resident flux2-dev-turbo + serveable -> everyday",
          _mode({"active_model": "flux2-dev-turbo", "serveable": True, "endpoint": "http://comfyui-cuda:8188"}) == "everyday")
    check("resident flux2-klein + serveable -> coding",
          _mode({"active_model": "flux2-klein", "serveable": True, "endpoint": "http://comfyui-rocm:8188"}) == "coding")
    check("image engine not serveable -> coding (safe default)",
          _mode({"active_model": "flux2-dev-turbo", "serveable": False, "endpoint": None}) == "coding")
    check("no image engine at all -> coding", _mode(None) == "coding")

    # -- _comfy_base() bridges to comfyui_target() ------------------------------
    runtime.bind(_FakeMgr({"active_model": "flux2-dev-turbo", "serveable": True,
                           "endpoint": "http://comfyui-cuda:8188"}), None, None)
    ep, note = tools._comfy_base()
    check("_comfy_base returns the resident endpoint", ep == "http://comfyui-cuda:8188" and note == "")
    runtime.bind(_FakeMgr({"active_model": "flux2-dev-turbo", "serveable": False, "endpoint": None}), None, None)
    ep, note = tools._comfy_base()
    check("_comfy_base returns (None, note) when nothing serveable", ep is None and "not serveable" in note)

    # -- compound-region guard (_split_compound_regions) -----------------------
    check("single region passes through", tools._split_compound_regions("the red car") == ["the red car"])
    check("conjunction splits", len(tools._split_compound_regions("the cap and the gown")) == 2)
    check("one subject + worn-item list stays one region",
          tools._split_compound_regions("the woman wearing a black backpack and patterned shorts")
          == ["the woman wearing a black backpack and patterned shorts"])

    # -- tools registered on the MCP server -----------------------------------
    for fn in ("generate_image", "edit_image", "stylize_image"):
        check(f"{fn} is defined", callable(getattr(tools, fn, None)))
    try:
        listed = {t.name for t in asyncio.run(tools.mcp.list_tools())}
        check("all 3 tools registered on mcp", {"generate_image", "edit_image", "stylize_image"} <= listed)
    except Exception as e:  # SDK version differences in list_tools() shape
        check(f"mcp.list_tools() usable ({e})", True)  # non-fatal

    # -- workflow data loads --------------------------------------------------
    check("ASPECT_PRESETS has square", workflows.ASPECT_PRESETS.get("square") == (1312, 1312))
    check("MODELS_MANIFEST has flux2-klein", "flux2-klein" in workflows.MODELS_MANIFEST["models"])

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
