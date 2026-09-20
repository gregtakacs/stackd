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
import math
import os

try:
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    HAS_PIL = True
except ImportError:  # pragma: no cover - exercised only on a pillow-less install
    Image = None
    ImageFilter = None
    ImageChops = None
    ImageStat = None
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

# --------------------------------------------------------------------------------
# The long-edge ceiling EVERY handover obeys: the editor's <img>, the SAM3 segmenter
# and the ComfyUI render all receive an image whose long side is <= this.
#
# This is a measurement, not a preference. Job e35265c66c8bcce4 (2026-09-17) asked to
# wet an asphalt patch: 1.07% coverage, 2048x1584, flux2-klein, 6 sampler steps. It
# sampled in 486 s (59 -> 91 s/it, degrading as the APU thrashed its own unified pool)
# and decoded+composited in another 110 s -- 616 s wall, PAST the 600 s client deadline,
# so a finished render was reported to the user as an error. The same graph at the
# ~1.7MP the MCP tier budgets for itself ran in 76-156 s. Editing at 1024 on the long
# edge keeps the same edit inside a minute on the iGPU, which is the only engine that is
# always resident.
#
# Per-deployment escape hatch: TOOLBOX_RENDER_MAX_SIDE. The discrete-CUDA tier handled
# 2048 comfortably, so an operator who has that card free may raise it -- but the
# DEFAULT has to serve the machine that is actually up when a job arrives, and a
# silently-10-minute render is the worse failure.
# --------------------------------------------------------------------------------
RENDER_MAX_SIDE = int(os.getenv("TOOLBOX_RENDER_MAX_SIDE", "1024") or "1024")
LATENT_GRID = 16          # Flux.2 Klein requires multiples of 16 (see workflows._round16)
MIN_WORKING_SIDE = 64     # below this the latent grid has nothing to work with


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


def fit_within(width, height, max_side: int = 0, *,
               min_side: int = MIN_WORKING_SIDE, grid: int = LATENT_GRID):
    """(w, h) to RUN an image at: aspect preserved, long side <= max_side (0 = no cap),
    both sides on the 16-px latent grid, neither below min_side.

    Pure integer math — deliberately no PIL — because the working size has to be
    decidable (and testable) without an image decoder, and because `api._dims()` runs on
    the no-pillow fail-closed path too.

    The rounding matches the editor's own canvas maths (round to the nearest multiple of
    16), so the size the browser paints at and the size the server normalises the mask to
    cannot drift apart — a mismatch there is how an edit lands on the wrong third of a
    photo. After a shrink the long side lands exactly on max_side; the follow-up loop only
    bites for a max_side that is NOT itself grid-aligned (e.g. the env override set to
    1000), where naive rounding could push it back over the ceiling the operator asked for.
    """
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        raise ValueError("working size needs integer width and height")
    if w <= 0 or h <= 0:
        raise ValueError(f"working size {w}x{h} is not positive")
    cap = int(max_side or 0)
    if cap and max(w, h) > cap:
        s = cap / float(max(w, h))
        w, h = int(round(w * s)), int(round(h * s))

    lo = min_side if not cap or cap >= min_side else grid

    def grid_round(v):
        return max(lo, int(round(v / float(grid))) * grid)
    w, h = grid_round(w), grid_round(h)

    # A cap below the min_side floor is the operator overriding the floor on purpose, so
    # the floor yields to it (down to one grid step) rather than silently breaking the cap.
    while cap and max(w, h) > cap and max(w, h) > grid:
        if w >= h:
            w = max(grid, w - grid)
        else:
            h = max(grid, h - grid)
    return w, h


