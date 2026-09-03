"""
Shared ComfyUI Flux.2 Klein workflow definitions for the MCP server.

The four graphs (GENERATE_WORKFLOW, EDIT_WORKFLOW, EDIT_WORKFLOW_INPAINT, STYLIZE_WORKFLOW)
are loaded from workflow_graphs/*.json rather than embedded here -- that directory is the
single source of truth, not a copy to keep in sync by hand.

Each workflow_graphs/*.json is plain ComfyUI API-format JSON (exactly what gets POSTed to
/prompt) -- confirmed against a live ComfyUI 0.27.0 / frontend 1.45.20 that this format also
drag-and-drops directly onto its canvas and auto-builds an editable graph, so there's no
separate hand-maintained UI-export format needed any more either: hand-tweak in ComfyUI's UI,
then Workflow -> Export (API) back onto the same file. If you edit a workflow_graphs/*.json
directly instead, sanity-check every class_type it uses still exists in the live install (GET
/comfyui/object_info) before trusting it -- that's the only real drift risk now that there's
a single file, not a stale-copy risk.
"""

import json
import os

_GRAPH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow_graphs")


def _load_graph(filename):
    with open(os.path.join(_GRAPH_DIR, filename)) as f:
        return json.load(f)


# -----------------------------------------------------------------------------
# generate_image: Flux.2 Klein 9B txt2img (distilled, no negative prompt)
# -----------------------------------------------------------------------------
GENERATE_WORKFLOW = _load_graph("generate.json")

GENERATE_WIDTH_NODE = "1"
GENERATE_HEIGHT_NODE = "2"
GENERATE_SEED_NODE = "3"
GENERATE_POSITIVE_NODE = "4"
GENERATE_SAVE_NODE = "14"

# Flux.2 Klein 9B: dimensions must be multiples of 16, max output is 4 megapixels. This is
# the pipeline's real technical ceiling -- still enforced as a hard validation check on an
# explicit width+height request (see _resolve_dimensions_*) -- but NOT what default/auto
# sizing targets; see PRACTICAL_MAX_PIXELS below.
MAX_PIXELS = 4 * 1024 * 1024

# Confirmed in practice: decoding a source image anywhere close to the full 4MP ceiling
# measurably leaks into CPU/host RAM during edit_image (the same class of problem as the
# VRAM spike a large UPLOAD causes -- see comfyui_client.py's pre-upload downscale --
# just resurfacing at a higher plateau once the working CANVAS itself got large, not just
# the upload). Default/auto sizing across all three tools therefore targets this smaller,
# RAM-safe ceiling instead of MAX_PIXELS -- 2048 x 848, i.e. MAX_SIDE on the long edge times
# the short edge of "ultratall"/"ultrawide" (the most elongated of the non-square presets,
# so also the smallest-area preset at MAX_SIDE) -- rather than an arbitrary fraction of
# MAX_PIXELS. An explicit width+height request from the caller can still go all the way up
# to MAX_PIXELS (validated, not silently capped) if that RAM cost is specifically wanted.
PRACTICAL_MAX_PIXELS = 2048 * 848

# Each non-square preset's long edge stays at MAX_SIDE (2048) -- the short edge is then
# whatever that aspect ratio implies, which is always well under PRACTICAL_MAX_PIXELS on its
# own (the most extreme, ultrawide/ultratall, is 2048x848 = 1.74MP; the least extreme,
# landscape/portrait, is 2048x1584 = 3.24MP -- still under MAX_PIXELS). "square" is the one
# preset where both sides would hit MAX_SIDE simultaneously (2048x2048 = 4.19MP, essentially
# the raw MAX_PIXELS ceiling with no aspect-ratio-driven reduction at all), so it's the one
# preset capped down to PRACTICAL_MAX_PIXELS instead (1312x1312).
ASPECT_PRESETS = {
    "square": (1312, 1312),
    "landscape": (2048, 1584),
    "portrait": (1584, 2048),
    "widescreen": (2048, 1168),
    "tall": (1168, 2048),
    "ultrawide": (2048, 848),
    "ultratall": (848, 2048),
}

