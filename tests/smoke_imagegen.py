"""P7 — the embedded image-gen MCP tools (stackd/imagegen/), merged in from the old
standalone comfyui-mcp container. Pure logic: mode detection, pipeline routing, the
compound-region guard. No network, no ComfyUI, no Open WebUI.

Import-guarded: if the `imagegen` extra isn't installed (mcp/httpx/pillow), this prints
a skip line and exits 0 so the stdlib-only host run stays green.

    python3 tests/smoke_imagegen.py
"""

from __future__ import annotations

import asyncio
import json
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


_PREFER = [
    {"active_model": "flux2-dev-turbo", "capabilities": ["generate", "stylize", "edit"],
     "backends": ["cuda"], "footprint_gib": {"cuda": 34}},
    {"active_model": "flux2-klein", "capabilities": ["generate", "stylize", "edit"],
     "backends": ["cuda", "vulkan"], "footprint_gib": {"cuda": 24, "vulkan": 20}},
    {"active_model": "ideogram4", "capabilities": ["generate"],
     "backends": ["cuda"], "footprint_gib": {"cuda": 34}},
]


class _FakeMgr:
    def __init__(self, image_entry, swap=None):
        self._image = image_entry
        self._swap = swap          # dict the fake set_image returns; also mutates _image on ok

    def capabilities(self):
        return {"active_profile": "chat", "engines": [], "image": self._image, "video": None}

    def image_status(self):
        return {"resident": self._image, "headroom_gib": {"cuda0": 60.0}, "prefer": _PREFER}

    def set_image(self, *, model=None, need_capability=None, now=None):
        res = self._swap or {"ok": False, "error": "no capable image model available"}
        if res.get("ok") and res.get("active_model"):
            self._image = {"active_model": res["active_model"], "serveable": True,
                           "capabilities": res.get("capabilities", ["generate", "stylize", "edit"]),
                           "endpoint": "http://comfyui:8188"}
        return res

    def comfyui_target(self, kind="image", *, now=None):
        if self._image and self._image.get("serveable"):
            return self._image["endpoint"], ""
        if self._image:
            return None, f"image engine image:{self._image['active_model']} not serveable (warming)"
        return None, "no image engine resident (no headroom under the active profile)"


def _resident(image_entry, tool="generate", swap=None):
    runtime.bind(_FakeMgr(image_entry, swap), None, None)
    return asyncio.run(tools._resident_model_for(tool))   # (active_model, note)


def main() -> int:
    # -- resident-model selection (_resident_model_for) -------------------------
    # No "coding"/"chat" notion: whatever active_model is resident, if it
    # declares the verb; otherwise escalate via the elastic image tier.
    dev = {"active_model": "flux2-dev-turbo", "serveable": True,
           "capabilities": ["generate", "stylize", "edit"], "endpoint": "http://comfyui-cuda:8188"}
    klein = {"active_model": "flux2-klein", "serveable": True,
             "capabilities": ["generate", "stylize", "edit"], "endpoint": "http://comfyui-rocm:8188"}
    check("resident dev-turbo -> dev-turbo (generate)", _resident(dev, "generate")[0] == "flux2-dev-turbo")
    check("resident dev-turbo -> dev-turbo (edit, no klein special-case)",
          _resident(dev, "edit")[0] == "flux2-dev-turbo")
    check("resident klein -> klein (stylize)", _resident(klein, "stylize")[0] == "flux2-klein")
    check("satisfied path returns no note", _resident(dev, "generate")[1] is None)

    # verb missing -> escalate; tier brings a capable model up, note relayed
    m, note = _resident(
        {"active_model": "gen-only", "serveable": True, "capabilities": ["generate"], "endpoint": "x"},
        "edit",
        swap={"ok": True, "active_model": "flux2-klein", "note": "loaded flux2-klein instead"},
    )
    check("escalation swaps to a capable model", m == "flux2-klein")
    check("escalation relays the tier note", note == "loaded flux2-klein instead")

    # verb missing and the tier can't provide it -> ToolUnsupported
    try:
        _resident({"active_model": "gen-only", "serveable": True, "capabilities": ["generate"],
                   "endpoint": "x"}, "edit",
                  swap={"ok": False, "error": "no capable image model fits the headroom"})
        check("tier can't provide the verb -> raises", False)
    except tools.workflows.ToolUnsupported as e:
        check("tier can't provide the verb -> raises", "headroom" in str(e))

    try:
        _resident(None, "generate", swap={"ok": False, "error": "no image tier"})
        check("no image engine + no swap -> raises", False)
    except tools.workflows.ToolUnsupported:
        check("no image engine + no swap -> raises", True)

    # -- image_model param: loose-name resolution (_resolve_pipeline) ----------
    runtime.bind(_FakeMgr(dev), None, None)
    check("exact name resolves", tools._resolve_pipeline("flux2-klein")[0] == "flux2-klein")
    check("case/sep insensitive", tools._resolve_pipeline("Flux2 Klein")[0] == "flux2-klein")
    check("substring: 'klein'", tools._resolve_pipeline("klein")[0] == "flux2-klein")
    check("substring: 'turbo'", tools._resolve_pipeline("turbo")[0] == "flux2-dev-turbo")
    check("token match: 'flux dev turbo'", tools._resolve_pipeline("flux dev turbo")[0] == "flux2-dev-turbo")
    check("substring: 'ideogram'", tools._resolve_pipeline("ideogram")[0] == "ideogram4")
    name, names = tools._resolve_pipeline("wan2.2")
    check("no match -> (None, full list)", name is None and "flux2-klein" in names)
    check("empty -> (None, list)", tools._resolve_pipeline("")[0] is None)

    # -- _select_pipeline: empty passthrough / swap / bad name ----------------
    runtime.bind(_FakeMgr(klein, swap={"ok": True, "active_model": "flux2-dev-turbo",
                                       "note": "using flux2-klein instead"}), None, None)
    err, note = asyncio.run(tools._select_pipeline(""))
    check("empty image_model -> no-op", err is None and note is None)
    err, note = asyncio.run(tools._select_pipeline("dev turbo"))
    check("resolvable image_model -> swap, note relayed", err is None and note == "using flux2-klein instead")
    err, note = asyncio.run(tools._select_pipeline("stable-diffusion-1.5"))
    check("unresolvable image_model -> error json with the list",
          err is not None and "available" in err and "flux2-klein" in err)

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

    # -- timing metrics in the success JSON --------------------------------------
    tm = tools._timing(100.0, 101.0, 105.5)          # no rewrite
    check("_timing: generate_s span", tm["generate_s"] == 4.5 and "rewrite_s" not in tm)
    tm = tools._timing(100.0, 101.0, 105.0, rewrite_s=0.8)
    check("_timing: rewrite_s included when nonzero", tm["rewrite_s"] == 0.8)
    body = json.loads(tools._success_response("d", ["u"], timing={"total_s": 9.1, "generate_s": 8.0}))
    check("_success_response carries timing", body["timing"]["generate_s"] == 8.0)

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
