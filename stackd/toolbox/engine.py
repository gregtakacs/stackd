"""M1: the REAL ComfyUI render + cancel + source/artifact seams for the toolbox jobs.

This is the only file in the package that imports httpx / pillow / the imagegen clients,
and it does so LAZILY (inside functions) for two hard reasons:

  1. `httpx`/`mcp` are the optional [imagegen] extra — `import stackd.serve` and the
     whole stdlib smoke suite must stay importable on a host that lacks them. The toolbox
     itself (tokens/masks/web/api/jobs) is stdlib-only and ships in the base install; only
     the live render needs the extras, so only this module's bodies need them.
  2. The imagegen clients + runtime are bound lazily (runtime.bind() runs inside
     `stackd serve`); importing them at module load would freeze an unbound handle.

The GPU-touching operations are exported as plain functions with the EXACT shape
jobs.JobQueue expects (render/cancel) so the queue is driven by the suite with fakes and
by serve.py with these — one swappable seam, per jobs.py's contract. No GPU here means no
test coverage either, so every branch that can fail does so into a jobs.* exception the
worker turns into an honest error row, never a silent stall.

Attribution mirrors imagegen/tools.py: the artifact is saved under the CALLING user's own
Open WebUI key (runtime.resolve_user_key(email)); the token's email is the identity. There
is deliberately no shared-key fallback — a file must belong to who made it.
"""

from __future__ import annotations

import asyncio
import base64
import uuid

from stackd.toolbox import graphs as _graphs
from stackd.toolbox import jobs as _jobs

# The verb the elastic image tier must satisfy for a masked edit to be serveable.
EDIT_CAPABILITY = "edit"
_DEFAULT_MODEL = "flux2-klein"


def _runtime():
    from stackd.imagegen import runtime          # lazy: needs the imagegen extra
    return runtime


def comfyui_base() -> tuple[str | None, str]:
    """(endpoint, note) for the resident image ComfyUI, or (None, why-not)."""
    return _runtime().comfyui_endpoint()


def image_engine_up() -> bool:
    """Cheap truth for Toolbox(image_engine_up=...): is a serveable image ComfyUI up?"""
    try:
        return comfyui_base()[0] is not None
    except Exception:  # noqa: BLE001 — anything the Manager raises means 'not up'
        return False


def prewarm_edit() -> dict:
    """Bring an edit-capable model resident when an editor opens, so the first render does
    not pay the model-swap cold start. Best-effort: a failure here is NOT a render failure
    (the model may already be resident, or fit later) — we only surface it, never raise."""
    try:
        return _runtime().request_capability(EDIT_CAPABILITY) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def make_source():
    """The Toolbox `source(email, ref)` callable for the live daemon.

    `ref` is an OWU file path (/api/v1/files/<id>/content) that was resolved at LAUNCH and
    is echoed back by the editor — never a caller-supplied arbitrary URL, matching the
    deny-by-default rule imagegen/tools.py enforces for edit sources. Fetched with the
    caller's own OWU key so a token minted for A cannot read B's file.
    """
    def source(email: str, ref: str) -> bytes | None:
        if not email or not ref:
            return None
        from stackd.imagegen import openwebui_client
        key = _runtime().resolve_user_key(email)
        if not key:
            return None
        try:
            return asyncio.run(openwebui_client.fetch_image(str(ref), key))
        except Exception:  # noqa: BLE001 — an unresolvable source is 'None', not a crash
            return None
    return source


def _pick_model(spec: dict) -> str:
    """The pipeline to run: an explicit spec model, else whatever the tier has resident,
    else the klein default that is guaranteed to carry a builtin inpaint graph."""
    m = (spec or {}).get("model")
    if m:
        return str(m)
    try:
        cap = _runtime().image_capability() or {}
    except Exception:  # noqa: BLE001
        cap = {}
    return cap.get("active_model") or _DEFAULT_MODEL


