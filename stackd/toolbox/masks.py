"""
What a mask MEANS, once it is on the server.

The browser is deliberately only a crude painter: strokes, overshoot, wobbly hands.
Everything the pipeline actually depends on — the mask's size, its polarity, its
softness, whether it selects anything at all — is decided HERE, for three reasons:

1. **Dimensions must match the working canvas, not the display.** The inpaint graphs
   condition on `ImageScale` (node 13 in `edit/flux2-klein-inpaint.json`), i.e. the
   *scaled* source at the job's working width/height, and `VAEEncodeForInpaint` (20)
   takes that mask at those exact dims. A browser canvas paints at whatever CSS size it
   was shown, so trusting the client's pixel grid is how you get an edit applied to the
   wrong third of a photo. The server resizes the mask to the dims it is submitting.
2. **Polarity has to survive foreign PNGs.** Our own painter emits RGBA where RGB is
   white and A is coverage. But a user will equally hand us a black-on-white doodle from
   Markup/Photoshop/Samsung-Notes, which has no alpha at all. `extract_coverage()` is the
   single rule that maps any of those onto "255 = edit here".
3. **An empty mask must be a loud failure, not a silent no-op.** `imagegen` already learned
   this with `comfyui_client.mask_indicates_nothing_found` for the CLIPSeg path (a
   segmentation that localizes nothing paints nothing and looks like a successful
   generation). For a *user-painted* mask the same condition almost always means the mask
   did not line up with the photo (wrong orientation, painted on a thumbnail, inverted
   export), so the message has to say that instead of "improve your wording".

PIL is an optional dependency, guarded exactly like `imagegen/comfyui_client.py`
(pillow ships in the `[imagegen]` extra, not in core stackd). With PIL absent these
functions return `None`/"cannot tell" and the caller must fail closed rather than submit
a job whose mask nobody inspected.
"""

from __future__ import annotations

import io

try:
    from PIL import Image, ImageChops, ImageFilter
    HAS_PIL = True
except ImportError:  # pragma: no cover - exercised only on a pillow-less install
    Image = None
    ImageFilter = None
    ImageChops = None
    HAS_PIL = False


# Same two numbers as imagegen/comfyui_client.py, deliberately duplicated rather than
# imported: toolbox must not import the MCP layer (different extra, different failure
# domain), and these are physical constants of "what counts as a painted pixel", not a
# shared implementation. If one changes, change both.
COVERAGE_BRIGHTNESS_THRESHOLD = 50   # 0-255; above this a pixel counts as selected
# The CLIPSeg path's floor — NOT the right bar for a user-painted mask. There, GrowMask's
# mandatory 12-28px margin means even a small genuine detection ends above ~1.4% coverage,
# so 0.3% reliably means "segmentation found nothing". Applied to a user mask it rejects
# the flagship use case outright: measured on the spike, one pimple on a 1024x768 photo is
# ~0.18%, and a spot-heal brush is *supposed* to be small. A user mask is therefore only
# refused when it selects literally nothing; small-but-real is a note, never an error.
COVERAGE_FLOOR = 0.003               # informational: "this is a small edit" bar
USER_MASK_EMPTY_FLOOR = 0.00015      # ~100 px on 1024x768 => the paint never landed

# An alpha channel counts as "the user meant something by it" only if it is not already
# fully opaque. A flat 255 alpha is what every screenshot/Photoshop export carries.
ALPHA_IS_MEANINGFUL_MIN = 250

MAX_MORPH_PX = 64                   # expand/shrink radius clamp; see _morph()
DEFAULT_FORMAT = "PNG"


class MaskError(Exception):
    """The mask cannot be used at all (undecodable, empty after normalization).
    Distinct from MaskError-with-`hint`: callers surface `str(e)` to the user."""


def _require_pil() -> None:
    if not HAS_PIL:
        raise MaskError("pillow is not available on this stackd install — "
                        "the toolbox mask pipeline cannot normalize or verify masks")


def _open(mask_bytes: bytes) -> "Image.Image":
    _require_pil()
    try:
        img = Image.open(io.BytesIO(mask_bytes))
        img.load()
    except Exception as e:  # noqa: BLE001 — PIL raises a dozen types for bad images
        raise MaskError(f"could not decode the mask image ({e.__class__.__name__}: {e})")
    if img.width < 8 or img.height < 8:
        raise MaskError(f"mask is only {img.width}x{img.height} — it must be drawn at "
                        f"the photo's own size")
    return img