# -----------------------------------------------------------------------------
# edit_image: whole-image img2img (target_region empty)
# -----------------------------------------------------------------------------
EDIT_WORKFLOW = _load_graph("edit.json")

EDIT_WIDTH_NODE = "1"
EDIT_HEIGHT_NODE = "2"
EDIT_SEED_NODE = "3"
EDIT_POSITIVE_NODE = "4"
EDIT_LOAD_IMAGE_NODE = "5"
EDIT_KSAMPLER_NODE = "14"
EDIT_SAVE_NODE = "16"

# -----------------------------------------------------------------------------
# edit_image: masked precision edit (target_region given)
#
# NOTE (history -- five real bugs, found and fixed in sequence during this pipeline's own
# development, each with empirical evidence -- kept here as a numbered reference since
# later comments in this file cross-reference them by number):
#
# Bug 1: VAEEncodeForInpaint binarizes its mask input via mask.round() before growing it,
# so softening applied before that node gets thrown away. Fixed by painting from GrowMask's
# raw (always-binary) output and doing all visual softening as an image-space alpha
# composite (MASK_COMPOSITE_NODE) entirely outside the diffusion path.
#
# Bug 2: the "soft edge" mask feeding that composite was FeatherMask, which only fades rows/
# columns near the CANVAS edges (for outpainting), not around an arbitrary object's own
# silhouette mid-frame -- it was doing nothing for a centered subject. Fixed with a real
# contour-following soft edge: MaskToImage -> ImageBlur (a true Gaussian blur, radius/sigma
# set from edge_softness) -> ImageToMask, clamped against the hard mask (multiply) to stop
# the blur's outward spillover past what was actually painted.
#
# Bug 3: clamping the blur against the RAW hard mask (not a dilated version of it) zeroed
# the alpha in a single pixel step right at the visible edge -- a Gaussian blur of a hard
# mask is ~0.5 exactly at the mask's own boundary, and multiplying by a hard mask already 0
# one pixel outside means alpha drops 0.5->0.0 in one pixel, a real visible discontinuity
# independent of any color/tone mismatch. Fixed by clamping against a version of the mask
# dilated by blur_radius (MASK_CLAMP_MARGIN_NODE) instead, so the blur has already decayed
# to ~0 by the time it reaches the clamp boundary. grow_mask_by on the inpaint-encode node
# was also bumped from 2 to 6 (ComfyUI's own node default) for latent-space blend cushion.
#
# Bug 4: debug_mask_preview could silently return nothing -- ComfyUI's node cache is keyed
# on traced input values regardless of prompt_id, and the mask branch doesn't depend on the
# KSampler seed, so a repeat call with the same (image, dims, target_region, invert_mask)
# hits the cache for the WHOLE branch including PreviewImage, and cached nodes never appear
# in that prompt's /history outputs. Fixed with a throwaway ImageBlur (MASK_PREVIEW_CACHEBUST_NODE,
# randomized sigma every call) inserted only when debug_mask_preview is requested, guaranteeing
# a cache miss for that node without touching the real composite/output path at all.
#
# Bug 5: additions that need to extend past the segmented subject's existing silhouette (a
# hat's crown above a head, wings) came out barely-visible/translucent rather than absent or
# malformed -- GrowMask's own expand is the actual PAINT EXTENT (how far past the raw
# segmentation the sampler may generate anything at all), not just an edge-softness knob, and
# left at a fixed 12px there was nowhere for that structure to be drawn. Fixed by growing that
# expand together with edge_softness (see server.py's _submit_edit_masked) instead of leaving
# it fixed.
# -----------------------------------------------------------------------------
EDIT_WORKFLOW_INPAINT = _load_graph("edit/flux2-klein-inpaint.json")