def _patch_graph(graph: dict, spec: dict, *, source_filename: str, mask_filename: str,
                 w: int, h: int, seed: int) -> dict:
    """Point the derived painted-mask graph at this job's uploaded files + parameters.
    The LOAD_IMAGE (photo), the mask filename on the LoadImageMask node, the
    PrimitiveString front-ends (prompt / width / height / seed) and the KSampler
    conditioning (per the mode, see _apply_mode) change -- the rest of the MASK_*
    scaffold stays put, which is what keeps workflows' id-based rewiring valid."""
    from stackd.imagegen import workflows as wf
    graph[_graphs.LOAD_IMAGE_NODE]["inputs"]["image"] = source_filename
    graph[_graphs.SEGMENT_NODE]["inputs"]["image"] = mask_filename
    wf.set_node(graph, wf.MASK_POSITIVE_NODE, "positive", str((spec or {}).get("prompt") or ""))
    wf.set_node(graph, wf.MASK_WIDTH_NODE, "width", int(w))
    wf.set_node(graph, wf.MASK_HEIGHT_NODE, "height", int(h))
    wf.set_node(graph, wf.MASK_SEED_NODE, "seed", int(seed))
    _apply_mode(graph, spec)
    return graph


def _apply_mode(graph: dict, spec: dict) -> None:
    """The mode dropdown is NOT cosmetic: it selects the KSampler conditioning, so the
    two modes genuinely produce different images. The region is ALWAYS the painted mask
    (LoadImageMask) in both -- this only changes WHAT the region is regenerated toward.

    'edit' (default) keeps the scene-referenced chain (KSampler positive -> node 23, the
    ReferenceLatent that conditions on a pass built from the WHOLE original image). That
    second pass is what makes surface edits -- recolor, a different shirt, an accessory --
    land and blend into the surroundings, so it is the default for most edits.

    'replace' drops that full-image pass (nodes 21 + 23) and conditions the KSampler on the
    masked-region latent alone (node 22). Without the whole-image pass the model is no
    longer conditioned on the very thing being changed away from, so the prompt can do a
    genuine identity-level swap or removal of the masked region. This mirrors imagegen's
    own _submit_edit preserve_scene_context=False branch -- the same replacement the OWUI
    edit_image tool performs -- but bound to the PAINTED mask rather than a text
    segmentation, so it acts only where the user painted.

    Unknown / absent kind keeps the scene-referenced 'edit' behaviour (never a silent
    no-op mode, and never a path the labels do not advertise)."""
    from stackd.imagegen import workflows as wf
    kind = str((spec or {}).get("kind") or "edit").lower()
    if kind != "replace":
        return
    graph[wf.MASK_KSAMPLER_NODE]["inputs"]["positive"] = [wf.MASK_REGION_REFLATENT_NODE, 0]
    graph.pop(wf.MASK_FULL_REFLATENT_NODE, None)
    graph.pop(wf.MASK_FULL_ENCODE_NODE, None)


def _graph_mask(mask: bytes) -> bytes:
    """Invert the painted mask's alpha for the ComfyUI graph -- a polarity adapter, not a
    mask change. The server represents the selection as 255 = edit-here (masks.
    extract_coverage), which is what the preview and the coverage the user sees are built
    from. But the graph's mask convention is the OPPOSITE: VAEEncodeForInpaint and
    ImageCompositeMasked edit where the mask is 0 -- the convention the canonical CLIPSeg
    path relies on (OWUI's edit_image removes the segmented object, which is the 0 side).
    Feeding the server's 255=edit mask straight in makes the graph edit the COMPLEMENT of
    the paint: the user paints a person and the background is replaced instead. In Edit
    mode that inversion is invisible (the background is regenerated to look like the
    background); in Replace mode it is glaring.

    Flip the alpha HERE, at the graph boundary, not in masks.normalize -- the preview and
    coverage keep their correct 255=edit-here meaning, and the guarded graph scaffold is
    untouched. The editor's invert toggle composes on top: it inverts the server mask
    first, so "edit everything outside the paint" still selects the complement."""
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(mask)).convert("RGBA")
    im.putalpha(im.getchannel("A").point(lambda p: 255 - p))
    out = io.BytesIO(); im.save(out, format="PNG")
    return out.getvalue()