def image_size(image_bytes: bytes) -> tuple[int, int]:
    """Natural (w, h) of an image — used to default a job's working dims from the photo
    itself rather than trusting what the browser thinks it displayed."""
    img = _open(image_bytes)
    return img.width, img.height



def extract_coverage(img: "Image.Image") -> "Image.Image":
    """Map any incoming mask image onto a single-channel 'L' coverage image where 255
    means 'edit here'. Alpha wins when it encodes something (our painter's output);
    otherwise luminance wins (a black-on-white doodle)."""
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    lo, _hi = alpha.getextrema()
    if lo < ALPHA_IS_MEANINGFUL_MIN:
        return alpha
    # No usable alpha: invert-free luminance, so WHITE = selected, matching every
    # "paint the area to edit in white" convention and our own painter's RGB.
    return img.convert("L")


def coverage(mask_bytes: bytes) -> float | None:
    """Fraction of the canvas selected (0.0-1.0), or None if PIL is unavailable."""
    if not HAS_PIL:
        return None
    img = _open(mask_bytes)
    cov = extract_coverage(img)
    # histogram(), not a getdata() loop: this runs on every Preview click and a
    # 2 MP mask makes a Python-level per-pixel loop noticeably laggy.
    masked = sum(cov.histogram()[COVERAGE_BRIGHTNESS_THRESHOLD + 1:])
    return masked / (cov.width * cov.height)


def looks_empty(mask_bytes: bytes, floor: float = COVERAGE_FLOOR) -> bool | None:
    """True => the mask selects essentially nothing; the edit would silently paint
    nothing. None => cannot tell (no PIL). Callers must treat None as 'do not submit'."""
    c = coverage(mask_bytes)
    if c is None:
        return None
    return c < floor


def _morph(cov: "Image.Image", px: int, *, grow: bool) -> "Image.Image":
    """Binary dilate (grow) / erode (shrink) by `px`, via PIL's MaxFilter/MinFilter.

    One 3x3 filter is `radius=1`. The radius is then walked one 3x3 pass at a time rather
    than handed to PIL as one (2*px+1) kernel -- identical output, roughly 14x cheaper at
    px=64 (see the compose note below). MAX_MORPH_PX still bounds it: the walk is linear in
    the radius, not free, and this runs per layer on every live preview. The browser exposes
    expand/shrink in the same units the graph's GrowMask nodes use (pixels on the working
    canvas), so what the preview shows and what the render does are the same number.
    """
    px = int(max(0, min(MAX_MORPH_PX, int(px))))
    if px <= 0:
        return cov
    # Square structuring elements COMPOSE: a 3x3 min/max filter applied r times is exactly
    # the (2r+1)x(2r+1) filter, because [-1,1] composed with itself r times is [-r,r]. So
    # the radius is walked one step at a time rather than requested as one big kernel, which
    # is byte-for-byte the same image (verified against the single kernel for grow and shrink
    # at r = 1, 3, 7, 15, 31, 64 on both a blob and a ring) and far cheaper: on a 400x400
    # layer, r=64 is 122 ms composed against 1728 ms for the direct kernel, and the walk was
    # faster at EVERY radius measured down to r=1, so there is no crossover worth a branch.
    #
    # _erode_with_cap leans on this equivalence rather than merely benefiting from it: it
    # walks the radius one step at a time to build a survival curve, and the mask it returns
    # has to be the same bitmap a single radius would have produced, or the preview the user
    # approved and the mask the render consumes would disagree about which pixels were picked.
    f = ImageFilter.MaxFilter(3) if grow else ImageFilter.MinFilter(3)
    for _ in range(px):
        cov = cov.filter(f)
    return cov