MASK_WIDTH_NODE = "1"
MASK_HEIGHT_NODE = "2"
MASK_SEED_NODE = "3"
MASK_POSITIVE_NODE = "4"
MASK_LOAD_IMAGE_NODE = "5"
MASK_REGION_NODE = "6"
MASK_COLOR_STRENGTH_NODE = "7"
MASK_SEGMENT_NODE = "14"  # CLIPSegMask (was Florence2Run) -- its "mask" output is index 0
MASK_GROW_NODE = "16"  # GrowMask -- consumes the (possibly inverted) mask; also the paint extent (node 20)
MASK_REGION_REFLATENT_NODE = "22"  # ReferenceLatent fed by the masked-region latent
MASK_FULL_REFLATENT_NODE = "23"  # ReferenceLatent fed by the whole-original-image latent
MASK_FULL_ENCODE_NODE = "21"  # VAEEncode of the whole original image, only feeds node 23
MASK_KSAMPLER_NODE = "24"
MASK_SAVE_NODE = "27"
MASK_PREVIEW_NODE = "29"  # PreviewImage, fed by MaskToImage(28) -- see debug_mask_preview
MASK_COMPOSITE_NODE = "30"  # ImageCompositeMasked
MASK_INVERT_NODE = "50"
MASK_BLUR_NODE = "32"  # ImageBlur -- see edge_softness; drives the real soft-edge alpha (node 34)
MASK_CLAMP_MARGIN_NODE = "35"  # GrowMask -- dilated clamp for node 34; see Bug 3 above
MASK_PREVIEW_CACHEBUST_NODE = "36"  # ImageBlur, only added when debug_mask_preview -- see Bug 4 above

# EDIT_WORKFLOW_INPAINT above is the flux2-klein inpaint graph and stays the canonical
# reference for every MASK_* node id in this file. Other models get their own inpaint
# graph via the manifest (models[m].tools.edit.inpaint_graph) -- see load_inpaint_graph()
# -- and those graphs MUST reuse this exact node-id layout for the scaffold nodes
# (Florence2 + mask + composite + KSampler), differing only in the model front-end
# (loaders, FluxGuidance), so server._submit_edit's id-based rewiring works unchanged.

# -----------------------------------------------------------------------------
# stylize_image: ReferenceLatent-conditioned full regenerate (fixed-menu adjustments)
# -----------------------------------------------------------------------------
STYLIZE_WORKFLOW = _load_graph("stylize.json")

STYLIZE_LOAD_IMAGE_NODE = "1"
STYLIZE_UPSCALE_NODE = "2"
STYLIZE_PROMPT_NODE = "7"
STYLIZE_SEED_NODE = "11"
STYLIZE_SAVE_NODE = "13"


# =============================================================================
# Multi-model registry (workflow_graphs/models.json)
#
# The constants above stay the canonical Flux.2 Klein path (and the ONLY path for
# edit_image's masked/inpaint mode). Everything below adds per-model graph selection
# for generate_image / edit_image (whole-image) / stylize_image so a caller can pass
# model="flux2-dev" | "ideogram4" | "flux2-klein". server.py resolves a model, loads
# its graph via load_model(), and patches parameters through set_node() -- which
# knows, per node class_type, which input key a logical role writes to, so the same
# {role: node_id} map works across the three models' differently-shaped graphs.
# =============================================================================

MODELS_MANIFEST = _load_graph("models.json")


class UnknownModel(ValueError):
    pass


class ModelUnavailable(UnknownModel):
    """model is in the manifest but marked `"available": false` -- it runs on a
    backend this MCP path can't reach right now (e.g. flux2-dev / ideogram4 live on
    the RTX ComfyUI, which is down while the agentic-coder model holds the card;
    only flux2-klein is on the always-on iGPU ComfyUI). Subclasses UnknownModel so
    the tools' existing `except (UnknownModel, ToolUnsupported)` returns it as a
    clean {"error": ...} without a new catch clause."""


class ToolUnsupported(ValueError):
    """model exists but doesn't offer this tool (or this mode of it)."""


def model_names() -> list[str]:
    return list(MODELS_MANIFEST["models"].keys())


def default_model_name() -> str:
    return MODELS_MANIFEST.get("default") or model_names()[0]


def resolve_model_name(model: str | None, fallback: str | None = None) -> str:
    name = (model or fallback or "").strip() or default_model_name()
    if name not in MODELS_MANIFEST["models"]:
        raise UnknownModel(
            f"Unknown image model '{name}'. Available: {', '.join(model_names())}."
        )
    return name