def render_size(job: dict) -> tuple[int, int, bool]:
    """(w, h, was_capped) — the size a job is ALLOWED to run at, enforced at the one place
    that actually hands pixels to ComfyUI.

    `api._dims()` applies the same ceiling when the row is created, so this is normally a
    no-op; it is the belt for a row that predates the ceiling, a create path that bypassed
    _dims, or a ceiling that moved while the job sat in the queue. Returning the flag (not
    silently resizing) is what lets comfy_render rescale the mask to match — source and
    mask must disagree by exactly nothing. See masks.RENDER_MAX_SIDE for the measurement.
    """
    from stackd.toolbox import masks as _m
    try:
        w = int((job or {}).get("working_w") or 0) or 1024
        h = int((job or {}).get("working_h") or 0) or 1024
    except (TypeError, ValueError):
        w = h = 1024          # a junk row is sized, not fatalised, at the handover
    cw, ch = _m.fit_within(w, h, _m.RENDER_MAX_SIDE)
    return cw, ch, (cw, ch) != (w, h)


def comfy_render(job: dict, source: bytes, mask: bytes, *, on_prompt_id=None):
    """jobs.JobQueue render seam. Returns (artifact_png_b64, "image/png") after ALSO
    saving the result to the calling user's OWU (so the chat/standalone mounts have a
    durable copy that becomes the source of the next edit). Raises jobs.NoEngine /
    jobs.UserNotRegistered / TimeoutError / anything else — the worker turns each into an
    honest error row.

    Runs the imagegen async clients on a private loop via asyncio.run: the worker thread
    has no loop of its own, and a fresh one per render keeps a hung ComfyUI from stalling
    the shared MCP/daemon loop."""
    from stackd.imagegen import comfyui_client, openwebui_client
    from stackd.toolbox import graphs as G

    email = (job or {}).get("email") or ""
    spec = (job or {}).get("spec") or {}
    w, h, resized = render_size(job)
    if resized:
        # The row disagreed with the ceiling (it predates it, or the ceiling moved while
        # the job sat in the queue). Rescale the mask GEOMETRICALLY-FREE — resize_canonical
        # applies no grow/feather, because normalize_layers already applied each object's
        # geometry exactly once and applying it twice is the double-grow bug this package
        # has already fixed once. Source and mask must disagree by exactly nothing.
        from stackd.toolbox import masks as _m
        mask = _m.resize_canonical(mask, w, h)
    seed = int(spec.get("seed") if spec.get("seed") is not None else -1)
    if seed < 0:
        seed = uuid.uuid4().int % (2 ** 32 - 1)

    base, note = comfyui_base()
    if not base:
        raise _jobs.NoEngine(note or "no serveable image engine right now")
    key = _runtime().resolve_user_key(email)
    if not key:
        raise _jobs.UserNotRegistered(
            f"no Open WebUI key is on record for {email or '(unknown user)'} — the edit "
            "cannot be saved; register at the /register page and retry")

    model = _pick_model(spec)
    try:
        graph = G.painted_mask_graph(model)
    except G.GraphUnavailable as e:
        raise _jobs.NoEngine(f"the model {model!r} has no painted-mask graph: {e}") from e
    problems = G.validate_painted_mask_graph(graph)
    if problems:
        # A drifted scaffold would edit the wrong region on paid GPU time — refuse loudly.
        raise RuntimeError("painted-mask graph is not valid: " + "; ".join(problems))

    async def _run() -> tuple:
        # Unique filename per upload: a fixed name with overwrite=true meant two renders
        # close together could clobber each other's source before LoadImage read it
        # (confirmed in comfyui_client's own comment). Same rule applies to source+mask.
        src_b = _prepare_source(source, w, h)
        src_name = await comfyui_client.upload_to_comfy(src_b, "toolbox_src", base=base)
        # _graph_mask flips the alpha to the graph's mask convention (edit where mask is 0);
        # see that function for why the server mask and the graph mask are opposite.
        msk_name = await comfyui_client.upload_to_comfy(_graph_mask(mask), "toolbox_mask", base=base)
        _patch_graph(graph, spec, source_filename=src_name, mask_filename=msk_name,
                     w=w, h=h, seed=seed)
        prompt_id = await comfyui_client.submit_workflow(graph, base=base)
        if on_prompt_id is not None:
            on_prompt_id(prompt_id, base)        # so a cancel can interrupt the wait below
        by_node = await comfyui_client.wait_and_fetch(prompt_id, {G.SAVE_NODE}, base=base)
        images = by_node.get(G.SAVE_NODE) or []
        if not images:
            return None, None
        png = images[0]
        url = await openwebui_client.save_image(png, "toolbox-edit.png", key)
        return base64.b64encode(png).decode(), url

    art_b64, url = asyncio.run(_run())
    # Stash the OWU url on the job dict; the queue persists only (b64, type), and the url
    # is a convenience the poll response surfaces alongside the inline artifact.
    job["_artifact_url"] = url
    return art_b64, "image/png"