def normalize(mask_bytes: bytes, width: int, height: int, *, expand: int = 0,
              shrink: int = 0, feather: float = 0.0, invert: bool = False,
              binary: bool = True) -> tuple[bytes, dict]:
    """Resample a client-painted mask onto the job's working canvas and return
    (canonical PNG, info).

    Canonical == RGBA whose RGB is white and whose ALPHA is the selection, which is
    exactly what ComfyUI's LoadImageMask(channel="alpha") and the graph's mask chain
    want. Order is deliberately resize -> invert -> grow/shrink -> threshold -> feather:
    geometry ops act on the *working-canvas* scale (a 10 px expand on a 512-wide mask is
    not the same gesture as on a 2048-wide one, and the user tuned the number against
    what they saw at working scale), and feather is last so it softens the final shape
    rather than being eaten by the threshold.

    `binary` re-hardedges after morphology: brush stamps are already solid, and leaving
    a 50%-alpha fringe in the *paint* mask would make VAEEncodeForInpaint's binarization
    (Bug 1 in imagegen/workflows.py) disagree with the preview the user approved.
    """
    _require_pil()
    img = _open(mask_bytes)
    cov = extract_coverage(img)
    before = sum(cov.histogram()[COVERAGE_BRIGHTNESS_THRESHOLD + 1:]) / (cov.width * cov.height)

    if (cov.width, cov.height) != (width, height):
        cov = cov.resize((width, height), Image.LANCZOS)
    if invert:
        cov = cov.point(lambda p: 255 - p)
    cov = _morph(cov, expand, grow=True)
    cov = _morph(cov, shrink, grow=False)
    if binary:
        cov = cov.point(lambda p: 255 if p > COVERAGE_BRIGHTNESS_THRESHOLD else 0)
    # Measured BEFORE the blur, on purpose: a Gaussian blur pushes energy outward, so the
    # >threshold pixel COUNT RISES with feather (measured: a 49x37 spot went 0.0023 ->
    # 0.0033 at feather=6, crossing the "is this tiny?" line purely because of a blend
    # setting). "Did the user paint anything" is a question about the PAINT, so the
    # empty/tiny verdicts use coverage_paint; coverage_after is the real blend extent.
    paint = sum(cov.histogram()[COVERAGE_BRIGHTNESS_THRESHOLD + 1:]) / (width * height)
    if feather and feather > 0:
        cov = cov.filter(ImageFilter.GaussianBlur(max(0.5, float(feather))))

    after = sum(cov.histogram()[COVERAGE_BRIGHTNESS_THRESHOLD + 1:]) / (width * height)
    white = Image.new("RGB", (width, height), (255, 255, 255))
    out = Image.merge("RGBA", (*white.split(), cov))
    buf = io.BytesIO()
    out.save(buf, DEFAULT_FORMAT)
    return buf.getvalue(), {
        "size": [width, height],
        "src_size": [img.width, img.height],
        "coverage_before": round(before, 4),
        "coverage_paint": round(paint, 4),
        "coverage_after": round(after, 4),
        "expand": int(expand), "shrink": int(shrink),
        "feather": round(float(feather or 0), 2),
        "inverted": bool(invert),
        "capped": bool(expand and int(expand) > MAX_MORPH_PX),
    }


def overlay(source_bytes: bytes, mask_bytes: bytes, *, color=(232, 62, 62),
            alpha: float = 0.5, width: int | None = None, height: int | None = None,
            feather: float = 0.0) -> bytes:
    """Composite a translucent tint of the selected region over the source — the
    no-GPU mask preview. Returned as a PNG the editor draws straight back into its
    canvas, so what the user approves before paying for a render is produced by the
    SAME normalization the render will use (see normalize())."""
    _require_pil()
    try:
        src = Image.open(io.BytesIO(source_bytes)).convert("RGBA")
    except Exception as e:  # noqa: BLE001
        raise MaskError(f"could not decode the source image ({e.__class__.__name__}: {e})")
    w, h = (width or src.width, height or src.height)
    if (src.width, src.height) != (w, h):
        src = src.resize((w, h), Image.LANCZOS)
    cov = extract_coverage(_open(mask_bytes)).resize((w, h), Image.LANCZOS)
    if feather and feather > 0:
        cov = cov.filter(ImageFilter.GaussianBlur(max(0.5, float(feather))))
    tint = Image.new("RGBA", (w, h), color + (0,))
    tint.putalpha(cov.point(lambda p: int(round(p * max(0.0, min(1.0, alpha))))))
    out = Image.alpha_composite(src, tint).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, DEFAULT_FORMAT)
    return buf.getvalue()


