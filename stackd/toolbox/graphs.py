"""M1: the painted-mask edit graph, DERIVED from imagegen's canonical masked-inpaint graph.

We deliberately do NOT hand-maintain a copy of the 31-node flux2-klein-inpaint.json. This
loads that graph through imagegen.workflows and swaps ONLY the text-segmentation node
(MASK_SEGMENT_NODE = "14", a CLIPSegMask) for a LoadImageMask that reads the mask PNG the
user painted. Every other node id stays byte-identical, which is exactly what
workflows.py's hard rule (any inpaint graph must reuse the MASK_* scaffold layout so the
id-based rewiring in imagegen/_submit_edit works unchanged) demands. Deriving rather than
copying means zero drift: if the canonical graph is fixed upstream, this one inherits it.

Why LoadImageMask, and why channel="alpha": masks.normalize() returns canonical RGBA whose
RGB is white and whose ALPHA carries the selection (see masks.py). Reading any other channel
would read the constant white fill and inpaint the whole photo — so channel is not a
cosmetic choice here, it is the difference between editing a blemish and editing everything.
"""

from __future__ import annotations

import copy

from stackd.imagegen import workflows as _wf

# The node whose class we replace, re-exported so callers/tests reference the SAME constant
# imagegen does rather than a duplicated literal.
SEGMENT_NODE = _wf.MASK_SEGMENT_NODE            # "14"
GROW_NODE = _wf.MASK_GROW_NODE                   # "16", consumes ["14", 0]
SAVE_NODE = _wf.MASK_SAVE_NODE                   # "27"
LOAD_IMAGE_NODE = _wf.MASK_LOAD_IMAGE_NODE       # "5", the photo


class GraphUnavailable(RuntimeError):
    """The requested model has no builtin masked-inpaint graph, or the layout is off."""


def _mask_source_node(mask_filename: str) -> dict:
    """The node that replaces CLIPSegMask: a LoadImageMask reading the uploaded painted
    mask. Output index 0 is a MASK exactly as CLIPSegMask's was, so node 16 (GrowMask) and
    the whole soft-edge / VAEEncodeForInpaint chain downstream are untouched."""
    return {
        "class_type": "LoadImageMask",
        "inputs": {"image": mask_filename, "channel": "alpha"},
        "_meta": {"title": "Painted Mask (from the editor)"},
    }


def painted_mask_graph(model: str = "flux2-klein", *, mask_filename: str = "__MASK__.png") -> dict:
    """(freshly loaded, mutable) masked-inpaint graph with the mask source swapped.

    The caller uploads the canonicalized painted mask to ComfyUI, gets back its stored
    filename, and passes it as mask_filename (exactly as _submit_edit sets the LoadImage
    source filename), then submits via comfyui_client.submit_workflow. Raises
    GraphUnavailable for a model with no builtin inpaint graph."""
    try:
        _name, graph = _wf.load_inpaint_graph(model)
    except Exception as e:  # ToolUnsupported and anything the manifest can raise
        raise GraphUnavailable(str(e)) from e
    if SEGMENT_NODE not in graph:
        raise GraphUnavailable(f"mask graph {model!r} has no segmentation node {SEGMENT_NODE}")
    graph[SEGMENT_NODE] = _mask_source_node(mask_filename)
    # Node 6 (PrimitiveString "Mask Prompt") fed CLIPSeg's text; with the swap nothing
    # consumes it. ComfyUI still executes unreferenced nodes, so drop it rather than leave
    # a dangling node that could log a validation warning. Deep-safe: only remove if truly
    # unreferenced anywhere in the graph.
    _drop_if_unreferenced(graph, _wf.MASK_REGION_NODE)
    return graph


def _drop_if_unreferenced(graph: dict, node_id: str) -> None:
    if node_id not in graph:
        return
    for other in graph.values():
        for value in (other.get("inputs") or {}).values():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) == node_id:
                return                       # still consumed; leave it
            if isinstance(value, list) and any(str(v) == node_id for v in value):
                return
    graph.pop(node_id, None)


def validate_painted_mask_graph(graph: dict) -> list[str]:
    """Return a list of human-readable layout problems (empty == good). Used by the smoke
    suite so a drift between this graph and the scaffold contract fails a test, not a
    user's GPU render."""
    problems: list[str] = []
    seg = graph.get(SEGMENT_NODE)
    if not seg:
        problems.append(f"missing mask node {SEGMENT_NODE}")
        return problems
    if seg.get("class_type") != "LoadImageMask":
        problems.append(f"node {SEGMENT_NODE} is {seg.get('class_type')!r}, want LoadImageMask")
    if (seg.get("inputs") or {}).get("channel") != "alpha":
        problems.append("mask channel must be alpha (normalize() carries selection in ALPHA)")
    grow = graph.get(GROW_NODE)
    if not grow:
        problems.append(f"missing grow node {GROW_NODE}")
    elif (grow.get("inputs") or {}).get("mask") != [SEGMENT_NODE, 0]:
        problems.append(f"node {GROW_NODE} must consume [{SEGMENT_NODE}, 0]")
    if SAVE_NODE not in graph:
        problems.append(f"missing save node {SAVE_NODE}")
    if LOAD_IMAGE_NODE not in graph:
        problems.append(f"missing photo node {LOAD_IMAGE_NODE}")
    return problems


def canonical_inpaint_node_ids(model: str = "flux2-klein") -> set:
    """Node ids of the untouched canonical graph, for the drift test."""
    try:
        _n, g = _wf.load_inpaint_graph(model)
    except Exception as e:  # noqa: BLE001
        raise GraphUnavailable(str(e)) from e
    return set(g.keys())