def _prepare_source(source: bytes, w: int, h: int) -> bytes:
    """Downscale the photo to the exact working size the graph runs at, so the source and
    the mask (already resampled to w×h in masks.normalize) are the SAME size when they meet
    in the graph. Pillow is present whenever a real render is (the [imagegen] extra)."""
    if not source:
        raise _jobs.NoEngine("the source image could not be resolved for this user")
    try:
        from stackd.imagegen import comfyui_client
        return comfyui_client.downscale_to_exact_size(source, w, h)
    except Exception:  # noqa: BLE001 — a bad downscale must not sink the render
        return source


def comfy_segment_click(source: bytes, points, negative_points=None, *,
                        threshold: float = 0.5, base: str | None = None,
                        max_side: int = 1024):
    """Click-to-object mask via ComfyUI's native SAM3_Detect node -- the ``click_segmenter``
    seam the Toolbox's /toolbox/mask/click route calls (mirrors the ``segmenter`` text seam).

    ``points`` / ``negative_points`` are lists of ``[x, y]`` normalised to 0..1 in the
    DISPLAYED photo's own space (the browser divides the click by its canvas natural size),
    so the caller never needs to know the server's working resolution: we choose a modest
    segmentation size here and scale the fractional clicks to exactly the image we upload.
    SAM3 resizes to 1008 internally regardless, so a big upload buys nothing -- cap the side
    to keep the per-click upload and the paid GPU step cheap.

    Returns (canonical_mask_png, info) in the load-stroke contract (RGBA, RGB white, ALPHA =
    selection -- what the editor's 'load' stroke and masks.normalize both speak), or
    (None, {"error": ...}) so the handler can 502 honestly. Never raises on GPU/network
    trouble: like every other engine seam, a failure is an honest error row, not a stall.
    """
    from stackd.imagegen import comfyui_client
    from stackd.toolbox import graphs as G

    if not source:
        return None, {"error": "the source image could not be resolved for this user"}
    pts = _norm_points(points)
    negs = _norm_points(negative_points)
    if not pts:
        return None, {"error": "no click was given to select on"}

    if base is None:
        base, note = comfyui_base()
        if not base:
            return None, {"error": note or "no serveable image engine right now"}

    try:
        w, h = _seg_size(source, max_side)
    except Exception as e:  # noqa: BLE001 — a bad source is an honest error, not a crash
        return None, {"error": f"could not size the photo for segmentation ({e.__class__.__name__}: {e})"}

    async def _run():
        src_b = comfyui_client.downscale_to_exact_size(source, w, h)
        src_name = await comfyui_client.upload_to_comfy(src_b, "toolbox_seg_src", base=base)
        pos = [{"x": _q(px * w), "y": _q(py * h)} for (px, py) in pts]
        neg = [{"x": _q(px * w), "y": _q(py * h)} for (px, py) in negs]
        graph = G.sam3_segment_graph(source_filename=src_name, points=pos,
                                      negative_points=neg, threshold=threshold)
        problems = G.validate_sam3_segment_graph(graph)
        if problems:
            # A mis-wired segmentation graph returns the wrong pixels on paid GPU time; refuse.
            raise _jobs.NoEngine("sam3 graph is not valid: " + "; ".join(problems))
        pid = await comfyui_client.submit_workflow(graph, base=base)
        by_node = await comfyui_client.wait_and_fetch(pid, {G.SEG_SAVE}, base=base)
        return (by_node.get(G.SEG_SAVE) or [None])[0]

    try:
        raw = asyncio.run(_run())
    except Exception as e:  # noqa: BLE001 — surface every GPU/network failure honestly
        return None, {"error": f"segmentation failed ({e.__class__.__name__}: {e})"}
    if not raw:
        return None, {"error": "the segmentation engine returned no mask"}
    # The graph saved a plain black/white mask image. Before anything else, drop the
    # disconnected speckles SAM3 scatters around the real object (issue: "the selection has
    # to remain contiguous"): they are a handful of pixels each, and a later grow inflates
    # every one into a visible square. Keep substantial islands (a deliberately shift-clicked
    # hat/backpack survives), drop only decoder noise. Doing it HERE, at the source, means
    # the on-screen stroke the browser stores AND the mask the render re-normalises are both
    # already clean, so growth can never re-inflate a speckle.
    try:
        from stackd.toolbox import masks as _m
        raw = _m.keep_significant_components(raw)
        canon, info = _m.normalize(raw, w, h, binary=True)
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"could not canonicalize the mask ({e.__class__.__name__}: {e})"}
    return canon, info


