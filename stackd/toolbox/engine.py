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
    w = int((job or {}).get("working_w") or 0) or 1024
    h = int((job or {}).get("working_h") or 0) or 1024
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
        msk_name = await comfyui_client.upload_to_comfy(mask, "toolbox_mask", base=base)
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