# ---------------------------------------------------------------------------------
# Per-object layers
#
# Why layers exist: one flattened mask cannot carry per-object intent. A hand-painted
# brush stroke and a dragged rectangle need OPPOSITE post-processing -- the rectangle's
# edge is arbitrary with respect to the object, so it wants grow/shrink and a soften;
# the brush edge IS the user's intent (it came from the hardness ramp), so re-thresholding
# it destroys the only thing that made the hardness slider mean anything.
#
# Measured on the single-mask path: binary=True made hardness=1.0 and hardness=0.2
# byte-identical, and the feather slider did not soften the selection but SHRANK it
# (17881 -> 15429 px at feather 24) because ComfyUI's VAEEncodeForInpaint round()s the
# mask and discards alpha < 128. That is Bug 1 from imagegen/workflows.py, reintroduced
# one layer too early.
#
# So each object carries its own geometry knobs and the mask crosses the wire as layers,
# grouped by (kind, grow, shrink, feather, erase). Kind decides the rule; the numbers are
# the user's.
# ---------------------------------------------------------------------------------

KIND_RULES = {
    #             threshold?  feather?  grow/shrink?
    "brush": (False, False, False),   # hardness IS the edge; nothing here may touch it
    "shape": (True, True, True),      # rect/ellipse/polygon/lasso: arbitrary, so correctable
    "auto":  (True, True, True),      # CLIPSeg silhouette: hard-edged, usually under-selects
}
#
# The third flag is the server ENFORCING what the client only promised. toolbox.js sets
# grow/shrink/feather to 0 for brush objects in stamp(), but that is a client convenience:
# an older tab, a hand-built request, or a bug in stamp() could still send a shrink against a
# hand-painted stroke and the server would have obeyed it -- measured at 6525 -> 765 selected
# pixels for edge=-40 on one stroke. A brush edge is the user's own handwork, chosen by the
# hardness ramp, so no remote number is allowed to erode it. The rule lives here, at the
# boundary, rather than only in the UI that happens to call it.


EDGE_LIMIT = MAX_MORPH_PX            # signed boundary-offset clamp, in working px


def _bbox_min_side(cov):
    """Shorter side of the selection's bounding box, 0 when it selects nothing.

    Used only to tell the user how big an applied offset was RELATIVE to their object --
    the number that steers the control stays pixels, because pixels are what the graph's
    GrowMask consumes and what the preview must agree with to be WYSIWYG.
    """
    bb = cov.getbbox()
    if not bb:
        return 0
    return min(bb[2] - bb[0], bb[3] - bb[1])


def _signed_edge(lay: dict):
    """The layer's boundary offset as one signed number, or None if it did not send one.

    Positive grows, negative shrinks. Deliberately ONE axis: with two independent fields a
    user could set grow=12 and shrink=12 and get a morphological CLOSING (measured: a
    notched shape went 9704 -> 10251 px, i.e. the notch filled) rather than the "no net
    change" both-sliders-equal reads as. One axis makes that state impossible to express.
    """
    if lay.get("edge") is None:
        return None
    try:
        e = int(round(float(lay["edge"])))
    except (TypeError, ValueError):
        return 0
    return max(-EDGE_LIMIT, min(EDGE_LIMIT, e))


def _selected_px(cov) -> int:
    """How many pixels count as selected. histogram(), not a pixel loop: this runs per
    layer, per preview, at working-canvas size (see coverage())."""
    return sum(cov.histogram()[COVERAGE_BRIGHTNESS_THRESHOLD + 1:])


def _erode_with_cap(cov, px: int):
    """Erode by `px`, clamped to the largest radius that still leaves a real selection.

    Returns (image, px_actually_applied, capped).

    Why the clamp exists: shrinking is the one direction that can destroy what it is
    applied to. A 20 px shape is simply gone by radius 12 (measured 441 -> 121 -> 1 -> 0 px),
    and a mask that selects nothing paints nothing -- silently, which is the exact failure
    this module exists to refuse. Capping by bounding box does NOT catch the common case: a
    ring painted as blob-minus-eraser has a 300 px bbox but only ~100 px of material, so it
    dies far earlier than its bbox suggests. The cap must come from what actually survives.

    Why one threshold rather than a fallback: the applied radius has to be monotonic in the
    requested one, or the slider behaves perversely. Halving on failure did exactly that --
    on a ring, edge=-40 fell back to -20 and kept 31024 px while the SMALLER edge=-32 kept
    11944, so dragging further left produced MORE mask. One scan, one cap, applied=min(px,cap).

    Why a walk instead of a kernel: square-SE erosion composes -- applying a 3x3 min filter
    r times is exactly a single (2r+1) filter, because [-1,1] composed r times is [-r,r].
    Verified byte-for-byte against the single kernel at r = 1, 3, 7, 15, 31. Walking by one
    is therefore both cheaper (a 3x3 never pays the quadratic kernel cost: measured 118 ms
    vs 1855 ms at r=64 on a 400x400 layer) and yields the whole survival curve for free,
    which is what finding the cap needs. Cost becomes proportional to the radius actually
    kept rather than to EDGE_LIMIT, which matters because this runs on the live debounced
    preview at up to 4096px working size: 40 ms is usable, 10 s is not.

    The floor is 10% of the original coverage, never a token 8 px: below that the surviving
    mask is speckle the user cannot see, and approving it costs them a 13-170 s render to
    discover it painted nothing.
    """
    px = int(max(0, min(EDGE_LIMIT, int(px))))
    if px <= 0:
        return cov, 0, False
    before = _selected_px(cov)
    if before <= 0:
        return cov, 0, False
    floor = max(8, int(before * 0.10))
    cur, keep = cov, 0
    for _r in range(px):
        nxt = _morph(cur, 1, grow=False)
        if _selected_px(nxt) < floor:
            break                      # every larger radius can only remove more
        cur, keep = nxt, _r + 1
    return cur, keep, keep != px