# ---------------- click-to-segment graph (SAM3, standalone) ----------------
# This is NOT derived from the flux2 inpaint scaffold: it is a tiny, self-contained graph
# whose only job is pixel-coordinates -> mask. It runs ComfyUI's native SAM3_Detect node
# (comfy_extras/nodes_sam3.py) on the multiplex checkpoint, and never touches the MASK_*
# layout imagegen's id-based rewiring depends on, so the two graphs cannot drift one
# another. Kept in graphs.py (not engine.py) so its shape is importable + testable GPU-free.
#
#   1 LoadImage  (the photo)              -> IMAGE
#   2 CheckpointLoaderSimple (SAM3.1)      -> MODEL, CLIP, VAE
#   3 CLIPTextEncode (optional, text only) -> CONDITIONING
#   4 SAM3_Detect   model+image+coords[/cond]-> MASK, BBOXES
#   5 MaskToImage   (binary MASK -> IMAGE)
#   6 SaveImage     (so the result is fetchable via /history like any render)

SAM3_CKPT = "sam3.1_multiplex_fp16.safetensors"   # Comfy-Org/sam3.1, fp16, ~1.7 GB
SEG_LOAD = "1"
SEG_CKPT = "2"
SEG_CLIP = "3"
SEG_DETECT = "4"
SEG_MASKIMG = "5"
SEG_SAVE = "6"


def sam3_segment_graph(*, source_filename: str, ckpt_name: str = SAM3_CKPT,
                       points=None, negative_points=None, text: str = "",
                       threshold: float = 0.5, refine_iterations: int = 2) -> dict:
    """A ready-to-submit SAM3 segmentation graph.

    points / negative_points are lists of {"x": int, "y": int} in the SOURCE PIXEL space
    the uploaded image is stored at (the caller scales the editor's fractional 0..1 clicks
    to the working size before calling). SAM3_Detect reads positive_coords/negative_coords
    as JSON strings -- this is exactly the KJNodes PointsEditor convention it expects.

    text, when given, is encoded through the checkpoint's own CLIP and fed as conditioning
    (the detector path); the point path needs no CLIP at all, so for a pure click we leave
    the CLIPTextEncode node out entirely to keep the graph minimal and avoid loading a text
    encoder the click never uses.
    """
    import json as _json
    graph = {
        SEG_LOAD: {"class_type": "LoadImage",
                   "inputs": {"image": source_filename},
                   "_meta": {"title": "Photo to segment"}},
        SEG_CKPT: {"class_type": "CheckpointLoaderSimple",
                   "inputs": {"ckpt_name": ckpt_name},
                   "_meta": {"title": "SAM3.1 checkpoint"}},
        SEG_DETECT: {"class_type": "SAM3_Detect",
                     "inputs": {
                         "model": [SEG_CKPT, 0],
                         "image": [SEG_LOAD, 0],
                         "positive_coords": _json.dumps(list(points or [])),
                         "negative_coords": _json.dumps(list(negative_points or [])),
                         "threshold": float(threshold),
                         "refine_iterations": int(refine_iterations),
                         "individual_masks": False,
                     },
                     "_meta": {"title": "SAM3 click/text segmentation"}},
        SEG_MASKIMG: {"class_type": "MaskToImage",
                      "inputs": {"mask": [SEG_DETECT, 0]},
                      "_meta": {"title": "Mask to image"}},
        SEG_SAVE: {"class_type": "SaveImage",
                   "inputs": {"images": [SEG_MASKIMG, 0],
                              "filename_prefix": "toolbox_seg"},
                   "_meta": {"title": "Save mask"}},
    }
    if text:
        graph[SEG_CLIP] = {"class_type": "CLIPTextEncode",
                           "inputs": {"clip": [SEG_CKPT, 1], "text": str(text)},
                           "_meta": {"title": "SAM3 text prompt"}}
        graph[SEG_DETECT]["inputs"]["conditioning"] = [SEG_CLIP, 0]
    return graph


def validate_sam3_segment_graph(graph: dict) -> list[str]:
    """Layout problems (empty == good). Guards the two contracts that actually break a
    click-select if they drift: the SAM3_Detect node must consume the checkpoint's MODEL
    (output 0) and the LoadImage IMAGE (output 0), and SaveImage must sit at the end of the
    MaskToImage chain. A wrong wiring here returns the wrong pixels (or none) on paid GPU
    time, so it is checked before submit, exactly like validate_painted_mask_graph."""
    problems: list[str] = []
    det = graph.get(SEG_DETECT)
    if not det or det.get("class_type") != "SAM3_Detect":
        problems.append(f"node {SEG_DETECT} must be SAM3_Detect")
        return problems
    di = det.get("inputs") or {}
    if di.get("model") != [SEG_CKPT, 0]:
        problems.append(f"SAM3_Detect.model must be [{SEG_CKPT}, 0] (CheckpointLoader MODEL)")
    if di.get("image") != [SEG_LOAD, 0]:
        problems.append(f"SAM3_Detect.image must be [{SEG_LOAD}, 0] (LoadImage IMAGE)")
    if (graph.get(SEG_CKPT) or {}).get("class_type") != "CheckpointLoaderSimple":
        problems.append(f"node {SEG_CKPT} must be CheckpointLoaderSimple")
    mi = (graph.get(SEG_MASKIMG) or {}).get("inputs") or {}
    if mi.get("mask") != [SEG_DETECT, 0]:
        problems.append(f"MaskToImage.mask must be [{SEG_DETECT}, 0] (SAM3 MASK output)")
    if SEG_SAVE not in graph:
        problems.append(f"missing save node {SEG_SAVE}")
    return problems