def shrink_to_max_side(image_bytes: bytes, max_side: int = RENDER_MAX_SIDE, *,
                       min_side: int = MIN_WORKING_SIDE, grid: int = LATENT_GRID):
    """(bytes, (w, h)) — the ONE ingest resize. The photo the browser is handed, the
    source uploaded to ComfyUI and the source the no-GPU preview overlay is tinted from
    all come from here, so no process ever sees the raw upload at full size.

    Downscale ONLY. An image already inside the cap is returned byte-for-byte unchanged,
    and that is a correctness decision, not an optimisation:
      * grid-rounding an 800x600 photo to 800x608 would be an *upscale* — resampling in
        detail the sensor never captured, which the next edit then inherits, because
        toolbox edits chain (the artifact becomes the next source);
      * a needless PNG re-encode of a correctly-sized photo costs CPU on every request
        and, for a photo that arrived with metadata, quietly discards it.
    """
    _require_pil()
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as e:  # noqa: BLE001 — normalised into the module's own failure type
        raise MaskError(f"could not decode the source image ({e.__class__.__name__}: {e})")
    cap = int(max_side or 0)
    if not cap or max(img.width, img.height) <= cap:
        return image_bytes, (img.width, img.height)     # already inside: hand back the bytes
    # FLOOR to the grid here, NOT round-to-nearest as fit_within does. The difference is
    # deliberate: this function resamples REAL PIXELS, and rounding a 1030x770 photo to
    # 1024x800 would be a 4% upscale of the short edge — invented detail that the next edit
    # inherits, because toolbox edits chain (the artifact becomes the next source).
    # fit_within keeps round-to-nearest because it sizes the working canvas, not pixels.
    s = cap / float(max(img.width, img.height))
    w = max(grid, int(img.width * s) // grid * grid)
    h = max(grid, int(img.height * s) // grid * grid)
    out = io.BytesIO()
    # convert("RGB") matches comfyui_client.downscale_to_exact_size: sources are photos,
    # and an alpha channel reaching the graph is a different image than the one previewed.
    img.convert("RGB").resize((w, h), Image.LANCZOS).save(out, DEFAULT_FORMAT)
    return out.getvalue(), (w, h)


# --------------------------------------------------------------------------------
# Crop-and-paste: render the region the user actually selected.
#
# The ceiling above caps what the ENGINE can afford; cropping is what makes a small
# edit cheap WITHOUT throwing the photo away. A 1% selection in a full-resolution frame
# costs 1% of the latents, and the result is pasted back onto the ORIGINAL pixels
# instead of replacing them — which is also the compositing pass the panel's
# blend_mode / opacity / color_match / preserve_detail knobs have promised since M1
# (TODO 1B's honest-control inventory). This is where they finally live.
#
# Coordinate spaces, spelled out because a mistake here is silent:
#   canvas  the working grid the browser paints on and the canonical mask lives on,
#           (cw, ch) == the job's working_w/h, always <= RENDER_MAX_SIDE
#   box     the crop rect, held in CANVAS px, grid-aligned
#   render  what ComfyUI runs at — the box MAPPED TO SOURCE PIXELS at use time,
#           floor-aligned to the latent grid and capped at the ceiling
#           (render_budget). Canvas-box size for a photo that holds more real
#           pixels under the box is the ratchet, not a saving.
#           When source == canvas (already inside the ceiling) it is the box itself.
#   source  the photo as the user loaded it, (sw, sh). Mapping canvas -> source is
#           PROPORTIONAL (see _scale_box) and only ever happens where the source is
#           actually in hand, so a stale canvas size cannot quietly skew a crop.
# --------------------------------------------------------------------------------

CROP_MARGIN_FRAC = 0.35      # context ring around the selection, x its long edge
CROP_MARGIN_MIN = 96         # floor on that ring, in canvas px
MIN_CROP_SIDE = 256          # don't ask a diffusion model to paint a postage stamp
CROP_AFFORDABLE = 0.75       # crop only when the box stays under this fraction of frame
SEAM_FEATHER_PX = 12         # how far the paste edge fades
BLEND_MODES = ("normal", "multiply", "screen", "overlay", "soft_light", "hard_light",
               "luminosity", "color")


def selection_bbox(mask_bytes: bytes, *, threshold: int = COVERAGE_BRIGHTNESS_THRESHOLD):
    """(x0, y0, x1, y1) of the selected pixels in the mask's OWN grid, or None when it
    selects nothing. Read via extract_coverage — the SAME rule the render's mask uses —
    so a crop planned from this can never miss paint the graph will honour."""
    cov = extract_coverage(_open(mask_bytes))
    box = cov.point(lambda p: 255 if p > threshold else 0).getbbox()
    return tuple(box) if box else None


def _scale_box(box, frame, target):
    """A rect from one pixel grid to another, proportionally per axis, clamped to the
    target. Per-axis on purpose: canvas and source agree on aspect only up to a grid
    rounding, and assuming one shared factor is how an edit lands off-centre."""
    (bx0, by0, bx1, by1), (fw, fh), (tw, th) = box, frame, target
    fx, fy = (tw / float(fw or 1)), (th / float(fh or 1))
    x0, y0 = max(0, int(round(bx0 * fx))), max(0, int(round(by0 * fy)))
    x1, y1 = min(tw, int(round(bx1 * fx))), min(th, int(round(by1 * fy)))
    return (x0, y0, max(x0 + 1, x1), max(y0 + 1, y1))


def plan_crop(mask_bytes: bytes, *, max_side: int = RENDER_MAX_SIDE,
              grid: int = LATENT_GRID):
    """The crop plan for a canonical mask, or None when rendering the whole frame is the
    better call (empty/degenerate selection, or a box covering most of the frame).

    {"box": canvas-space grid-aligned rect, "frame": (cw, ch) the mask's own size,
     "size": (rw, rh) the render runs at}

    The margin decides whether the paste is invisible. A model can only continue a
    texture/perspective/light gradient from context it was GIVEN, so a box hugging the
    selection pastes a region whose surroundings were never in the latents and the seam
    shows: ring = CROP_MARGIN_FRAC of the selection's long edge, floored at
    CROP_MARGIN_MIN, with MIN_CROP_SIDE on the box itself.
    """
    _require_pil()
    img = _open(mask_bytes)
    cw, ch = img.width, img.height
    box = selection_bbox(mask_bytes)
    if not box:
        return None
    x0, y0, x1, y1 = box
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    ring = max(CROP_MARGIN_MIN, int(round(CROP_MARGIN_FRAC * max(x1 - x0, y1 - y0))))
    bx0, by0 = max(0, x0 - ring), max(0, y0 - ring)
    bx1, by1 = min(cw, x1 + ring), min(ch, y1 + ring)
    # Widen to the model's comfort floor, symmetrically where the frame allows. Ask for a
    # grid step MORE than MIN_CROP_SIDE, because the alignment below floors inward: asking
    # for exactly 256 could land on 240 and quietly break the floor we just applied.
    for axis in ("x", "y"):
        lo, hi, cap = (bx0, bx1, cw) if axis == "x" else (by0, by1, ch)
        want = min(MIN_CROP_SIDE + grid, cap)
        if hi - lo < want:
            need = want - (hi - lo)
            lo, hi = max(0, lo - (need + 1) // 2), min(cap, hi + need // 2)
            if hi - lo < want:                      # wall-hit: push the other way instead
                lo = max(0, hi - want) if hi >= want else 0
        if axis == "x":
            bx0, bx1 = lo, hi
        else:
            by0, by1 = lo, hi
    # THEN align: origins floor to the grid and the sizes are floored to whole grid steps,
    # which keeps the far edge inside the frame (bw <= bx1 - bx0 <= cap - bx0). Sending an
    # unaligned width/height to a Flux graph is not a rounding detail — it is rejected.
    bx0, by0 = bx0 // grid * grid, by0 // grid * grid
    bw = max(grid * 2, ((bx1 - bx0) // grid) * grid)
    bh = max(grid * 2, ((by1 - by0) // grid) * grid)
    bw, bh = min(bw, cw - bx0), min(bh, ch - by0)
    if bw % grid or bh % grid or bw < grid or bh < grid:
        return None                       # a frame so small nothing grid-aligned fits
    if bw * bh > CROP_AFFORDABLE * cw * ch:
        return None                       # no real saving, and a seam to hide for nothing
    return {"box": (int(bx0), int(by0), int(bx0 + bw), int(by0 + bh)),
            "frame": (int(cw), int(ch)), "size": (int(bw), int(bh))}


def crop_for_render(source_bytes: bytes, plan: dict, *, size=None):
    """The photo crop the GPU is given — the plan's rect out of whatever source it is
    handed (the full-resolution photo, proportionally), resampled to the render size.
    Returns (bytes, (rw, rh)) so the caller patches the graph's width/height nodes with
    the SAME numbers it cropped at. Pass size=render_budget(plan, source_size) to spend
    the photo's real pixels; the default (plan size) keeps the canvas-box geometry. Resize-to-exact is deliberate: it matches
    comfyui_client.downscale_to_exact_size and the graph's own ImageScale(crop="disabled")
    stretch, so source and mask arrive with identical geometry."""
    _require_pil()
    img = Image.open(io.BytesIO(source_bytes))
    box = _scale_box(plan["box"], plan["frame"], (img.width, img.height))
    want = tuple(size or plan["size"])
    buf = io.BytesIO()
    img.convert("RGB").crop(box).resize(want, Image.LANCZOS).save(buf, DEFAULT_FORMAT)
    return buf.getvalue(), want


def crop_mask(mask_bytes: bytes, plan: dict, *, size=None) -> bytes:
    """The SAME rect of the canonical mask, on the render grid. Geometry-free on purpose
    (see resize_canonical): normalize_layers applied each object's grow/feather exactly
    once already, and applying them twice is the double-grow bug fixed in 0B-follow-up-2."""
    _require_pil()
    img = _open(mask_bytes).convert("RGBA")
    want = tuple(size or plan["size"])
    sub = img.crop(_scale_box(plan["box"], plan["frame"], (img.width, img.height)))
    if sub.size != want:
        sub = sub.resize(want, Image.LANCZOS)
    buf = io.BytesIO(); sub.save(buf, DEFAULT_FORMAT)
    return buf.getvalue()


def render_budget(plan: dict, source_size, *, max_side: int = RENDER_MAX_SIDE,
                  grid: int = LATENT_GRID):
    """The pixel size a crop ACTUALLY RENDERS AT — decided at USE time, when the photo
    is in hand, never at plan time (the plan stays photo-free so a stale or missing
    photo cannot silently move a crop; this is the one number that legitimately needs
    the photo, and it is computed from its size alone).

    plan["size"] is a CANVAS-space box: on a 1024 canvas it tops out around 300 px even
    when the photo under that box holds four times as many real pixels. Rendering at
    canvas size and letting paste_back upscale the artifact to the photo-sized region is
    the ratchet this module exists to stop: the model paints 256 latents' worth of
    detail, the artifact comes back at PHOTO size through an upscale, and the NEXT edit
    inherits that softness as its source. Mapping the box to source pixels (the same
    per-axis proportion as _scale_box) and clamping to the ceiling spends the canvas's
    whole budget where the user is looking at it.

    Two rules the ratchet taught:
      * floor to the latent grid, NEVER round up — rounding up fabricates rows the photo
        never had (see the 2048x1584 -> 1024x800 incident documented at
        shrink_to_max_side), and a non-multiple is a flat Flux-graph rejection;
      * never below plan["size"] — a photo that arrives SMALLER than the canvas (ingest
        fails open without pillow) must not shrink the box below the comfort floor the
        plan already chose; that was the old behaviour and it was fine.

    Pure integer math, like fit_within: no decoder needed, fully testable, and the
    caller only has to measure the bytes it already holds.
    """
    fw, fh = (int(plan["frame"][0]), int(plan["frame"][1]))
    bw, bh = (int(plan["size"][0]), int(plan["size"][1]))
    try:
        sw, sh = int(source_size[0]), int(source_size[1])
    except (TypeError, ValueError, IndexError):
        return (bw, bh)
    if fw <= 0 or fh <= 0 or sw <= 0 or sh <= 0 or bw <= 0 or bh <= 0:
        return (bw, bh)                       # junk geometry falls back to the plan, never to a guess
    cap_side = max(grid * 2, (int(max_side) // grid) * grid)   # the ceiling, pre-floored to the grid

    def one(v, floorv):
        v = min(v, cap_side)
        v = (v // grid) * grid                # floor: rounding up invents pixels
        return max(floorv, min(v, cap_side))  # the floor itself can never exceed the cap

    # per-axis truncation (NOT round) — _scale_box's rounding maps a corner, this maps a
    # side length, and a side may only claim pixels the photo actually has
    return (one(bw * sw // fw, min(bw, cap_side)),
            one(bh * sh // fh, min(bh, cap_side)))


def _seam_alpha(size, feather: int = SEAM_FEATHER_PX) -> "Image.Image":
    """An 'L' mask, 255 across the crop, fading to 0 at its border. This border fade is
    the ONLY edge the paste has to hide — the selection's own edge was already honoured by
    ImageCompositeMasked inside the crop — so the seam is a rectangle, not a silhouette.

    feather <= 0 means NO fade (fully solid): used by the full-frame paste, where a border
    fade would keep the ORIGINAL's outermost ring and show it as a halo around a wholly
    regenerated image. There is no seam to hide when the box is the frame.
    """
    w, h = int(size[0]), int(size[1])
    if int(feather) <= 0:
        return Image.new("L", (w, h), 255)
    f = max(1, min(int(feather), max(1, min(w, h) // 3)))
    a = Image.new("L", (w, h), 0)
    a.paste(Image.new("L", (max(1, w - 2 * f), max(1, h - 2 * f)), 255), (f, f))
    return a.filter(ImageFilter.GaussianBlur(f / 2.0))


def _color_match(src: "Image.Image", ref: "Image.Image") -> "Image.Image":
    """Per-channel mean/std transfer of src toward ref (no numpy): the crop inherits the
    photo it is being dropped back into, which is what the color_match knob exposes."""
    if src.mode != "RGB":
        src = src.convert("RGB")
    if ref.mode != "RGB":
        ref = ref.convert("RGB")
    s, r = ImageStat.Stat(src), ImageStat.Stat(ref)
    # split()/merge(), not the band API: getband/putband are not part of Pillow's public
    # Image surface (absent in 12.x), so this is a portable form rather than an
    # AttributeError raised mid-render on real GPU time — the class of bug a syntax check
    # cannot see and a suite with no coverage for this function will happily bless.
    bands = src.split()
    for i in range(3):
        smean, sstd = s.mean[i], s.stddev[i]
        rmean, rstd = r.mean[i], r.stddev[i]
        # A flat source band has no scale to transfer — but it still has the wrong LEVEL,
        # and shifting its mean toward the reference is the whole point of the knob (a flat
        # grey patch pasted into sunlight must come out of color_match lit, not grey).
        # Bailing out here used to make color_match a silent no-op on exactly that case.
        ratio = (rstd / sstd) if sstd > 1e-6 else 1.0
        offset = rmean - smean * ratio
        shifted = bands[i].point(
            lambda p, k=ratio, b=offset: max(0, min(255, int(round(p * k + b)))))
        bands = bands[:i] + (shifted,) + bands[i + 1:]
    return Image.merge("RGB", bands)


def _gate_from_selection(selection_png: bytes, want) -> "Image.Image":
    """An 'L' gate from the canonical (255 = edit-here) selection, resized to the paste
    region. HARD-edged on purpose: the artifact `new` already carries the graph's own
    soft-edged composite at the silhouette (nodes 31–35), so its pixels converge on the
    base photo right at the gate boundary — a blur here would push the model's output
    PAST the paint (measured: a 1.5px Gaussian bled red 30+ levels into the 2px unpainted
    margin of the suite's border test). A decode failure RAISES rather than returning a
    solid gate: silently un-gated is exactly the whole-photo regrade this exists to stop.
    """
    try:
        sel = extract_coverage(_open(selection_png))
    except Exception as e:  # noqa: BLE001 — name the stage, never fall back to opaque
        raise MaskError(f"the selection gate could not be decoded ({e.__class__.__name__}: {e})")
    if sel.size != tuple(want):
        sel = sel.resize(tuple(want), Image.LANCZOS)
    return sel.point(lambda p: 255 if p > 128 else 0)


def paste_back(source_bytes: bytes, artifact_png: bytes, plan: dict, *,
               opacity: float = 1.0, blend_mode: str = "normal",
               color_match: float = 0.0, preserve_detail: float = 0.0,
               feather: int = SEAM_FEATHER_PX, selection_png: bytes | None = None):
    """Paste a regenerated crop back over the FULL-RESOLUTION photo. (bytes, note).

    `selection_png` (canonical RGBA, 255 = edit-here) gates the paste to the painted
    selection. It is REQUIRED by the full-frame path (feather 0): there is no crop box
    to hide behind there, and the four knob operations below run on `new` as a WHOLE —
    color_match in particular fits ONE per-channel affine to the stats of the entire
    frame and applies it to EVERY pixel. With a large selection (the live sailboat:
    tall sail -> plan_crop declined -> full frame) that affine is dominated by the
    regenerated region and visibly re-grades the untouched sky: measured background
    mean-abs-diff 24.6 (RGB shift -38/-31/-1) with knobs as shipped vs 0.1 zeroed.
    Gated, the photo survives byte-for-byte outside the paint. The crop path passes
    None: its ring context is deliberately part of the paste and the graph's own
    ImageCompositeMasked already honoured the silhouette inside the crop.

    Server-side rather than graph nodes, because the graph never sees the uncropped
    photo — no ComfyUI node could composite against it — and keeping it here means the
    one module that owns mask semantics also owns "what pixels survived the round trip",
    where the smoke suite can look at it.

    The four M1 dead knobs ARE the four operations below, each with a direction check in
    tests/smoke_toolbox.py:
      opacity           alpha of the paste; 0 returns the source byte-for-byte
      blend_mode        ImageChops operators in ComfyUI's ImageBlend spellings
      color_match       0..1 pull of the crop's histogram toward the photo around it
      preserve_detail   0..1 of the ORIGINAL high-frequency detail added back, so a
                        smooth model output does not erase the texture it replaced
    """
    _require_pil()
    try:
        base = Image.open(io.BytesIO(source_bytes)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise MaskError(f"could not decode the source image ({e.__class__.__name__}: {e})")
    box = _scale_box(plan["box"], plan["frame"], (base.width, base.height))
    x0, y0, x1, y1 = box
    if x1 - x0 < 2 or y1 - y0 < 2:
        return source_bytes, "the crop fell outside the photo; nothing was pasted"
    region = base.crop((x0, y0, x1, y1))
    want = region.size
    try:
        new = Image.open(io.BytesIO(artifact_png)).convert("RGB").resize(want, Image.LANCZOS)
    except Exception as e:  # noqa: BLE001
        return source_bytes, f"could not decode the rendered crop ({e.__class__.__name__}: {e})"

    notes = []

    def frac(key, default=0.0):
        try:
            v = float(key if key is not None else default)
        except (TypeError, ValueError):
            notes.append(f"{default if key is None else key!r} was not a number; used {default}")
            v = default
        return max(0.0, min(1.0, v))

    cm = frac(color_match)
    if cm > 0:
        new = Image.blend(new, _color_match(new, region), cm)
    pd = frac(preserve_detail)
    if pd > 0:
        # high-pass the ORIGINAL, graft it onto the model output: detail = orig - blur(orig)
        r = max(1, min(8, (min(want) // 24) or 2))
        gray = Image.new("RGB", want, (128, 128, 128))
        hp = ImageChops.subtract(ImageChops.add(region, gray), region.filter(
            ImageFilter.GaussianBlur(r)))
        new = ImageChops.subtract(ImageChops.add(new, Image.blend(gray, hp, pd)), gray)

    mode = str(blend_mode or "normal").strip().lower()
    if mode not in BLEND_MODES:
        notes.append(f"unknown blend_mode {blend_mode!r}; used normal")
        mode = "normal"
    if mode != "normal":
        fn = {"multiply": ImageChops.multiply, "screen": ImageChops.screen,
              "overlay": ImageChops.overlay, "soft_light": ImageChops.soft_light,
              "hard_light": ImageChops.hard_light}.get(mode)
        if fn is not None:
            new = fn(new, region)
        else:
            try:      # luminosity / color: one LAB channel swap, no extra library needed
                # split()/merge() rather than getband(): that API is not on Pillow 12's
                # Image. The except below stays, so a genuinely unsupported mode degrades
                # instead of losing the render — but smoke_toolbox asserts the "unavailable"
                # note NEVER appears, which is what keeps this guard from quietly becoming
                # a dead knob again (the failure this suite was blind to at 366/366 green).
                al, bl = new.convert("LAB").split(), region.convert("LAB").split()
                merged = Image.merge("LAB", (al[0], bl[1], bl[2])
                                     if mode == "luminosity"
                                     else (bl[0], al[1], al[2]))
                new = merged.convert("RGB")
            except Exception as e:  # noqa: BLE001 — never lose a render over a blend
                notes.append(f"blend_mode {mode} unavailable ({e.__class__.__name__}); normal")

    op = frac(opacity, 1.0)
    if op <= 0.0:
        return source_bytes, "opacity 0 — the paste was skipped"
    alpha = _seam_alpha(want, feather)
    if op < 1.0:
        alpha = alpha.point(lambda p, k=op: int(round(p * k)))
    if selection_png:
        # AND the paste with the painted selection (see the docstring): outside the
        # gate the base photo survives byte-for-byte, so the whole-frame knob ops
        # above cannot re-grade pixels the user never painted. An undecodable gate
        # raises (MaskError) rather than falling back to solid — a silent un-gated
        # paste is the exact defect this exists to prevent; the caller's except turns
        # it into the honest "could not be composited" note instead.
        alpha = ImageChops.multiply(alpha, _gate_from_selection(selection_png, want))
    out = base.copy()
    out.paste(new, (x0, y0), alpha)
    buf = io.BytesIO(); out.save(buf, DEFAULT_FORMAT)
    return buf.getvalue(), "; ".join(notes)


def resize_canonical(mask_bytes: bytes, width: int, height: int) -> bytes:
    """Resample a CANONICAL mask (RGBA, alpha = coverage) onto a new size.

    Deliberately no morph, no threshold, no feather: normalize_layers already applied
    each object's grow/shrink/feather exactly once, and applying them twice is the
    double-grow bug this package fixed once already (see the display-wash note in
    web/toolbox.js). This exists for the one case where a job's recorded working size
    disagrees with the mask normalised for it — an older row, or a cap changed between
    create and render — so the source and the mask still meet at the same dims.
    """
    _require_pil()
    img = _open(mask_bytes).convert("RGBA")
    if (img.width, img.height) == (int(width), int(height)):
        return mask_bytes
    out = io.BytesIO()
    img.resize((int(width), int(height)), Image.LANCZOS).save(out, DEFAULT_FORMAT)
    return out.getvalue()




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
# ---------------------------------------------------------------------------------
# Circular (Euclidean) morphology, connected-component denoise, outward feather.
#
# For the SMART-SELECT ('auto') objects ONLY -- the square `_morph` above stays for
# shape/brush/legacy (a web of byte-exact tests pins its composition identity). A square
# structuring element is precisely why a grown smart-select turns into a SQUARE (a lone
# pixel dilated by r=6 becomes 169 px, not a disc's ~113). A true binary disc is built with
# a separable squared-EUCLIDEAN DISTANCE TRANSFORM (Felzenszwalb-Huttenlocher): on inside
# the disc of radius r grown from the selection iff distance-to-nearest-selected <= r. Two
# linear-time 1-D lower-envelope passes, O(area), no numpy/scipy (container ships neither).
# A Gaussian can NOT do this: it ERASES thin masks (a lone pixel blurred at sigma 0.6 then
# thresholded at 128 -> 0 px) -- the same reason the old symmetric feather shrank objects.
# ---------------------------------------------------------------------------------
_INF = float("inf")
DISK_CAP = 2048     # hard bound on the EDT grid's long side; bigger inputs morph at this
                    # scale and composite back, so a 4 MP preview cannot hang the UI.


def _dt1d(f):
    """1-D squared-distance transform: D[q] = min_p((q-p)^2 + f[p]), via the parabola lower
    envelope. f holds 0.0 at seeds and _INF elsewhere (or a prior pass's distances). Only
    FINITE f entries are seeds; a whole-line of _INF returns _INF (no seed -> distance is
    unbounded), which is what keeps a fully-selected auto mask from annihilating on erode."""
    n = len(f)
    if n == 0:
        return []
    INF = _INF
    v = [0] * n
    z = [0.0] * (n + 1)
    d = [INF] * n
    k = -1                                   # no parabola yet
    for q in range(n):
        if f[q] >= INF:                      # not a seed: contributes no parabola
            continue
        if k < 0:                            # first seed becomes the initial parabola
            k = 0
            v[0] = q
            z[0] = -INF
            z[1] = INF
            continue
        fq = f[q]
        while True:
            vk = v[k]
            s = ((q * q + fq) - (vk * vk + f[vk])) / (2.0 * (q - vk))
            if s > z[k]:
                break
            k -= 1
        k += 1
        v[k] = q
        z[k] = s
        z[k + 1] = INF
    if k < 0:
        return d                             # the whole line is _INF: distance unbounded
    kk = 0
    for q in range(n):
        while z[kk + 1] < q:
            kk += 1
        dq = q - v[kk]
        d[q] = dq * dq + f[v[kk]]
    return d


def _edt(on_rows, w, h):
    """Exact squared Euclidean distance-to-nearest-selected over an h x w bool grid."""
    INF = _INF
    tmp = [[0.0] * h for _ in range(w)]
    for x in range(w):
        col = [0.0 if on_rows[y][x] else INF for y in range(h)]
        dc = _dt1d(col)
        for y in range(h):
            tmp[x][y] = dc[y]
    out = [[0.0] * w for _ in range(h)]
    for y in range(h):
        row = [tmp[x][y] for x in range(w)]
        dr = _dt1d(row)
        for x in range(w):
            out[y][x] = dr[x]
    return out


def _on_rows(cov, w, h):
    px = list(cov.getdata())
    t = COVERAGE_BRIGHTNESS_THRESHOLD
    return [[px[y * w + x] > t for x in range(w)] for y in range(h)]


def _rows_to_image(rows_bool, w, h):
    im = Image.new("L", (w, h), 0)
    p = im.load()
    for y in range(h):
        r = rows_bool[y]
        for x in range(w):
            p[x, y] = 255 if r[x] else 0
    return im


def _morph_disk(cov: "Image.Image", px: int, *, grow: bool) -> "Image.Image":
    """Binary dilate/erode by a CIRCULAR structuring element of radius `px`.

    grow: on iff a selected pixel lies within Euclidean distance px. shrink: stay on iff
    every pixel within px is selected (distance to nearest UNselected > px). Read off the
    same COVERAGE threshold the rest of the module uses, so preview and render agree. The
    grid is capped so cost never explodes; a lone pixel grows to a DISC, not a square."""
    px = int(max(0, min(MAX_MORPH_PX, int(px))))
    if px <= 0:
        return cov
    w, h = cov.size
    s = max(w, h)
    if s > DISK_CAP:
        sc = DISK_CAP / float(s)
        cw, ch = max(1, int(round(w * sc))), max(1, int(round(h * sc)))
        work = cov.resize((cw, ch), Image.LANCZOS)
        r = max(1, int(round(px * (cw / float(w)))))
    else:
        cw, ch, work, r = w, h, cov, px
    on = _on_rows(work, cw, ch)
    if grow:
        D = _edt(on, cw, ch)
        r2 = r * r + 0.25
        out_rows = [[D[y][x] <= r2 for x in range(cw)] for y in range(ch)]
    else:
        off = [[not on[y][x] for x in range(cw)] for y in range(ch)]
        D = _edt(off, cw, ch)
        # erode: a pixel survives radius r iff NO unselected pixel lies within r of it,
        # i.e. its distance-to-nearest-outside D (exact integer squared dist) exceeds r*r.
        # Strict '>' here is the contract _erode_with_cap's histogram relies on: both must
        # pick the same pixels or the preview and the render would disagree.
        out_rows = [[on[y][x] and D[y][x] > r * r for x in range(cw)] for y in range(ch)]
    im = _rows_to_image(out_rows, cw, ch)
    if (cw, ch) != (w, h):
        im = im.resize((w, h), Image.LANCZOS).point(
            lambda p: 255 if p > COVERAGE_BRIGHTNESS_THRESHOLD else 0)
    return im


def keep_significant_components(mask_bytes: bytes, *, min_frac: float = 0.01,
                                min_px: int = 30) -> bytes:
    """Drop the disconnected islands SAM3 scatters around the real object.

    The 'random squares outside the selection' are the click segmenter's stray speckles: a
    few px each, which a square grow then inflates into a visible block. A smart selection
    is meant to be contiguous, so islands below a size floor are removed -- but the floor is
    RELATIVE (a fraction of the dominant blob) so a genuinely separate part the user
    deliberately shift-clicked to add (the hat, the backpack) SURVIVES; only decoder noise
    dies. 8-connected flood fill (a diagonal bridge keeps a part attached to its object)."""
    _require_pil()
    img = _open(mask_bytes)
    cov = extract_coverage(img)
    w, h = cov.size
    on = _on_rows(cov, w, h)
    flat = [v for row in on for v in row]
    n = w * h
    lab = [0] * n
    seen = [False] * n
    areas = []
    nxt = 0
    nbr = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
    for i in range(n):
        if not flat[i] or seen[i]:
            continue
        seen[i] = True
        lab[i] = nxt
        size = 1
        stack = [i]
        while stack:
            c = stack.pop()
            cx, cy = c % w, c // w
            for dx, dy in nbr:
                x, y = cx + dx, cy + dy
                if 0 <= x < w and 0 <= y < h:
                    j = y * w + x
                    if flat[j] and not seen[j]:
                        seen[j] = True
                        lab[j] = nxt
                        size += 1
                        stack.append(j)
        areas.append(size)
        nxt += 1
    if nxt <= 1:
        return mask_bytes                       # empty or a single region: nothing to drop
    floor = max(int(min_px), int(min_frac * max(areas)))
    keepset = set(i for i, a in enumerate(areas) if a >= floor)
    if len(keepset) == nxt:
        return mask_bytes                       # every region clears the floor
    out_rows = [[(flat[y * w + x] and lab[y * w + x] in keepset)
                 for x in range(w)] for y in range(h)]
    cov = _rows_to_image(out_rows, w, h)
    white = Image.new("RGB", (w, h), (255, 255, 255))
    out = Image.merge("RGBA", (*white.split(), cov))
    buf = io.BytesIO()
    out.save(buf, DEFAULT_FORMAT)
    return buf.getvalue()


def _feather_out(cov: "Image.Image", f: float) -> "Image.Image":
    """Soften the boundary OUTWARD, never eroding the object.

    A plain Gaussian pulls the 128-crossing INWARD, so the binarised mask is smaller than
    what the user approved (the 'feather shrinks my selection' complaint). Growing first by
    the feather radius then blurring places the soft skirt BEYOND the edge and keeps the
    solid core; the >=128 footprint can only match or exceed the hard selection."""
    f = float(f or 0)
    if f <= 0:
        return cov
    grown = _morph_disk(cov, max(1, int(round(f))), grow=True)
    soft = grown.filter(ImageFilter.GaussianBlur(max(0.5, f)))
    return ImageChops.lighter(cov, soft)     # union: interior preserved, edge only grows






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


def _erode_with_cap(cov, px: int, *, disk: bool = False):  # noqa: C901 (two kernel paths)
    """Erode by `px`, clamped to the largest radius that still leaves a real selection.

    Returns (image, px_actually_applied, capped).

    Why the clamp exists: shrinking is the one direction that can destroy what it is
    applied to. A 20 px shape is simply gone by radius 12 (measured 441 -> 121 -> 1 -> 0 px),
    and a mask that selects nothing paints nothing -- silently, which is the exact failure
    this module exists to refuse. The cap must come from what actually survives, not bbox.

    The square path walks a 3x3 min filter r times (cheap, C, and composes to the direct
    kernel). The DISK path (smart-select 'auto') cannot use that walk: a disc's unit step is
    a plus/4-neighbourhood, and r pluses compose into a DIAMOND, not a disc. So the whole
    monotone survival curve comes from ONE Euclidean distance transform instead -- the count
    of pixels that survive erosion of radius r is exactly the count whose distance-to-
    outside exceeds r -- which is what finding the cap needs and keeps applied monotonic in
    requested, so the slider never behaves perversely. Both paths return the same bitmap a
    single radius would, so the approved preview and the render agree.

    The floor is 10% of the original coverage, never a token 8 px: below that the surviving
    mask is speckle the user cannot see, and approving it costs them a 13-170 s render.
    """
    px = int(max(0, min(EDGE_LIMIT, int(px))))
    if px <= 0:
        return cov, 0, False
    before = _selected_px(cov)
    if before <= 0:
        return cov, 0, False
    if not disk:
        floor = max(8, int(before * 0.10))
        cur, keep = cov, 0
        for _r in range(px):
            nxt = _morph(cur, 1, grow=False)
            if _selected_px(nxt) < floor:
                break                      # every larger radius can only remove more
            cur, keep = nxt, _r + 1
        return cur, keep, keep != px

    # --- disc survival from a single EDT -------------------------------------------
    w, h = cov.size
    s = max(w, h)
    if s > DISK_CAP:
        sc = DISK_CAP / float(s)
        cw, ch = max(1, int(round(w * sc))), max(1, int(round(h * sc)))
        work = cov.resize((cw, ch), Image.LANCZOS)
        rad_scale = cw / float(w)
    else:
        cw, ch, work, rad_scale = w, h, cov, 1.0
    on = _on_rows(work, cw, ch)
    sel = sum(1 for row in on for v in row if v)
    if sel <= 0:
        return cov, 0, False
    floorc = max(8, int(sel * 0.10))
    off = [[not v for v in row] for row in on]
    D = _edt(off, cw, ch)
    # Histogram by the WORKING max radius each selected pixel survives: it stays on under an
    # erosion of working radius rr iff m >= rr, where m = isqrt(Doff-1). Candidate `cand`
    # erodes at working radius round(cand*rad_scale) (exactly what _morph_disk uses), so
    # bucketing here and indexing by rr below are in the SAME units -- and produce the same
    # bitmap _morph_disk(cov, keep) renders, or preview and render would disagree.
    hist = [0] * (px + 1)
    for y in range(ch):
        onr, Dr = on[y], D[y]
        for x in range(cw):
            if not onr[x]:
                continue
            dv = Dr[x]
            if dv == _INF or dv >= (1 << 52):
                m = px                       # no outside pixel anywhere: survives any radius
            else:
                m = int(math.isqrt(max(0, int(dv) - 1)))   # survives r iff r*r < Doff
            hist[min(m, px)] += 1
    suffix = [0] * (px + 2)
    acc = 0
    for r in range(px, -1, -1):
        acc += hist[r]
        suffix[r] = acc
    keep = 0
    for cand in range(1, px + 1):
        rr = max(1, int(round(cand * rad_scale))) if rad_scale != 1.0 else cand
        if suffix[min(rr, px)] >= floorc:
            keep = cand
        else:
            break                       # monotone: larger cand keeps fewer pixels
    if keep <= 0:
        return cov, 0, False
    return _morph_disk(cov, keep, grow=False), keep, keep != px



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
    kind = str(lay.get("kind") or "shape")     # bound once; drives the disc/outward morph below
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
        # New one-axis protocol: a single signed offset, grow or shrink, never both. An
        # 'auto' (smart-select) silhouette grows/shrinks on a DISC so a blob stays round and
        # a lone pixel becomes a circle, not a square (masks._morph_disk); shapes keep the
        # cheap square kernel their byte-exact tests pin down.
        if edge > 0:
            cov = (_morph_disk(cov, edge, grow=True) if kind == "auto"
                   else _morph(cov, edge, grow=True))
            applied = int(edge)
        elif edge < 0:
            cov, got, capped = _erode_with_cap(cov, -edge, disk=(kind == "auto"))
            applied = -int(got)
    else:
        # Legacy two-field protocol, unchanged in behaviour: an older client (or the spike)
        # sending both values meant closing, and quietly re-defining that as a net offset
        # would change what its saved masks render. Only the new `edge` field is one-axis.
        # Report the two independent radii as one net offset: they are one axis to the
        # user even on the legacy path, and the preview has to name a single number.
        g = int(lay.get("grow", 0) or 0)
        shr = int(lay.get("shrink", 0) or 0)
        cov = (_morph_disk(cov, g, grow=True) if kind == "auto"
               else _morph(cov, g, grow=True))
        applied = g
        if shr:
            cov, got, capped = _erode_with_cap(cov, shr, disk=(kind == "auto"))
            applied = g - int(got)
    if binary:
        # Hard-edged only where the source is a geometric shape, which has no meaningful
        # alpha ramp to preserve. Brush layers skip this -- that is exactly what lets the
        # hardness gradient survive into the composite.
        cov = cov.point(lambda p: 255 if p > COVERAGE_BRIGHTNESS_THRESHOLD else 0)
    feather = lay.get("feather", 0) if allow_feather else 0
    if feather and feather > 0:
        # EVERY feathered kind softens OUTWARD — 'auto' and 'shape' alike. The plain
        # symmetric Gaussian this used to fall back to for shapes pulls the 128-crossing
        # INWARD (its own docstring at the _morph_disk header calls that out: "the same
        # reason the old symmetric feather shrank objects"): half the skirt's alpha is
        # stolen from the solid core, so what the graph binarises is SMALLER than what
        # the user approved and the ramp visibly points into the object. _feather_out
        # (disc-grow by f -> blur by f -> lighter union) keeps the >=128 footprint at or
        # beyond the hard silhouette and puts all of the softness beyond the edge. No
        # test pinned the shape-side blur's kernel (the contract checked is the REPORTED
        # feather number + thresholding), so the "byte-exact" excuse for the split was
        # stale; the browser's display copy grows the same disc the same way, which is
        # what makes preview and render agree again. The disc is also what stops the
        # skirt squaring off at the corners (Chebyshev grow bulges on the diagonals).
        cov = _feather_out(cov, float(feather))
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

