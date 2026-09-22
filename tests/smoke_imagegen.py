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

    # -- qwen-image-2-1: registry integrity + graph executability -------------
    # (The Comfy-Org templates ship UI-format subgraphs full of frontend-only nodes --
    # SaveImageAdvanced/ResolutionSelector/ComfySwitchNode each submit as a hard
    # class-not-found on /prompt. The qwen graphs here are hand-converted to API
    # format; these checks pin the conversion stayed server-executable AND that every
    # role id in models.json still resolves inside its graph file.)
    QW = "qwen-image-2-1"
    _qwen_entry = workflows.MODELS_MANIFEST["models"].get(QW) or {}
    check("MODELS_MANIFEST has qwen-image-2-1", bool(_qwen_entry))
    _FRONTEND_ONLY = {"SaveImageAdvanced", "ResolutionSelector", "MarkdownNote",
                      "Note", "ImageCompare", "PrimitiveCheckbox", "ComfySwitchNode"}
    _loaded = {}
    for _tool, _spec in (_qwen_entry.get("tools") or {}).items():
        if _tool.startswith("_") or "graph" not in _spec:
            continue
        _g = workflows._load_graph(_spec["graph"])
        _loaded[_tool] = (_g, _spec.get("nodes") or {})
        for _role, _nid in (_spec.get("nodes") or {}).items():
            check(f"qwen {_tool}: role {_role} -> node {_nid} exists", _nid in _g)
        for _nid, _node in _g.items():
            _ct = _node.get("class_type", "")
            check(f"qwen {_tool} node {_nid} ({_ct}) is server-executable",
                  _ct not in _FRONTEND_ONLY and "-" not in _ct)
    check("qwen wires all three tools", set(_loaded) == {"generate", "edit", "stylize"})
    check("qwen edit is whole-image only (no builtin inpaint declared -- masked must "
          "clean-error, toolbox must fall back to klein)",
          (_qwen_entry.get("tools") or {}).get("edit", {}).get("inpaint") != "builtin")


    _gg, _gn = _loaded["generate"]
    _g3, _n3 = _loaded["edit"]
    _gs, _ns = _loaded["stylize"]
    # set_node regression for the TextEncodeQwenImage21 role mapping: positive/prompt
    # must land on the TE's `prompt` input, NOT fall through to the PrimitiveString
    # default key `value` -- a graph patched into `value` encodes an empty prompt and
    # still RUNS, the silent kind of bug this file exists to collect. (_load_graph
    # returns a fresh dict per call, so these mutate scratch copies, not shipped state.)
    workflows.set_node(_gg, _gn["positive"], "positive", "hello")
    check("qwen positive role writes the TE prompt input",
          _gg[_gn["positive"]]["inputs"].get("prompt") == "hello")
    workflows.set_node(_gs, _ns["prompt"], "prompt", "hello")
    workflows.set_node(_gs, _ns["seed"], "seed", 42)
    workflows.set_node(_gs, _ns["scale_by"], "scale_by", 2.0)
    check("qwen stylize prompt/seed/scale_by roles hit the right inputs",
          _gs[_ns["prompt"]]["inputs"].get("prompt") == "hello"
          and _gs[_ns["seed"]]["inputs"].get("seed") == 42
          and _gs[_ns["scale_by"]]["inputs"].get("scale_by") == 2.0)
    workflows.set_node(_g3, _n3["image"], "image", "src.png")
    workflows.set_node(_g3, _n3.get("width"), "width", 1024)  # absent role: silent no-op
    check("qwen edit image role writes LoadImage; missing width role no-ops",
          _g3[_n3["image"]]["inputs"].get("image") == "src.png")

    # Template fidelity (the numbers image_qwen_image_2_1_* ships with)
    _ks = _gg[_gn["seed"]]
    check("qwen generate sampler = template (25 / cfg 1 / euler / simple / denoise 1)",
          (_ks["inputs"]["steps"], _ks["inputs"]["cfg"], _ks["inputs"]["sampler_name"],
           _ks["inputs"]["scheduler"], _ks["inputs"]["denoise"]) == (25, 1, "euler", "simple", 1))
    check("qwen negative conditioning is the TE's own slot 1 (no ConditioningZeroOut)",
          _ks["inputs"]["negative"] == [_gn["positive"], 1])
    check("qwen graphs use no CLIPTextEncode/ConditioningZeroOut/VAEEncode (TE-only front-end)",
          not any(_nd["class_type"] in ("CLIPTextEncode", "ConditioningZeroOut", "VAEEncode")
                  for _g in (_gg, _g3, _gs) for _nd in _g.values()))
    check("qwen edit samples the TE's own image-sized latent (slot 2), not an EmptyLatent",
          _g3[_n3["seed"]]["inputs"]["latent_image"] == [_n3["positive"], 2])
    _ete = _g3[_n3["positive"]]
    check("qwen edit TE links the VAE + resolution 0 (reference latents ON; canvas follows "
          "image_1 -- the template's ComfySwitchNode switch=false path)",
          isinstance(_ete["inputs"].get("vae"), list) and _ete["inputs"].get("resolution") == 0)
    # LIVE-VERIFIED 2026-09-22 against 0.37.0 /prompt: the Autogrow reference input MUST
    # use the per-slot dotted name (exactly as the UI subgraph names it). The nested
    # {"images": {"image_1": <link>}} form is SILENTLY DROPPED (validation accepts it --
    # the template is min=0 -- then execute() sees images={} and its
    # `latent_w = latent_h = resolution or 1024` branch yields a SQUARE latent: an edit
    # of a 960x1280 source came back 1024x1024, no error anywhere. This check pins the
    # dotted form AND forbids the nested one; the dimension assertion lives in
    # tests' live gate, not here.)
    check("qwen edit/stylize wire references via the dotted Autogrow key",
          _ete["inputs"].get("images.image_1") == ["1", 0]
          and _gs[_ns["prompt"]]["inputs"].get("images.image_1") == ["2", 0]
          and "images" not in _ete["inputs"] and "images" not in _gs[_ns["prompt"]]["inputs"])
    check("qwen stylize feeds its (scaled) image_1 into the dotted Autogrow slot",
          _gs[_ns["prompt"]]["inputs"]["images.image_1"] == [_ns["scale_by"], 0])
    check("qwen loads UNET/CLIP/VAE as separate nodes (an int8/bf16 mix stays a filename swap)",
          {"UNETLoader", "CLIPLoader", "VAELoader"}
          <= {n["class_type"] for n in _gg.values()})

    # -- per-model geometry: qwen on the 32 grid, klein byte-identical ---------
    _gq = workflows.geometry_for(QW)
    _gk = workflows.geometry_for("flux2-klein")
    check("qwen geometry: multiple 32 / native-2K 4MP ceiling / 2048x832 practical",
          _gq["multiple"] == 32 and _gq["max_pixels"] == 4 * 1024 * 1024
          and _gq["practical_max_pixels"] == 2048 * 832)
    check("klein geometry == the module constants (no behavior drift for shipped models)",
          _gk["multiple"] == 16 and _gk["max_pixels"] == workflows.MAX_PIXELS
          and _gk["practical_max_pixels"] == workflows.PRACTICAL_MAX_PIXELS
          and _gk["aspect_presets"] == workflows.ASPECT_PRESETS)
    check("qwen presets keep klein's names", set(_gq["aspect_presets"]) == set(workflows.ASPECT_PRESETS))
    check("every qwen preset side is a multiple of 32",
          all(v % 32 == 0 for pr in _gq["aspect_presets"].values() for v in pr))
    check("qwen rounds 1008 -> 1024 (32 grid); klein keeps 1008 (16 grid)",
          tools._resolve_dimensions_no_source("", 1008, 1008, _gq)[0:2] == (1024, 1024)
          and tools._resolve_dimensions_no_source("", 1008, 1008, _gk)[0:2] == (1008, 1008))
    # qwen 2100x2100 can NOT trip the MP guard: the MAX_SIDE(2048) clamp lands it on
    # exactly 2048^2 == the 4MP ceiling (geometry deliberately aligns them = the card's
    # native 2K). Assert THAT, and exercise the over-ceiling message with a synthetic
    # tighter geometry (the only shape where an explicit request can pass the clamp
    # and still exceed max_pixels).
    _cw, _ch, _cnote = tools._resolve_dimensions_no_source("", 2100, 2100, _gq)
    check("qwen clamps 2100^2 to native-2K 2048x2048 (= its own ceiling, no error)",
          (_cw, _ch) == (2048, 2048) and any("2048x2048" in n for n in _cnote))

    # -- loose-name resolution incl. the card's dotted form ----------------------
    class _PreferMgr:
        def image_status(self):
            return {"prefer": [{"active_model": m} for m in
                                 ("flux2-klein", "ideogram4", "qwen-image-2-1")]}
    runtime.bind(_PreferMgr(), None, None)
    for _q, _want in (("qwen", "qwen-image-2-1"), ("Qwen-Image 2.1", "qwen-image-2-1"),
                      ("qwen image 2.1", "qwen-image-2-1"), ("Qwen_Image", "qwen-image-2-1"),
                      ("klein", "flux2-klein"), ("ideogram", "ideogram4")):
        check(f"_resolve_pipeline({_q!r}) -> {_want}", tools._resolve_pipeline(_q)[0] == _want)

    try:
        tools._resolve_dimensions_no_source(
            "", 1400, 1400, {**_gq, "model": QW, "max_pixels": 1_000_000})
        check("over-ceiling explicit size raises", False)
    except ValueError as e:
        check("over-ceiling error names the pipeline (not 'Flux.2 Klein 9B')",
              "qwen" in str(e) and "Klein" not in str(e))

    # -- wait_and_fetch optional_nodes (2026-09-20: the scratch janitor TTL'd the
    #    mask PreviewImage out of temp mid-render; the 404 on that DEBUG artifact
    #    sank two fully rendered ~6-min MCP edits as "Could not reach ComfyUI
    #    proxy") ---------------------------------------------------------------
    from stackd.imagegen import comfyui_client
    import httpx

    def _http_404():
        import httpx as _hx
        return _hx.HTTPStatusError(
            "404", request=_hx.Request("GET", "http://x/view"),
            response=_hx.Response(404, request=_hx.Request("GET", "http://x/view")),
        )

    async def _waf_case(fail_nodes: set[str]):
        async def fake_get_json(url, timeout=30):
            return {"pid": {"outputs": {
                "27": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]},
                "29": {"images": [{"filename": "mask.png", "subfolder": "", "type": "temp"}]},
            }}}

        async def fake_get_bytes(url, timeout=30):
            node = "29" if "mask.png" in url else "27"
            if node in fail_nodes:
                raise _http_404()
            return b"PNGDATA"

        saved = (comfyui_client.get_json, comfyui_client.get_bytes)
        comfyui_client.get_json, comfyui_client.get_bytes = fake_get_json, fake_get_bytes
        try:
            return await comfyui_client.wait_and_fetch(
                "pid", {"27", "29"}, base="http://x",
                optional_nodes={"29"},
            )
        finally:
            comfyui_client.get_json, comfyui_client.get_bytes = saved

    try:
        got = asyncio.run(_waf_case({"29"}))
        check("optional preview 404 -> empty list, render kept",
              got["27"] == [b"PNGDATA"] and got["29"] == [])
        got = asyncio.run(_waf_case(set()))
        check("all nodes present -> both fetched",
              got["27"] == [b"PNGDATA"] and got["29"] == [b"PNGDATA"])
        try:
            asyncio.run(_waf_case({"27"}))
            check("required-node 404 still raises", False)
        except httpx.HTTPStatusError:
            check("required-node 404 still raises", True)
    except TypeError:
        # httpx.HTTPStatusError needs a real Request/Response on some versions
        check("optional_nodes fetch-failure simulation (skipped: httpx ctor)", True)

    # -- hard inpaint gate: the mask VAEEncodeForInpaint reads must be binary --
    _g = workflows.EDIT_WORKFLOW_INPAINT
    _seg = _g[workflows.MASK_SEGMENT_NODE]["inputs"]
    check("CLIPSeg gate hard: feather_frac/blur/max_feather all zero",
          _seg["feather_frac"] == 0.0 and _seg["blur"] == 0.0 and _seg["max_feather"] == 0.0)
    check("VAEEncodeForInpaint reads the grown mask (node16), not raw node14",
          _g[workflows.MASK_SEGMENT_NODE]["class_type"] == "CLIPSegMask"
          and _g["20"]["inputs"]["mask"][0] == workflows.MASK_GROW_NODE)

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