def model_entry(model: str | None, fallback: str | None = None) -> tuple[str, dict]:
    name = resolve_model_name(model, fallback)
    entry = MODELS_MANIFEST["models"][name]
    if entry.get("available") is False:
        reason = entry.get("unavailable_reason") or "it isn't available right now."
        raise ModelUnavailable(f"The '{name}' image model {reason}")
    return name, entry


def rewrite_config(model: str | None, fallback: str | None = None) -> dict:
    """The model's prompts/*.txt rewrite settings, or {} if it declares none."""
    _, entry = model_entry(model, fallback)
    return dict(entry.get("prompt_rewrite") or {})


def load_model(tool: str, model: str | None, fallback: str | None = None) -> tuple[str, dict, dict, dict]:
    """(model_name, fresh graph dict, {role: node_id}, model entry).

    tool is 'generate' | 'edit' | 'stylize'. Raises ToolUnsupported if the model
    doesn't wire that tool. The returned graph is freshly json.load-ed, so the
    caller may mutate it directly (server.py still deepcopies for symmetry with the
    legacy path -- harmless)."""
    name, entry = model_entry(model, fallback)
    tools = entry.get("tools") or {}
    if tool not in tools:
        raise ToolUnsupported(
            f"Image model '{name}' does not support {tool}. "
            f"Use one of: {', '.join(n for n, e in MODELS_MANIFEST['models'].items() if tool in (e.get('tools') or {}))}."
        )
    spec = tools[tool]
    graph = _load_graph(spec["graph"])
    return name, graph, dict(spec.get("nodes") or {}), entry


def load_inpaint_graph(model: str | None, fallback: str | None = None) -> tuple[str, dict]:
    """(model_name, fresh inpaint graph dict) for a model whose `edit` tool declares
    `inpaint: "builtin"` + an `inpaint_graph`. Raises ToolUnsupported otherwise. The
    returned graph reuses the klein MASK_* node-id layout (see the note by
    EDIT_WORKFLOW_INPAINT) so server._submit_edit patches it the same way for every
    model."""
    name, entry = model_entry(model, fallback)
    edit_spec = (entry.get("tools") or {}).get("edit") or {}
    if edit_spec.get("inpaint") != "builtin" or not edit_spec.get("inpaint_graph"):
        raise ToolUnsupported(
            f"Image model '{name}' has no builtin masked-inpaint graph. "
            f"Use one of: {', '.join(n for n, e in MODELS_MANIFEST['models'].items() if ((e.get('tools') or {}).get('edit') or {}).get('inpaint') == 'builtin')}."
        )
    return name, _load_graph(edit_spec["inpaint_graph"])


# Per-node-class input key for each logical role. A role not listed here (or a class
# not listed for it) falls through to `_ROLE_DEFAULT_KEY`, then to the role name.
_ROLE_KEY_BY_CLASS: dict[str, dict[str, str]] = {
    "positive": {"CLIPTextEncode": "text"},
    "prompt": {"CLIPTextEncode": "text"},
    "seed": {
        "KSampler": "seed",
        "KSamplerAdvanced": "seed",
        "SamplerCustomAdvanced": "noise_seed",
        "RandomNoise": "noise_seed",
    },
}
_ROLE_DEFAULT_KEY: dict[str, str] = {
    "positive": "value",   # PrimitiveStringMultiline / PrimitiveString
    "prompt": "value",
    "seed": "value",       # PrimitiveInt / INTConstant
    "width": "value",
    "height": "value",
    "denoise": "denoise",
    "scale_by": "scale_by",
    "image": "image",
    "guidance": "guidance",
}


def set_node(graph: dict, node_id: str, role: str, value) -> None:
    """graph[node_id].inputs[<key for (role, class_type)>] = value.

    Silently no-ops if node_id isn't in the graph (a model whose graph legitimately
    lacks that role -- e.g. an img2img graph with no explicit width node) so callers
    can patch a common role set without branching per model."""
    node = graph.get(node_id)
    if node is None:
        return
    cls = node.get("class_type", "")
    key = _ROLE_KEY_BY_CLASS.get(role, {}).get(cls) or _ROLE_DEFAULT_KEY.get(role, role)
    node.setdefault("inputs", {})[key] = value