def _norm_points(points):
    """Coerce a browser point list to [(x, y)] clamped to 0..1. Rejects junk entries
    rather than trusting them: a stray non-number must not poison the whole click request
    with a 500, it is simply dropped (an empty result makes the handler 400)."""
    out = []
    for p in (points or []):
        try:
            if isinstance(p, dict):
                x, y = p["x"], p["y"]
            else:
                x, y = p[0], p[1]
            x = max(0.0, min(1.0, float(x)))
            y = max(0.0, min(1.0, float(y)))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        out.append((x, y))
    return out


def _seg_size(source: bytes, max_side: int) -> tuple[int, int]:
    """Working size for the segment graph — ONE rule with the render path, via
    masks.fit_within (aspect preserved, long side capped, both sides on the 16-px grid,
    min 64). The old body claimed "round to 16" in its docstring and never did it, so the
    mask SAM3 returned could be a non-multiple of 16 while the render canvas was — the
    browser resamples between the two, but agreement is free here and drift is not.

    max_side deliberately defaults to 1024 in comfy_segment_click rather than following
    masks.RENDER_MAX_SIDE: SAM3 resizes to 1008 internally, so a bigger upload buys
    nothing at all and costs an upload plus a paid GPU step every single click.
    """
    from stackd.toolbox import masks as _m
    w, h = _m.image_size(source)
    if w <= 0 or h <= 0:
        raise ValueError("image has no size")
    return _m.fit_within(w, h, max_side)



def _q(v: float) -> int:
    return int(round(v))


def comfy_cancel(prompt_id: str, base: str) -> None:
    """jobs.JobQueue cancel seam: best-effort interrupt of ONE running prompt. Mirrors
    imagegen/bench.py's _abort_queue but targeted — we interrupt only the prompt id this
    job recorded, so a concurrent user's render is left alone. Never raises: the queue logs
    and treats a failed interrupt as best-effort (the row is already marked cancelled)."""
    if not prompt_id or not base:
        return
    from stackd.imagegen import comfyui_client

    async def _go():
        b = base.rstrip("/")
        try:
            await comfyui_client.post_json(f"{b}/interrupt", {"prompt_id": prompt_id}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    try:
        asyncio.run(_go())
    except Exception:  # noqa: BLE001
        pass

