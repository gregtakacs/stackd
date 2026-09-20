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
import json
import os
import time
import uuid

from stackd.toolbox import graphs as _graphs
from stackd.toolbox import jobs as _jobs

# The verb the elastic image tier must satisfy for a masked edit to be serveable.
EDIT_CAPABILITY = "edit"
_DEFAULT_MODEL = "flux2-klein"

# ---- how long a toolbox render may take, and what happens when it does not -------
# Deliberately SEPARATE from imagegen's config.TIMEOUT_S. The toolbox runs crop-bounded
# inpaints whose expected duration is a different distribution from a full generation, and
# inheriting one shared number is not hypothetical: the resident-iGPU deployment ran with
# TIMEOUT_S=600 (set in AI-STACK/docker-compose.yml, not in the image), and a render that
# needed ~616 s was reported to the user as a failure while the GPU went on finishing an
# image nobody received. Two separate knobs, because they are two separate questions:
# when do we stop waiting, and do we bother to look after we stop.
RENDER_TIMEOUT_S = int(os.getenv("TOOLBOX_RENDER_TIMEOUT_S", "900") or 900)
# After the deadline, keep watching /history quietly for this long BEFORE interrupting.
# The render that dies at 600 s was 16 s from done; interrupting at the deadline throws
# away a finished image on the far side of a rounding of the clock.
RENDER_GRACE_S = int(os.getenv("TOOLBOX_RENDER_GRACE_S", "45") or 45)
# /queue is polled at most this often while waiting, so a progress line costs one small
# request per few seconds rather than one per history poll.
PROGRESS_THROTTLE_S = 5.0


def render_deadline_s() -> int:
    """The deadline handed to ComfyUI for one render, in seconds. Kept as a function so a
    test (or a future per-job override) can see the same number the seam uses."""
    return RENDER_TIMEOUT_S


def _stage_from_queue(q, prompt_id: str) -> dict:
    """The pure half of progress_of: /queue's JSON -> a stage. Separate so the branch logic
    is testable without a network, and so the async and sync callers cannot drift."""
    def ids(entries):
        # /queue entries are [number, prompt_id, extra_data, outputs, prompt]
        return [e[1] for e in (entries or []) if isinstance(e, (list, tuple)) and len(e) > 1]

    running, pending = ids(q.get("queue_running")), ids(q.get("queue_pending"))
    if prompt_id in running:
        return {"stage": "running", "ahead": 0}
    if prompt_id in pending:
        return {"stage": "queued", "ahead": pending.index(prompt_id)}
    return {"stage": "vanished", "ahead": 0, "queued_total": len(pending)}


async def progress_of_async(client, base: str, prompt_id: str) -> dict:
    """Awaitable progress peek, for use INSIDE the render's event loop.

    This exists as a separate function because asyncio.run() cannot be called from within a
    running loop — and the seam that needs progress is exactly that: a callback firing from
    the coroutine that is already awaiting ComfyUI. Getting this wrong fails the render, not
    just the progress line, so the two callers are deliberately different functions.
    """
    try:
        q = await client.get_json(f"{base.rstrip('/')}/queue", timeout=5)
    except Exception:  # noqa: BLE001 — progress is advisory
        return {}
    if not isinstance(q, dict):
        return {}
    return _stage_from_queue(q, prompt_id)


def progress_of(base: str, prompt_id: str) -> dict:
    """Sync progress peek for callers OUTSIDE an event loop (tests, the CLI)."""
    try:
        import httpx
    except Exception:  # noqa: BLE001 — no httpx means no progress, never a failed render
        return {}

    async def _go():
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{base.rstrip('/')}/queue")
            r.raise_for_status()
            return r.json()

    try:
        q = asyncio.run(_go())
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(q, dict):
        return {}
    return _stage_from_queue(q, prompt_id)


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
    # The prompt arrives ALREADY CAPTIONED: api._create ran it through
    # stackd.imagegen.captioning (the same reduction imagegen.tools' masked edit_image
    # applies) before persisting the row, so the graph executes exactly the stored
    # prompt and this layer does NO text handling of its own. Drift between the two
    # masked paths is the bug this pin exists to prevent.
    wf.set_node(graph, wf.MASK_POSITIVE_NODE, "positive", str((spec or {}).get("prompt") or ""))
    wf.set_node(graph, wf.MASK_WIDTH_NODE, "width", int(w))
    wf.set_node(graph, wf.MASK_HEIGHT_NODE, "height", int(h))
    wf.set_node(graph, wf.MASK_SEED_NODE, "seed", int(seed))
    _apply_mode(graph, spec)
    _apply_mask_geometry(graph, spec)
    return graph


