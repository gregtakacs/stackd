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