def _layer_coverage(layer_png: bytes, width: int, height: int, lay: dict):
    """Rasterize one layer onto the working canvas and apply ITS OWN geometry.

    Order mirrors normalize(): resize first so every pixel number is expressed at the
    working-canvas scale the graph uses, then morphology, then threshold, then feather --
    feather last so it softens the final shape instead of being eaten by the threshold.
    """
    cov = extract_coverage(_open(layer_png))
    if (cov.width, cov.height) != (width, height):
        cov = cov.resize((width, height), Image.LANCZOS)
    binary, allow_feather, allow_morph = KIND_RULES.get(
        str(lay.get("kind") or "shape"), KIND_RULES["shape"])
    # measured on the RESAMPLED, pre-morphology coverage: this is the size the offset
    # is being applied against, so a client can say "that 12 px was 30% of this object"
    # without guessing from its own display scale.
    min_side = _bbox_min_side(cov)
    edge = _signed_edge(lay)
    capped = False
    applied = edge or 0            # the radius that ACTUALLY moved the boundary, sign included
    if not allow_morph:            # a brush edge is handwork: no offset, either direction
        edge = applied = 0
    elif edge is not None:
        # New one-axis protocol: a single signed offset, grow or shrink, never both.
        if edge > 0:
            cov = _morph(cov, edge, grow=True)
            applied = int(edge)
        elif edge < 0:
            cov, got, capped = _erode_with_cap(cov, -edge)
            applied = -int(got)
    else:
        # Legacy two-field protocol, unchanged in behaviour: an older client (or the spike)
        # sending both values meant closing, and quietly re-defining that as a net offset
        # would change what its saved masks render. Only the new `edge` field is one-axis.
        # Report the two independent radii as one net offset: they are one axis to the
        # user even on the legacy path, and the preview has to name a single number.
        g = int(lay.get("grow", 0) or 0)
        shr = int(lay.get("shrink", 0) or 0)
        cov = _morph(cov, g, grow=True)
        applied = g
        if shr:
            cov, got, capped = _erode_with_cap(cov, shr)
            applied = g - int(got)
    if binary:
        # Hard-edged only where the source is a geometric shape, which has no meaningful
        # alpha ramp to preserve. Brush layers skip this -- that is exactly what lets the
        # hardness gradient survive into the composite.
        cov = cov.point(lambda p: 255 if p > COVERAGE_BRIGHTNESS_THRESHOLD else 0)
    feather = lay.get("feather", 0) if allow_feather else 0
    if feather and feather > 0:
        cov = cov.filter(ImageFilter.GaussianBlur(max(0.5, float(feather))))
    return cov, edge, applied, capped, min_side