def _apply_mask_geometry(graph: dict, spec: dict) -> None:
    """OVERRIDE the scaffold's CLIPSeg-era mask-branch defaults — the "mask was way bigger
    than what I painted" fix (live: a 1.8% paint in Replace mode came back with half the
    dog repainted, because the scaffold still carried expand=12 / blur 28 / clamp-grow 28
    / grow_mask_by=6, the same constants imagegen's _submit_edit had to ABANDON for
    exactly this defect: "a fixed 28 px here is what made small objects balloon to fill an
    oversized mask").

    A PAINTED mask needs none of it: masks.normalize_layers already applied each object's
    grow/shrink/feather exactly once, and re-growing in the graph dilates the user's
    intent (and, in Replace mode, paints the prompt across the dilated ring). So the
    composite honours the silhouette AS PAINTED — the soft-edge chain (31→32→33→34→35) is
    deleted and both consumers (preview node 28, composite node 30) take the mask straight
    from node 16, which now passes it through (expand 0). Two spec knobs remain honest:

      mask_expand   >0 re-grows node 16 (and grow_mask_by with it) — the documented
                    "expand the paint region" affordance the UI advertises.
      mask_edge     >0 rebuilds the soft-edge chain scaled from it, with the dilated
                    clamp imagegen's Bug 3 proved necessary (clamping against the RAW
                    mask would zero alpha in one pixel at the boundary).

    ColorMatchV2 (node 26, fed by the strength PrimitiveFloat node 7) is REMOVED from
    the painted path outright: it fitted the generated crop to the GLOBAL stats of the
    whole photo (measured +34 mean-abs pushed outside a hard-edited square), while the
    server-side paste_back already colour-matches the pasted region against the LOCAL
    ring pixels it must blend with — and spec.color_match drives THAT one, so 0 means
    off end to end.
    """
    from stackd.imagegen import workflows as wf
    try:
        expand = max(0, min(64, int((spec or {}).get("mask_expand") or 0)))
    except (TypeError, ValueError):
        expand = 0
    try:
        edge = max(0, min(64, int((spec or {}).get("mask_edge") or 0)))
    except (TypeError, ValueError):
        edge = 0

    graph[wf.MASK_GROW_NODE]["inputs"]["expand"] = expand
    # tapered_corners left EXACTLY as the scaffold ships it: at expand 0 it is inert
    # (nothing grows), and mutating an input the user never asked about is how a
    # "geometry" patch drifts into changing corner-rounding on someone's next request.

    # VAEEncodeForInpaint (node 20): its grow_mask_by silently dilates WHICH latents get
    # noise — the same balloon, second time. Follow the user's expand instead of the
    # scaffold's baked 6. (No named constant upstream; the node-id layout is the contract.)
    for nid, node in graph.items():
        if node.get("class_type") == "VAEEncodeForInpaint":
            node["inputs"]["grow_mask_by"] = expand

    comp = graph[wf.MASK_COMPOSITE_NODE]["inputs"]
    mt28 = graph.get("28")                              # MaskToImage feeding the preview
    if edge <= 0:
        # Hard composite straight from the painted silhouette (imagegen's edge_softness<=0
        # branch, same wiring). Deleting 34 also makes the node-28 preview show the REAL
        # gate the composite used — with the scaffold chain it previewed the blurred mask,
        # not the painted one.
        for nid in ("31", wf.MASK_BLUR_NODE, "33", "34", wf.MASK_CLAMP_MARGIN_NODE):
            graph.pop(nid, None)
        comp["mask"] = [wf.MASK_GROW_NODE, 0]
        if mt28 is not None:
            mt28["inputs"]["mask"] = [wf.MASK_GROW_NODE, 0]
    else:
        blur_radius = max(1, min(10, round(edge * 0.3)))
        bl = graph.get(wf.MASK_BLUR_NODE)
        if bl is not None:
            bl["inputs"]["blur_radius"] = blur_radius
            bl["inputs"]["sigma"] = max(0.1, min(10.0, blur_radius / 3))
        graph[wf.MASK_CLAMP_MARGIN_NODE]["inputs"]["expand"] = blur_radius
        # chain intact: 16→31→32→33→34(multiply, clamped against dilated 35)→30/28

    # The graph's ColorMatchV2 (node 26, strength node 7) fits the generated crop to the
    # GLOBAL stats of the whole scaled photo (node 13): measured a +34 mean-abs band
    # pushed into the ring just outside a hard-edited square, and it is a pass that can
    # never HELP the composite — the server's paste_back already colour-matches the
    # region it pastes, against the LOCAL ring pixels it will be seen next to. A global
    # affine mean/std transfer toward the untouched photo's palette IS the "blown-out
    # highlights" family of defect. So node 30 takes the VAEDecodeTiled output (25)
    # directly and the dead colour-match pair is dropped — ComfyUI never executes it.
    # The spec knob still rides along for honesty: color_match 0 also zeroes the server
    # pass (paste_back), and if the pair is ever rewired the node-7 value says what the
    # user chose.
    graph[wf.MASK_COMPOSITE_NODE]["inputs"]["source"] = [_graphs.DECODE_NODE, 0]
    graph.pop(_graphs.COLOR_MATCH_NODE, None)
    graph.pop(wf.MASK_COLOR_STRENGTH_NODE, None)


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


def comfy_render(job: dict, source: bytes, mask: bytes, *, on_prompt_id=None,
                 on_progress=None):
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
    from stackd.toolbox import masks as _m

    email = (job or {}).get("email") or ""
    spec = (job or {}).get("spec") or {}
    w, h, resized = render_size(job)
    if resized:
        # The row disagreed with the ceiling (it predates it, or the ceiling moved while
        # the job sat in the queue). Rescale the mask GEOMETRICALLY-FREE — resize_canonical
        # applies no grow/feather, because normalize_layers already applied each object's
        # geometry exactly once and applying it twice is the double-grow bug this package
        # has already fixed once. Source and mask must disagree by exactly nothing.
        mask = _m.resize_canonical(mask, w, h)
    seed = int(spec.get("seed") if spec.get("seed") is not None else -1)
    if seed < 0:
        seed = uuid.uuid4().int % (2 ** 32 - 1)

    # ---- crop-and-paste: render the selection, not the whole frame ------------------
    # At the render ceiling a 3%-coverage selection gets 3% of the latents, so the model
    # paints it soft — and because the artifact becomes the source of the NEXT edit, the
    # image ratchets down a little more every round. plan_crop confines the graph to the
    # selection plus a context ring (spending the canvas's whole budget where it is looked
    # at), and paste_back composites the result into the FULL-RESOLUTION photo, which is
    # the only place that detail still exists. The photo is needed at use time but NOT at
    # plan time: the box lives in canvas px and maps to source proportionally later, so a
    # stale or missing photo cannot silently move a crop.
    #
    # Unconditional, with CROP_AFFORDABLE as the escape hatch, rather than a user toggle:
    # whether cropping would help is a function of geometry the user cannot evaluate, and a
    # sixth knob is one more thing that can quietly stop doing anything. What the user does
    # get is provenance — crop_json states plainly that the artifact is a composite of the
    # model output and their own photo, not a pure model output.
    crop_plan = None
    try:
        crop_plan = _m.plan_crop(mask)
    except Exception:  # noqa: BLE001 — no plan means the old full-frame path, not a failed job
        crop_plan = None
    if crop_plan and max(crop_plan["size"]) > _m.RENDER_MAX_SIDE:
        # Cannot happen while the box is bounded by the working canvas, but the ceiling is
        # an invariant worth defending at the boundary rather than by argument. (The use-time
        # render_budget clamps to the grid-floored ceiling independently of this guard.)
        crop_plan = None

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
        if crop_plan:
            # USE-time budget (masks.render_budget): the box mapped into SOURCE pixels and
            # capped at the ceiling. The plan's canvas-space size would render a 4x photo
            # crop at a quarter of the detail it actually holds, and paste_back's upscale
            # would bake that softness into the artifact that becomes the NEXT edit's
            # source — the ratchet, smuggled back in one round trip at a time.
            try:
                rw, rh = _m.render_budget(crop_plan, _m.image_size(source))
            except Exception:  # noqa: BLE001 — an unmeasurable photo falls back to the plan,
                rw, rh = crop_plan["size"]  # and crop_for_render itself fails honestly if it
                                             # truly cannot decode the bytes
            src_b, _used = _m.crop_for_render(source, crop_plan, size=(rw, rh))
            msk_b = _m.crop_mask(mask, crop_plan, size=(rw, rh))
            # The photo is the paste base: it is the only thing that still holds the detail
            # outside the crop, so the artifact comes back at the PHOTO's resolution.
            # (paste_plan carries the RENDER size for provenance; paste_back only reads the
            # canvas box+frame, so recording size here is honesty in _crop_json, not geometry.)
            base_bytes, paste_plan = source, dict(crop_plan, size=(rw, rh))
        else:
            rw, rh = w, h
            src_b = _prepare_source(source, w, h)
            msk_b = mask
            # Full-frame: base at the graph's own size, NOT the raw photo. Upscaling a
            # whole-image 1024 render to 4000x3000 adds no information, multiplies the
            # artifact's bytes ~10x, and slows the OWU save — for nothing. Here the paste
            # pass exists to serve the compositing knobs, not to change resolution.
            base_bytes = src_b
            paste_plan = {"box": (0, 0, w, h), "frame": (w, h), "size": (rw, rh)}
        src_name = await comfyui_client.upload_to_comfy(src_b, "toolbox_src", base=base)
        # _graph_mask flips the alpha to the graph's mask convention (edit where mask is 0);
        # see that function for why the server mask and the graph mask are opposite.
        msk_name = await comfyui_client.upload_to_comfy(_graph_mask(msk_b), "toolbox_mask", base=base)
        _patch_graph(graph, spec, source_filename=src_name, mask_filename=msk_name,
                     w=rw, h=rh, seed=seed)
        prompt_id = await comfyui_client.submit_workflow(graph, base=base)
        if on_prompt_id is not None:
            on_prompt_id(prompt_id, base)        # so a cancel can interrupt the wait below

        # Progress, honestly. /history carries no percent-complete and ComfyUI exposes NO
        # HTTP route for step count — that lives only on /ws, whose client is not installed
        # in this image. So the user gets elapsed time plus a TRUE stage from /queue, and
        # never a fabricated percentage. The /queue peek is scheduled on the loop we are
        # already running on; asyncio.run() would raise from right here, and a crash in a
        # progress hook would take down a render that is otherwise succeeding.
        state = {"elapsed": 0.0, "peeked_at": 0.0, "stage": "waiting"}
        peeks = set()

        def _publish():
            job["_progress_json"] = json.dumps({
                "elapsed_s": round(state["elapsed"], 1), "stage": state["stage"],
                "ahead": state.get("ahead", 0), "render_size": [rw, rh]})
            if on_progress is not None:
                try:
                    on_progress(json.loads(job["_progress_json"]))
                except Exception:  # noqa: BLE001 — the seam's observer must never bite
                    pass

        async def _peek(pid, b):
            st = await progress_of_async(comfyui_client, b, pid)
            if st:
                state.update(stage=st["stage"], ahead=st.get("ahead", 0))
                _publish()

        def _on_poll(elapsed):
            state["elapsed"] = elapsed
            _publish()
            if time.monotonic() - state["peeked_at"] < PROGRESS_THROTTLE_S:
                return
            state["peeked_at"] = time.monotonic()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:      # no loop => no stage peek, elapsed still advances
                return
            t = loop.create_task(_peek(prompt_id, base))
            peeks.add(t)
            t.add_done_callback(peeks.discard)

        try:
            by_node = await comfyui_client.wait_and_fetch(
                prompt_id, {G.SAVE_NODE}, base=base,
                timeout_s=RENDER_TIMEOUT_S, on_poll=_on_poll)
        except TimeoutError as first:
            # Past the deadline, in this order: keep watching quietly for a little longer
            # (the render that "timed out at 600s" was 16s from finishing, and interrupting
            # at the deadline throws that image away), THEN interrupt so the GPU is not left
            # painting an image nobody is waiting for, THEN one last look in case the output
            # landed during the interrupt round trip.
            by_node = await _harvest_after_deadline(comfyui_client, prompt_id, base)
            if not by_node:
                await _interrupt_prompt(comfyui_client, prompt_id, base)
                by_node = await _harvest_once(comfyui_client, prompt_id, base)
            if not by_node:
                raise TimeoutError(
                    f"gave up after {RENDER_TIMEOUT_S}s plus a {RENDER_GRACE_S}s grace, and "
                    f"interrupted the prompt so the GPU is free ({first})")
        # Drop any /queue peek still in flight: leaving one running when the render is over
        # logs "Task was destroyed but it is pending!" against every render on the box, and
        # noise like that is how a real warning gets learned to be ignored.
        for _t in peeks:
            if not _t.done():
                _t.cancel()
        images = by_node.get(G.SAVE_NODE) or []
        if not images:
            return None, None
        png = images[0]
        # The paste pass ALWAYS runs, on both paths, because the four compositing knobs are
        # arguments to it. If they were applied only when cropping, they would be live in
        # the panel yet silently inert on every large selection -- the exact "worse than an
        # absent control" failure this package's dead-knob registry exists to prevent. At
        # their defaults (opacity 1, normal, no match, no detail) the pass is a no-op on
        # pixels, so the unconditional form costs nothing when the user touches nothing.
        try:
            png, paste_note = _m.paste_back(
                base_bytes, png, paste_plan,
                opacity=spec.get("opacity"), blend_mode=spec.get("blend_mode"),
                color_match=spec.get("color_match"),
                preserve_detail=spec.get("preserve_detail"),
                # No border fade when the box IS the frame: a feathered edge would keep the
                # original's outermost ring and read as a halo around a wholly regenerated
                # image. There is no seam to hide at full frame.
                feather=(_m.SEAM_FEATHER_PX if crop_plan else 0),
                # BOTH paths gate the paste by the painted selection. The graph's own
                # ImageCompositeMasked honoured the silhouette at render time, but the
                # knob pass above runs on the artifact as a WHOLE: color_match fits one
                # affine across the entire box (dominated by the regenerated region) and
                # preserve_detail high-passes it everywhere, so an un-gated paste
                # re-graded pixels the user never painted — measured live as the sail's
                # blown-out sky (full frame) and half the dog repainted beside a 1% paint
                # (crop ring: +34 mean-abs on the ring inside the box, resample alone
                # measures 0.0). The ring stays in the LATENTS (model context) — it just
                # no longer ships as pixels.
                selection_png=mask)
        except Exception as e:  # noqa: BLE001 — hand back SOMETHING, never a blank
            paste_note = (f"the render could not be composited ({e.__class__.__name__}); "
                          "returning the model output alone")
        job["_crop_json"] = json.dumps({
            "cropped": bool(crop_plan),
            "box": list(paste_plan["box"]), "frame": list(paste_plan["frame"]),
            "size": list(paste_plan["size"]), "rendered_at": [rw, rh],
            "artifact": list(_m.image_size(png)) if _m.HAS_PIL else [rw, rh],
            "composited": "could not be" not in paste_note,
            "knobs": {k: spec.get(k) for k in
                      ("opacity", "blend_mode", "color_match", "preserve_detail")
                      if spec.get(k) not in (None, "", 0)},
            "note": paste_note,
        })
        url = await openwebui_client.save_image(png, "toolbox-edit.png", key)
        # HAND BACK INTO THE CHAT: an editor that opens inline is only half a feature if
        # its result stays trapped in the editor window. When the mint bound this session
        # to a chat (the OWU Tool / MCP retouch pass chat_id through, signed into the
        # launch token — the browser never supplies it), post the saved file as an
        # assistant message the moment the render lands. Best-effort: a failed post is a
        # note on the row, never a lost artifact (the file is saved and the editor shows
        # it either way).
        chat_id = str((job or {}).get("spec", {}).get("chat_id") or "")
        posted = False
        if chat_id and url:
            try:
                posted = await openwebui_client.post_chat_message(
                    chat_id, f"Toolbox render — ![toolbox edit]({url})", key)
            except Exception as e:  # noqa: BLE001 — post_chat_message guards too
                job["_chat_post"] = f"failed ({e.__class__.__name__})"
            else:
                job["_chat_post"] = "posted" if posted else "failed"
        if job.get("_chat_post") or chat_id:
            # Name it in the log too (same logger name serve.py gives the queue, so
            # `docker logs stackd | grep toolbox` answers "it never showed up in chat"
            # from the server side without reproducing the flow).
            import logging
            logging.getLogger("stackd.toolbox").info(
                "job %s: chat hand-back %s (chat %s…)",
                (job or {}).get("id") or "?",
                job.get("_chat_post") or "not attempted (no saved url)",
                chat_id[:8] or "-")
            # Ride the persisted provenance so the EDITOR can say it out loud — the
            # user's complaint was "it just sits in the new window": the message IS
            # appended, but OWU never live-pushes an external append, so the chat tab
            # needs one refresh. The status line says which of the three states happened.
            if job.get("_chat_post") and job.get("_crop_json"):
                try:
                    _cj = json.loads(job["_crop_json"])
                    _cj["chat_post"] = job["_chat_post"]
                    job["_crop_json"] = json.dumps(_cj)
                except (ValueError, TypeError):
                    pass
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