def normalize_layers(layers, width: int, height: int, *,
                     invert: bool = False) -> tuple[bytes, dict]:
    """Composite per-object layers into the canonical mask the graph consumes.

    Layers arrive in creation order and are composited in that order, so an eraser stroke
    still lifts what was under it while a later shape is unaffected by it -- the painting
    semantics the user sees while drawing, preserved across the per-layer geometry.
    invert applies to the UNION, not per layer: two overlapping inverted layers
    max-composited would resurrect the overlap, which is not what "edit everything outside
    what I marked" means.
    """
    _require_pil()
    acc = Image.new("L", (width, height), 0)
    per = []
    for i, lay in enumerate(layers or []):
        png = lay.get("png") if isinstance(lay, dict) else None
        if not png:
            continue
        cov, edge, applied_edge, capped, min_side = _layer_coverage(png, width, height, lay)
        kind = str(lay.get("kind") or "shape")
        binary, allow_feather, allow_morph = KIND_RULES.get(kind, KIND_RULES["shape"])
        if lay.get("erase"):
            acc = Image.composite(Image.new("L", (width, height), 0), acc, cov)
        else:
            # ImageChops.lighter, not "add": overlapping paint must reach 255 once and
            # stay there. Adding would let two half-covered brush passes compound into a
            # brighter pixel, which is the same class of error as the erode-the-brush bug.
            acc = ImageChops.lighter(acc, cov)   # union: paint can only ever ADD coverage
        per.append({
            "i": i, "kind": kind,
            "grow": int(lay.get("grow", 0) or 0),
            "shrink": int(lay.get("shrink", 0) or 0),
            # Report what was ACTUALLY applied, not what was asked: a brush layer asking
            # for feather gets none, and the info must say so or the preview can never be
            # reconciled against the render.
            "feather": (round(float(lay.get("feather", 0) or 0), 2) if allow_feather else 0),
            "erase": bool(lay.get("erase")),
            "thresholded": bool(binary),
            # One-axis reporting. `edge` is what was asked, `edge_applied` what survived the
            # anti-annihilation guard; when they differ the preview MUST show the applied
            # number or the user approves a mask the render will not match.
            "edge": edge if allow_morph else 0,
            "edge_applied": applied_edge if allow_morph else 0,
            "edge_capped": bool(capped) and allow_morph,
            "morphed": bool(allow_morph),
            "obj_min_side": int(min_side),
            "edge_pct": (round(100.0 * abs(applied_edge) / min_side, 1)
                         if (min_side and applied_edge) else 0),
        })
    if invert:
        acc = acc.point(lambda p: 255 - p)
    paint = sum(acc.histogram()[COVERAGE_BRIGHTNESS_THRESHOLD + 1:]) / (width * height)
    # coverage_after is the extent actually handed to the graph. For the layered path it
    # equals coverage_paint by construction -- brush layers are never blurred and shape
    # layers were blurred BEFORE the union -- so both keys are reported to keep this
    # response shape-compatible with the single-mask normalize() one.
    after = paint
    white = Image.new("RGB", (width, height), (255, 255, 255))
    buf = io.BytesIO()
    Image.merge("RGBA", (*white.split(), acc)).save(buf, DEFAULT_FORMAT)
    return buf.getvalue(), {
        "size": [width, height],
        "layers": per,
        "coverage_paint": round(paint, 4),
        "coverage_after": round(after, 4),
        "inverted": bool(invert),
        "per_object": True,
    }


def overlay_layers(source_bytes: bytes, layers, width: int, height: int, *,
                   color=(232, 62, 62), alpha: float = 0.5,
                   invert: bool = False) -> bytes:
    """The authoritative no-GPU preview for the layered path.

    Builds the tint from the SAME normalize_layers() output the render will submit, so the
    overlay the user approves is literally the mask ComfyUI consumes -- the whole point of
    the live preview. A separate feather argument would be a lie here, because feather is
    now a per-object property, not a global one.
    """
    _require_pil()
    try:
        src = Image.open(io.BytesIO(source_bytes)).convert("RGBA")
    except Exception as e:  # noqa: BLE001
        raise MaskError(f"could not decode the source image ({e.__class__.__name__}: {e})")
    if (src.width, src.height) != (width, height):
        src = src.resize((width, height), Image.LANCZOS)
    canon, _info = normalize_layers(layers, width, height, invert=invert)
    cov = extract_coverage(_open(canon)).resize((width, height), Image.LANCZOS)
    tint = Image.new("RGBA", (width, height), color + (0,))
    tint.putalpha(cov.point(lambda p: int(round(p * max(0.0, min(1.0, alpha))))))
    out = Image.alpha_composite(src, tint).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, DEFAULT_FORMAT)
    return buf.getvalue()


def blank_mask(width: int, height: int) -> bytes:
    """An empty (nothing-selected) canonical mask. Handy for tests and for a job that
    intentionally edits the whole frame."""
    _require_pil()
    cov = Image.new("L", (width, height), 0)
    white = Image.new("RGB", (width, height), (255, 255, 255))
    buf = io.BytesIO()
    Image.merge("RGBA", (*white.split(), cov)).save(buf, DEFAULT_FORMAT)
    return buf.getvalue()