async def _harvest_once(client, prompt_id: str, base: str):
    """One /history check that downloads the output if the prompt has already landed.

    timeout_s=0 is deliberate, not sloppy: wait_and_fetch reads /history BEFORE it tests
    the deadline, so this means 'harvest it if it is there, fail fast if it is not' rather
    than a second full wait. Returns the images dict, or None if nothing is there.
    """
    try:
        got = await client.wait_and_fetch(prompt_id, {_graphs.SAVE_NODE}, base=base,
                                          timeout_s=0)
    except Exception:  # noqa: BLE001 — 'not finished yet' is the expected shape here
        return None
    if got and got.get(_graphs.SAVE_NODE):
        return got
    return None


async def _harvest_after_deadline(client, prompt_id: str, base: str, grace=None):
    """Keep polling for grace seconds after the deadline, WITHOUT interrupting.

    The order matters and is the whole point: interrupt first and a render that was 16 s
    from finishing is destroyed. Wait first and it may still arrive.
    """
    end = time.monotonic() + (RENDER_GRACE_S if grace is None else float(grace))
    while True:
        got = await _harvest_once(client, prompt_id, base)
        if got:
            return got
        if time.monotonic() > end:
            return None
        await asyncio.sleep(2.0)


async def _interrupt_prompt(client, prompt_id: str, base: str) -> None:
    """Tell ComfyUI to stop painting an image we have stopped waiting for. Best-effort and
    scoped to this one prompt id, so a concurrent user's render is untouched."""
    try:
        await client.post_json(f"{base.rstrip('/')}/interrupt",
                               {"prompt_id": prompt_id}, timeout=10)
    except Exception:  # noqa: BLE001 — we are already giving up; never mask the timeout
        pass


def comfy_cancel(prompt_id: str, base: str) -> None:
    """jobs.JobQueue cancel seam: best-effort interrupt of ONE running prompt. Mirrors
    imagegen/bench.py's _abort_queue but targeted — we interrupt only the prompt id this
    job recorded, so a concurrent user's render is left alone. Never raises: the queue logs
    and treats a failed interrupt as best-effort (the row is already marked cancelled)."""
    if not prompt_id or not base:
        return
    from stackd.imagegen import comfyui_client

    async def _go():
        # the same helper the timeout path uses, so "interrupt this one prompt" has exactly
        # one spelling in the package rather than two that can drift apart
        await _interrupt_prompt(comfyui_client, prompt_id, base)
    try:
        asyncio.run(_go())
    except Exception:  # noqa: BLE001
        pass

