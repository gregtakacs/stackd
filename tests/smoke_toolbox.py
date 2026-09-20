"""P8 — the Comfy Toolbox mask surface (stackd/toolbox/). Pure logic: token auth, mask
semantics, and every /toolbox/* route driven through Toolbox.dispatch() with a fake
handler. No network, no ComfyUI, no Open WebUI, no browser.

The browser-side half of M0 (does a finger paint inside a sandboxed iframe?) cannot be
answered from here — that is what `python3 -m stackd.toolbox.spike` is for. What THIS
file guarantees is that everything downstream of the browser is correct, so when the
spike shows a red status line we know whether to look at the sandbox or at us.

Import-guarded like smoke_imagegen.py: without pillow the mask-dependent checks skip
rather than fail, because pillow is the [imagegen] extra, not a core dependency.

    python3 tests/smoke_toolbox.py
"""

from __future__ import annotations

import base64
import io
import json
import pathlib
import re
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from stackd.toolbox import api as tb_api
    from stackd.toolbox import masks as M
    from stackd.toolbox import tokens as T
    from stackd.toolbox import web as W
except ImportError as e:  # extra not installed
    print(f"  SKIP  stackd toolbox not importable ({e})")
    raise SystemExit(0)

from PIL import Image, ImageDraw  # guaranteed present if we got this far with masks.HAS_PIL

SECRET = "smoke-secret"
EMAIL = "user@example.com"
CHECKS: list[tuple[str, bool]] = []


def check(name, cond, detail=""):
    """detail is shown on failure only: it carries the actual offending value, so a red
    line says WHAT to fix instead of just which assertion tripped."""
    CHECKS.append((name, bool(cond), detail))


# ---------------------------------------------------------------- fake handler
class Fake:
    """Implements the duck type documented on api._Http, recording what was sent."""

    def __init__(self, command="GET", path="/", headers=None, body=b""):
        self.command = command
        self.path = path
        self.headers = headers or {}
        self._body = body
        self.status = None
        self.payload = None
        self.raw = None
        self.ctype = None
        self.sent_headers = None

    def _send_json(self, code, payload, extra_headers=None):
        self.status, self.payload = code, payload
        self.sent_headers = extra_headers or {}

    def _send_bytes(self, code, raw, ctype, cache_s=0, etag=None):
        self.status, self.raw, self.ctype = code, raw, ctype

    def _read_body(self):
        # Consumed on first read, exactly like BaseHTTPRequestHandler's rfile. A fake that
        # hands the same bytes back forever would hide any handler that reads its body
        # twice — a bug that only shows against the real server.
        raw = self._body
        self._body = b""
        return raw

    # 302/redirect handlers (h_launch) go through the raw BaseHTTPRequestHandler API, not
    # _send_json — so the fake records them here, otherwise a redirect silently "passes"
    # by doing nothing observable, which is exactly the silent-green failure this suite
    # exists to eliminate (see the harness deadman note).
    def send_response(self, code, *_a):
        self.status = code
        self.sent_headers = self.sent_headers or {}

    def send_header(self, k, v):
        self.sent_headers = self.sent_headers or {}
        self.sent_headers[k.lower()] = v

    def end_headers(self):
        pass


def make_tb(**over):
    src = over.pop("source", lambda email, ref: PHOTO)
    deps = dict(secret=SECRET, spike_enabled=True, source=src, logger=None)
    deps.update(over)
    return tb_api.Toolbox(**deps)


def post(toolbox, path, obj, token=None, headers=None):
    h = {"content-type": "text/plain"}
    if token:
        h["authorization"] = "Bearer " + token
    h.update(headers or {})
    fake = Fake("POST", path, h, json.dumps(obj).encode())
    toolbox.dispatch(fake)
    return fake


def get(toolbox, path, token=None):
    h = {}
    if token:
        h["authorization"] = "Bearer " + token
    fake = Fake("GET", path, h)
    toolbox.dispatch(fake)
    return fake


# ---------------------------------------------------------------- fixtures
def png_bytes(mode, size, paint):
    im = Image.new(mode, size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = paint(x, y, size)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def rect_mask(size, box):
    """The shape our painter exports: white RGB + alpha coverage = the box."""
    def f(x, y, s):
        inside = (box[0] <= x <= box[2]) and (box[1] <= y <= box[3])
        return (255, 255, 255, 255) if inside else (0, 0, 0, 0)
    return png_bytes("RGBA", size, f)


def flat_png(size):
    """A real PNG at `size` without a per-pixel loop: png_bytes on a 3.2 MP frame takes
    longer than the assertions it feeds are worth, and a slow suite gets skipped."""
    im = Image.new("RGB", size, (11, 22, 33))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def doodle_mask(size, box):
    """The shape a phone markup app exports: black canvas, WHITE box, no meaningful
    alpha — polarity must come from luminance here, not from alpha."""
    def f(x, y, s):
        inside = (box[0] <= x <= box[2]) and (box[1] <= y <= box[3])
        return 255 if inside else 0
    return png_bytes("L", size, f)


def blank(size):
    return png_bytes("RGBA", size, lambda x, y, s: (255, 255, 255, 0))


def spot_mask():
    """A blemish-sized selection: ~0.18% of the frame. This fixture exists because the
    first draft of the coverage guard rejected it — a floor copied from the CLIPSeg path,
    where GrowMask guarantees coverage, is too high for a hand-painted spot heal."""
    return rect_mask((640, 480), (300, 220, 322, 240))


PHOTO = png_bytes("RGB", (640, 480), lambda x, y, s: ((x * 3) % 256, (y * 3) % 256, 128))
# The photo's OWN base64, for asserting a document really carries the pixels. Never
# assert on the bare "data:image/png;base64," prefix: toolbox.js contains that literal
# five times as a concatenation template (im.src = 'data:image/png;base64,' + b64), so
# the prefix is in EVERY document whether or not a photo was fetched. A check written on
# it passes vacuously — which is precisely how "No source image was handed to the editor"
# reached a live browser with 581 offline checks green.
PHOTO_B64 = base64.b64encode(PHOTO).decode()
MASK = rect_mask((640, 480), (200, 150, 300, 250))
MASK_B64 = base64.b64encode(MASK).decode()
TOK = T.mint(SECRET, scope="launch", email=EMAIL)


def _layer_png(size, paint):
    """A layer PNG shaped the way the editor exports one: white RGB, coverage in ALPHA."""
    a = Image.new("L", size, 0)
    paint(ImageDraw.Draw(a))
    im = Image.new("RGBA", size, (255, 255, 255, 0))
    im.putalpha(a)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def brush_ramp_mask(size, box, hardness):
    """A hand-painted brush stroke as the editor exports it: white RGB, and an ALPHA that
    ramps from 255 at the core (r*hardness) down to 0 at the rim. The ramp is the whole
    point — hardness is the brush's only edge control, so any pipeline that thresholds it
    away makes the hardness slider inert. Measured pre-fix: hardness=1.0 and hardness=0.2
    produced byte-identical masks."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    r = max(1.0, (x1 - x0) / 2.0)
    core = r * max(0.02, hardness)

    def f(x, y, s):
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        if d <= core:
            a = 255
        elif d >= r:
            a = 0
        else:
            a = int(round(255.0 * (r - d) / (r - core)))
        return (255, 255, 255, a)
    return png_bytes("RGBA", size, f)


def _hist(canon):
    return Image.open(io.BytesIO(canon)).getchannel("A").histogram()


def _selected(canon):
    return sum(_hist(canon)[M.COVERAGE_BRIGHTNESS_THRESHOLD + 1:])


def _partial(canon):
    h = _hist(canon)
    return sum(h[1:M.COVERAGE_BRIGHTNESS_THRESHOLD + 1])


# ---------------------------------------------------------------- 1b. per-object layers
def test_layers():
    """Per-object grow/shrink/feather, and the two defects that made the global sliders
    actively harmful for the brush. See masks.KIND_RULES for the reasoning."""
    SIZE = (320, 320)
    BOX = (110, 110, 210, 210)

    # --- A: a brush stroke keeps its hardness ramp ----------------------------------
    # The single-mask path binarized, which flattened the ramp: hardness 1.0 and 0.2 were
    # byte-identical, so the slider did nothing. Each hardness must now differ.
    hists = {}
    for hard in (1.0, 0.55, 0.2):
        canon, _ = M.normalize_layers(
            [{"png": brush_ramp_mask(SIZE, BOX, hard), "kind": "brush"}], *SIZE)
        hists[hard] = _partial(canon)
        check(f"layers: brush hardness={hard} edge as expected",
              (hists[hard] == 0) if hard == 1.0 else (hists[hard] > 0),
              f"partial={hists[hard]}")
    check("layers: hardness changes the mask at all (was inert)",
          hists[0.2] > hists[0.55] > hists[1.0],
          f"{hists[1.0]} / {hists[0.55]} / {hists[0.2]}")
    _, i1 = M.normalize_layers([{"png": brush_ramp_mask(SIZE, BOX, 0.55), "kind": "brush"}], *SIZE)
    check("layers: brush layers are NOT thresholded",
          i1["layers"][0]["thresholded"] is False, i1["layers"])

    # --- B: the feather slider must not erode a brush stroke ------------------------
    # Pre-refactor, feather blurred a binarized mask and ComfyUI then round()ed it, so a
    # wider "softness" silently SHANK the edited region (17881 -> 15429 px at feather 24).
    base = None
    for want in (0, 8, 24):
        canon, info = M.normalize_layers(
            [{"png": brush_ramp_mask(SIZE, BOX, 0.55), "kind": "brush", "feather": want}], *SIZE)
        n = _selected(canon)
        base = n if base is None else base
        check(f"layers: brush region unchanged at feather={want}", n == base, f"{n} vs {base}")
        check(f"layers: brush reports feather NOT applied (asked {want})",
              float(info["layers"][0]["feather"]) == 0.0, info["layers"][0])

    # --- C: shapes keep the geometry they asked for ---------------------------------
    c0, _ = M.normalize_layers([{"png": rect_mask(SIZE, BOX), "kind": "shape"}], *SIZE)
    c1, i1 = M.normalize_layers(
        [{"png": rect_mask(SIZE, BOX), "kind": "shape", "grow": 12, "feather": 8}], *SIZE)
    check("layers: shape grow enlarges the selection",
          _selected(c1) > _selected(c0), f"{_selected(c0)} -> {_selected(c1)}")
    check("layers: shape keeps its feather",
          float(i1["layers"][0]["feather"]) == 8.0, i1["layers"][0])
    check("layers: shape IS thresholded", i1["layers"][0]["thresholded"] is True, i1["layers"][0])
    cn, _ = M.normalize_layers(
        [{"png": rect_mask(SIZE, BOX), "kind": "shape", "shrink": 12}], *SIZE)
    check("layers: shape shrink reduces the selection", _selected(cn) < _selected(c0),
          f"{_selected(c0)} -> {_selected(cn)}")

    # --- C2: the 'auto' kind (smart-select / CLIPSeg) is a first-class layer, not a brush.
    # The browser's exportLayers used to skip every stroke without a vertex list, so a
    # smart-select PLUS a brush stroke shipped layers WITHOUT the SAM3 mask -- and because
    # the server prefers layers over mask_png, the selected object silently vanished from the
    # render. This pins that 'auto' is honoured (thresholded + growable like a shape).
    a0, _ = M.normalize_layers([{"png": rect_mask(SIZE, BOX), "kind": "auto"}], *SIZE)
    a1, ia1 = M.normalize_layers(
        [{"png": rect_mask(SIZE, BOX), "kind": "auto", "grow": 12, "feather": 8}], *SIZE)
    check("layers: an auto layer is thresholded (shape rule, NOT the brush no-threshold rule)",
          ia1["layers"][0]["thresholded"] is True, ia1["layers"][0])
    check("layers: an auto layer CAN grow (smart-select under-selects, so grow is honoured)",
          _selected(a1) > _selected(a0), f"{_selected(a0)} -> {_selected(a1)}")
    check("layers: an auto layer keeps its feather",
          float(ia1["layers"][0]["feather"]) == 8.0, ia1["layers"][0])

    # --- C3: the shape feather must be OUTWARD and DISC-round, at the server ---------
    # The render path used to Gaussian-blur shape layers symmetrically, which pulls the
    # 128-crossing INWARD: the binarised footprint the graph binarises shrinks under the
    # very slider meant to only soften, and the ramp points into the object (the user's
    # 'render feathers towards the inside' report, while the client preview — disc-grow +
    # blur — looked right). The shape branch now shares auto's masks._feather_out, and so
    # must every future kind. These gates read the canonical mask itself.
    _F = 12
    _sf0, _ = M.normalize_layers([{"png": rect_mask(SIZE, BOX), "kind": "shape"}], *SIZE)
    _sfc, _ = M.normalize_layers([{"png": rect_mask(SIZE, BOX), "kind": "shape",
                                   "feather": _F}], *SIZE)
    _c0 = Image.open(io.BytesIO(_sf0)).convert("RGBA").split()[3]
    _c1 = Image.open(io.BytesIO(_sfc)).convert("RGBA").split()[3]
    _solid0 = sum(1 for y in range(SIZE[1]) for x in range(SIZE[0]) if _c0.getpixel((x, y)) > 128)
    _solid1 = sum(1 for y in range(SIZE[1]) for x in range(SIZE[0]) if _c1.getpixel((x, y)) > 128)
    check("render contract: feathering a SHAPE never shrinks its >=128 footprint",
          _solid1 >= _solid0, f"{_solid0} -> {_solid1} (symmetric blur pulls it inward)")
    _inside_edge = _c1.getpixel((BOX[0] + 1, (BOX[1] + BOX[3]) // 2))
    check("render contract: the solid core survives the feather (interior stays 255)",
          _inside_edge == 255, f"1px inside boundary alpha={_inside_edge}")
    _skirt_mid = _c1.getpixel((BOX[2] + (_F + 2), (BOX[1] + BOX[3]) // 2))
    check("render contract: the soft skirt reaches OUTSIDE the approved edge",
          _skirt_mid > 50, f"alpha at f+2 beyond right edge = {_skirt_mid}")
    # Disc vs Chebyshev on the >=128 SILHOUETTE, not on raw tail alpha: a blurred alpha
    # probe cannot tell the kernels apart (the square grow's corner bulge cancels the
    # extra blur distance — measured: a Chebyshev _feather_out passes an alpha-tail probe
    # cleanly). The silhouette's REACH in the diagonal direction is the honest signal:
    # disc ~ f + ~0.4f, Chebyshev ~ f*sqrt(2) + ~0.4f. Side reach is the reference.
    def _reach(px, x_axis):
        # how far past the box the >128 silhouette extends, scanning from 3f out
        for d in range(3 * _F, 1, -1):
            if x_axis:
                if _c1.getpixel((BOX[2] + d, (BOX[1] + BOX[3]) // 2)) > 128:
                    return d
            else:
                if _c1.getpixel((BOX[2] + d, BOX[3] + d)) > 128:
                    return d
        return 1
    _r_side, _r_corner = _reach(_c1, True), _reach(_c1, False)
    check("render contract: feather skirt rounds off at the corner (disc, not Chebyshev)",
          _r_corner <= _r_side + 3,
          f"diag reach {_r_corner} vs side {_r_side} (square grow bulges ~{(2**0.5-1)*_F:.0f}px)")

    # --- D: mixing kinds in one mask applies per-object rules -----------------------
    cmix, imix = M.normalize_layers(
        [{"png": brush_ramp_mask(SIZE, BOX, 0.4), "kind": "brush"},
         {"png": rect_mask(SIZE, (20, 20, 60, 60)), "kind": "shape", "feather": 10}], *SIZE)
    kinds = {l["kind"]: l for l in imix["layers"]}
    check("layers: brush + shape coexist with different rules",
          float(kinds["brush"]["feather"]) == 0.0 and float(kinds["shape"]["feather"]) == 10.0,
          imix["layers"])

    # --- E: an eraser layer subtracts -----------------------------------------------
    cpaint, _ = M.normalize_layers([{"png": rect_mask(SIZE, BOX), "kind": "shape"}], *SIZE)
    cerase, _ = M.normalize_layers(
        [{"png": rect_mask(SIZE, BOX), "kind": "shape"},
         {"png": brush_ramp_mask(SIZE, BOX, 1.0), "kind": "brush", "erase": True}], *SIZE)
    check("layers: an eraser layer subtracts from what is under it",
          _selected(cerase) < _selected(cpaint),
          f"{_selected(cpaint)} -> {_selected(cerase)}")

    # --- E2: what per-fragment morphology does to a split, and where erase layers fit --
    # Server semantics the client depends on, pinned as facts (not as the client's
    # strategy — the client stopped shipping erase layers for the eraser, see
    # toolbox.js foldLiveGeometry):
    #   * Give the server two feathered/grown fragments of a split blob and it genuinely
    #     HEALS the channel: each fragment dilates outward from its own cut edge, which is
    #     exactly what the user reported as "the two halves are bigger and overlap". No
    #     client-side drawing bug — it is what per-layer morphology must produce. The fix
    #     is therefore to hand the server a silhouette that already IS the object, i.e. to
    #     fold grow into the pixels at the split, not to keep punching afterwards.
    #   * A punch that ships LAST does seal it, and that path stays correct for whole
    #     OBJECT deletions (deleteSel) — but it must never be used for a cut the user can
    #     undo by moving objects: the punch sits still in canvas space while the objects
    #     move, which is how a "latent gap" appears in the middle of a joined object.
    #   * The punch must spare the fragments themselves (it lifts, it does not halve).
    frag_t = rect_mask(SIZE, (110, 110, 210, 149))     # split at rows 150..160
    frag_b = rect_mask(SIZE, (110, 161, 210, 200))
    sweep = rect_mask(SIZE, (100, 147, 220, 163))      # a 6-px-wide eraser is thin; 16 is decisive
    def _row_sel(canon, y=155):
        ch = Image.open(io.BytesIO(canon)).convert("RGBA").split()[3]
        return sum(1 for x in range(115, 206) if ch.getpixel((x, y)) > 128)
    healed, _ = M.normalize_layers(
        [{"png": frag_t, "kind": "shape", "grow": 4, "feather": 12},
         {"png": frag_b, "kind": "shape", "grow": 4, "feather": 12}], *SIZE)
    sealed, _ = M.normalize_layers(
        [{"png": frag_t, "kind": "shape", "grow": 4, "feather": 12},
         {"png": frag_b, "kind": "shape", "grow": 4, "feather": 12},
         {"png": sweep, "kind": "brush", "erase": True}], *SIZE)
    check("layers: WITHOUT the shipped erase the server heals the split (defect pinned)",
          _row_sel(healed) > 80, f"mid-gap selected={_row_sel(healed)}")
    check("layers: an erase layer shipped LAST keeps the split channel CUT (union punch wins)",
          _row_sel(sealed) < 10, f"mid-gap selected={_row_sel(sealed)}")
    check("layers: that punch spares the fragments themselves (both halves survive)",
          _selected(sealed) > _selected(healed) * 0.5,
          f"selected={_selected(sealed)} vs healed {_selected(healed)}")

    # --- F: invert acts on the union, never per layer -------------------------------
    # Overlapping inverted layers max-composited would resurrect the overlap, which is
    # not what "edit everything outside what I marked" means.
    ua, _ = M.normalize_layers(
        [{"png": rect_mask(SIZE, (20, 20, 150, 150)), "kind": "shape"},
         {"png": rect_mask(SIZE, (100, 100, 250, 250)), "kind": "shape"}], *SIZE)
    a, _ = M.normalize_layers(
        [{"png": rect_mask(SIZE, (20, 20, 150, 150)), "kind": "shape"},
         {"png": rect_mask(SIZE, (100, 100, 250, 250)), "kind": "shape"}], *SIZE, invert=True)
    total = SIZE[0] * SIZE[1]
    check("layers: invert is the complement of the UNION",
          _selected(a) == total - _selected(ua), f"{_selected(a)} vs {total - _selected(ua)}")

    # --- G: paint unions, and never compounds past opaque ---------------------------
    # ImageChops.lighter, not add: two half-covered passes must not make a brighter pixel.
    half = brush_ramp_mask(SIZE, BOX, 0.5)
    one, _ = M.normalize_layers([{"png": half, "kind": "brush"}], *SIZE)
    two, _ = M.normalize_layers([{"png": half, "kind": "brush"},
                                 {"png": half, "kind": "brush"}], *SIZE)
    check("layers: duplicate paint is a union, not an additive sum",
          _selected(one) == _selected(two), f"{_selected(one)} vs {_selected(two)}")

    # --- G2: BRUSH CORE LIFT -- the "dog stayed latent" guard (live GPU defect) -------
    # ComfyUI's VAEEncodeForInpaint computes m = 1 - mask.round() and noise_mask =
    # mask.round(): every mask pixel below 128/255 is "NOT INPAINT" -- the source image's
    # latents ride straight through and the sampler hands back the original. A soft
    # brush's accumulated interior parks exactly there (hardness 0.55 leaves 45% of the
    # disc as single-pass gradient), so a hand paint that LOOKS solid can be mid-grey
    # alpha, and the model never sees the region at all. The canonical mask must arrive
    # WHITE in the middle, fading only at the perimeter.
    def _flat_alpha(size, box, a):
        im = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(im).rectangle(list(box), fill=(255, 255, 255, a))
        buf = io.BytesIO(); im.save(buf, "PNG")
        return buf.getvalue()

    ramp = Image.new("L", (1, 10))
    for _i, _v in enumerate([0, 25, 50, 63, 64, 100, 127, 128, 200, 255]):
        ramp.putpixel((0, _i), _v)
    _got = list(M._lift_brush_core(ramp).tobytes())
    check("lift: >=half -> opaque, <half -> x2, continuous across 128, zero stays zero",
          _got == [0, 50, 100, 126, 128, 200, 254, 255, 255, 255], _got)

    cg, _ig = M.normalize_layers([{"png": _flat_alpha(SIZE, BOX, 150), "kind": "brush"}],
                                 *SIZE)
    ag = M.extract_coverage(M._open(cg))
    check("lift: a mid-alpha painted interior reaches the graph WHITE (the dog)",
          ag.getpixel(((BOX[0] + BOX[2]) // 2, (BOX[1] + BOX[3]) // 2)) == 255,
          "centre alpha %s" % (ag.getpixel(((BOX[0] + BOX[2]) // 2,
                                            (BOX[1] + BOX[3]) // 2)),))

    soft2 = brush_ramp_mask(SIZE, BOX, 0.45)
    raw2 = M.extract_coverage(M._open(soft2)).tobytes()
    c2, _i2 = M.normalize_layers([{"png": soft2, "kind": "brush"}], *SIZE)
    new2 = M.extract_coverage(M._open(c2)).tobytes()
    _cross = sum(1 for r in raw2 if r >= 128)
    _fade = sum(1 for r, g in zip(raw2, new2) if r < 128 and 0 < g < 255)
    check("lift: whole-stroke property -- every pixel lifted per the levels curve",
          all((g == 255) if r >= 128 else (g == r * 2) for r, g in zip(raw2, new2))
          and _cross > 0 and _fade > 0,
          "crossed %d, still-fading %d" % (_cross, _fade))

    cE, _ = M.normalize_layers([{"png": _flat_alpha(SIZE, BOX, 200), "kind": "brush"},
                                {"png": _flat_alpha(SIZE, (BOX[0] + 4, BOX[1] + 4,
                                                           BOX[2] - 4, BOX[3] - 4), 100),
                                 "kind": "brush", "erase": True}], *SIZE)
    aE = M.extract_coverage(M._open(cE))
    check("lift: the ERASER keeps its soft ramp -- a half-pass erase halves, not deletes",
          abs(aE.getpixel(((BOX[0] + BOX[2]) // 2, (BOX[1] + BOX[3]) // 2)) - 155) <= 1,
          "erased-at-100 alpha %s (want ~155: lifted 255 minus the 100/255 erase)"
          % (aE.getpixel(((BOX[0] + BOX[2]) // 2, (BOX[1] + BOX[3]) // 2)),))

    cZ, iZ = M.normalize_layers([{"png": _flat_alpha(SIZE, BOX, 0), "kind": "brush"}],
                                *SIZE)
    check("lift: an empty paint stays empty (the coverage floor still fires)",
          iZ["coverage_paint"] == 0.0, iZ)

    # --- H: empty + response shape --------------------------------------------------
    _, iempty = M.normalize_layers([], *SIZE)
    check("layers: no layers selects nothing", iempty["coverage_paint"] == 0.0, iempty)
    check("layers: info is response-compatible with normalize()",
          {"size", "coverage_paint", "coverage_after", "layers", "inverted"} <= set(iempty),
          sorted(iempty))
    ov = M.overlay_layers(PHOTO, [{"png": rect_mask(SIZE, BOX), "kind": "shape"}], *SIZE)
    check("layers: overlay_layers is a decodable PNG at working size",
          Image.open(io.BytesIO(ov)).size == SIZE, len(ov) if ov else None)
    check("layers: overlay_layers actually tints (differs from an empty mask)",
          ov != M.overlay_layers(PHOTO, [], *SIZE), "identical to blank overlay")

    # --- I: the ROUTE honours per-object params end to end ---------------------------
    # masks.* is unit-tested above; the browser suite stubs fetch(); so this is the only
    # place that proves api.py actually ROUTES a layered body into normalize_layers rather
    # than quietly falling back to the single global-parameter mask.
    tb = make_tb()
    tok = T.mint(SECRET, scope="launch", email=EMAIL)
    soft = brush_ramp_mask(SIZE, BOX, 0.45)
    hard = rect_mask(SIZE, (20, 20, 70, 70))
    layered = {
        "image_id": "img",
        "mask_png": MASK_B64,
        "layers": [
            {"png": base64.b64encode(soft).decode(), "kind": "brush",
             "grow": 0, "shrink": 0, "feather": 30},
            {"png": base64.b64encode(hard).decode(), "kind": "shape",
             "grow": 12, "shrink": 0, "feather": 4},
        ],
        "spec": {"width": SIZE[0], "height": SIZE[1], "prompt": "smooth it",
                 # global values a brush must NOT inherit:
                 "mask_feather": 30, "mask_expand": 0, "mask_shrink": 0},
    }
    r = post(tb, "/toolbox/mask/preview", layered, token=tok)
    lay = ((r.payload or {}).get("info") or {}).get("layers") or []
    check("route: layered preview succeeds", (r.status or 0) // 100 == 2, r.payload)
    check("route: preview went through the LAYERED normalizer",
          len(lay) == 2 and (r.payload or {}).get("info", {}).get("per_object") is True, lay)
    kinds = {l.get("kind"): l for l in lay}
    check("route: brush layer arrived with feather SUPPRESSED, not the global 30",
          float(kinds.get("brush", {}).get("feather", -1)) == 0.0, kinds)
    check("route: shape layer kept the per-object grow it was given",
          int(kinds.get("shape", {}).get("grow", -1)) == 12, kinds)
    check("route: preview overlay came back", bool((r.payload or {}).get("overlay_png")))

    # The 'auto' (SAM3 smart-select) layer must obey per-object grow/shrink/feather for REAL,
    # not just echo the number back. The earlier suite only asserted the info ECHO carried the
    # requested grow, which is exactly the weak check that let "re-render but nothing changes"
    # survive: echoing 12 is not proof the mask moved. Compare returned COVERAGE across edge
    # values so a grow that silently no-ops on an auto layer fails here.
    _auto = base64.b64encode(rect_mask(SIZE, (30, 30, 80, 80))).decode()
    def _cov(edge, feather=0):
        rr = post(tb, "/toolbox/mask/preview", {
            "image_id": "img", "mask_png": MASK_B64,
            "layers": [{"png": _auto, "kind": "auto", "edge": edge,
                        "grow": max(0, edge), "shrink": max(0, -edge), "feather": feather}],
            "spec": {"width": SIZE[0], "height": SIZE[1]},
        }, token=tok)
        return (rr.payload or {}).get("coverage"), (rr.payload or {}).get("info", {}).get("layers", [{}])
    c0, li0 = _cov(0)
    cg, lig = _cov(10)
    cs, lis = _cov(-10)
    check("route: an AUTO layer's coverage GROWS with a positive edge (per-object morph is real)",
          c0 is not None and cg is not None and cg > c0, {"edge0": c0, "edge+10": cg})
    check("route: an AUTO layer's coverage SHRINKS with a negative edge",
          cs is not None and c0 is not None and cs < c0, {"edge0": c0, "edge-10": cs})
    check("route: an AUTO layer reports the edge it actually applied",
          li0 and int((lig or [{}])[0].get("edge_applied", 0)) == 10, lig)

    # A body with no layers must still work: the spike, older editors and any
    # hand-built request rely on the single-mask path.
    r2 = post(tb, "/toolbox/mask/preview",
              {"image_id": "img", "mask_png": MASK_B64,
               "spec": {"width": 512, "height": 512, "mask_feather": 6}}, token=tok)
    check("route: single-mask (no layers) body still previews",
          (r2.status or 0) // 100 == 2 and "layers" not in ((r2.payload or {}).get("info") or {}),
          r2.payload)

    # Garbage layers are refused, not silently downgraded: an unknown kind must not be
    # allowed to fall through to the lenient brush rule, and an oversized layer list is a
    # CPU-denial shape (each layer costs a resize + morphology at working dims).
    r3 = post(tb, "/toolbox/mask/preview",
              dict(layered, layers=[{"png": base64.b64encode(soft).decode(),
                                      "kind": "not-a-kind", "feather": 40}]), token=tok)
    lay3 = ((r3.payload or {}).get("info") or {}).get("layers") or []
    check("route: unknown layer kind is clamped to the STRICTEST rule, not the brush rule",
          lay3 and lay3[0]["kind"] == "shape" and lay3[0]["thresholded"] is True, lay3)
    r4 = post(tb, "/toolbox/mask/preview",
              dict(layered, layers=[{"png": base64.b64encode(soft).decode(), "kind": "brush"}]
                                    * (tb.MAX_LAYERS + 1)), token=tok)
    check("route: an absurd layer count is refused", (r4.status or 0) >= 400,
          f"{r4.status} {str(r4.payload)[:90]}")
    r5 = post(tb, "/toolbox/mask/preview",
              dict(layered, layers=[{"png": base64.b64encode(soft).decode(), "kind": "brush",
                                      "grow": 99999, "shrink": 99999}]), token=tok)
    lay5 = ((r5.payload or {}).get("info") or {}).get("layers") or []
    check("route: per-layer grow is CLAMPED, not trusted",
          lay5 and int(lay5[0]["grow"]) <= M.MAX_MORPH_PX, lay5)

    # --- N: the signed one-axis protocol, through the ROUTE ---------------------------
    # Unit tests above call normalize_layers directly; this proves api._layers_from actually
    # reads `edge` and hands it down, and that grow/shrink stay consistent with it -- the
    # browser sends all three, and a server that ignored `edge` while zeroing grow/shrink
    # would render the user's edge knob as a no-op with a green offline suite.
    e_neg = dict(layered, layers=[{"png": base64.b64encode(soft).decode(), "kind": "shape",
                                   "edge": -18, "grow": 0, "shrink": 0, "feather": 0}])
    r6 = post(tb, "/toolbox/mask/preview", e_neg, token=tok)
    l6 = ((r6.payload or {}).get("info") or {}).get("layers") or []
    check("route: a signed negative edge arrives as a shrink",
          l6 and l6[0]["edge_applied"] < 0, l6)
    check("route: grow/shrink stay consistent with the signed edge",
          l6 and int(l6[0]["grow"]) == 0 and int(l6[0]["edge_applied"]) == -18, l6)
    e_pos = dict(layered, layers=[{"png": base64.b64encode(soft).decode(), "kind": "shape",
                                   "edge": 18}])
    r7 = post(tb, "/toolbox/mask/preview", e_pos, token=tok)
    l7 = ((r7.payload or {}).get("info") or {}).get("layers") or []
    check("route: a signed positive edge arrives as a grow",
          l7 and int(l7[0]["grow"]) == 18 and int(l7[0]["edge_applied"]) == 18, l7)
    check("route: the layer reports the object scale the % was taken against",
          l7 and l7[0]["obj_min_side"] > 0 and l7[0]["edge_pct"] > 0, l7)
    # both protocols at once must not double-count: edge wins, pair is derived from IT
    both = dict(layered, layers=[{"png": base64.b64encode(soft).decode(), "kind": "shape",
                                 "edge": 8, "grow": 40, "shrink": 40}])
    r8 = post(tb, "/toolbox/mask/preview", both, token=tok)
    l8 = ((r8.payload or {}).get("info") or {}).get("layers") or []
    check("route: when edge and the legacy pair disagree, edge governs",
          l8 and int(l8[0]["grow"]) == 8 and int(l8[0]["shrink"]) == 0, l8)
    # and the server-side clamp must survive the route too
    r9 = post(tb, "/toolbox/mask/preview",
              dict(layered, layers=[{"png": base64.b64encode(soft).decode(),
                                    "kind": "shape", "edge": -99999}]), token=tok)
    l9 = ((r9.payload or {}).get("info") or {}).get("layers") or []
    check("route: an absurd signed edge is CLAMPED, and never annihilates",
          l9 and abs(int(l9[0]["edge_applied"])) <= M.MAX_MORPH_PX
          and (r9.payload or {}).get("empty") is not True, l9)

    # --- J: the INNER morph cap, asserted without paying for it ----------------------
    # api.py clamps grow/shrink before masks ever sees them, so the route check above cannot
    # distinguish "the API clamped" from "the filter clamped" -- reverting masks._morph's
    # cap leaves it green (verified: it does). The cap is what bounds the WORK, and it matters
    # enormously: an unclamped 10 000 px request was measured taking >30 s on a 200x200 image,
    # which is a request-level denial-of-service on the render worker.
    #
    # _morph implements a radius by walking a 3x3 filter `px` times (composing square
    # structuring elements is exactly equivalent to one (2r+1) kernel, and ~14x cheaper), so
    # the quantity that must be capped is the NUMBER OF PASSES, not a kernel size. Asserting
    # passes keeps the test about the guarantee instead of the implementation: a future change
    # back to direct kernels, or to a different decomposition, stays under test either way.
    built = []

    class _Kernel:
        def __init__(self, k): self.k = k

    class _FakeCov:
        def filter(self, f):
            built.append(f.k)
            return self

    _real_filter = M.ImageFilter

    class _Shim:
        MaxFilter = staticmethod(_Kernel)
        MinFilter = staticmethod(_Kernel)

    def _count_morph_calls(px, grow):
        del built[:]
        M.ImageFilter = _Shim
        try:
            M._morph(_FakeCov(), px, grow=grow)
        finally:
            M.ImageFilter = _real_filter
        return list(built)

    grown = _count_morph_calls(10_000, True)
    shrunk = _count_morph_calls(10_000, False)
    # What must be bounded is the WORK, not the shape of the implementation. _morph currently
    # gets there by walking a 3x3 filter MAX_MORPH_PX times; PIL's own (2r+1) kernel reaches
    # the identical pixels in ONE call (proved identical by the comparison below, and by the
    # direct_kernel revert). An earlier version of this check asserted "exactly MAX_MORPH_PX
    # passes", which the direct-kernel implementation failed despite producing byte-identical
    # masks -- a test that punishes a faster route to the same answer is testing its own
    # assumptions, so both the pass count and the kernel width are bounded instead.
    cap_calls = 2 * M.MAX_MORPH_PX
    cap_kernel = 2 * M.MAX_MORPH_PX + 1
    check("layers: an absurd radius is refused BEFORE the work is done (no 10000-pass walk)",
          len(grown) <= cap_calls and len(shrunk) <= cap_calls,
          "%d / %d filter calls vs cap %d" % (len(grown), len(shrunk), cap_calls))
    check("layers: no kernel wider than MAX_MORPH_PX is ever requested",
          all(k <= cap_kernel for k in grown + shrunk) and len(grown) > 0,
          "widest requested %s vs cap %d" % (max(grown + shrunk) if (grown or shrunk) else 0,
                                             cap_kernel))
    check("layers: a sane radius still does real work (the guard is not a no-op)",
          _count_morph_calls(3, True) and len(_count_morph_calls(3, True)) <= cap_calls,
          _count_morph_calls(3, True))
    check("layers: _morph runs no filter at all for a zero radius",
          _count_morph_calls(0, True) == [], _count_morph_calls(0, True))
    check("layers: grow and shrink are both bounded (neither direction lost its cap)",
          len(grown) == len(shrunk) and max(grown) == max(shrunk),
          "%d calls widest=%s vs %d calls widest=%s"
          % (len(grown), max(grown), len(shrunk), max(shrunk)))

    # --- K: the composition trick must be EXACTLY the kernel it replaced --------------
    # _morph walks a 3x3 filter r times instead of asking PIL for one (2r+1) kernel, on the
    # grounds that square structuring elements compose. If that ever stops being true every
    # mask in the product is subtly wrong, and SILENTLY wrong, because the output still looks
    # like a mask. So compare raw pixels against the direct kernel, both directions, across
    # the range, straddling any internal threshold, on shapes with THIN material where an
    # off-by-one would actually show.
    from PIL import ImageFilter as _IF
    for _label, _paint in (("blob", lambda d: d.ellipse([50, 50, 350, 350], fill=255)),
                           ("ring", lambda d: (d.ellipse([50, 50, 350, 350], fill=255),
                                               d.ellipse([150, 150, 250, 250], fill=0)))):
        _im = Image.new("L", SIZE, 0)
        _paint(ImageDraw.Draw(_im))
        for _grow in (True, False):
            _bad = [_r for _r in (1, 3, 8, 9, 12, 13, 20, 31, 48, 64)
                    if M._morph(_im, _r, grow=_grow).tobytes()
                    != _im.filter(_IF.MaxFilter(2 * _r + 1) if _grow
                                  else _IF.MinFilter(2 * _r + 1)).tobytes()]
            check("layers: composed erosion matches the direct kernel [%s grow=%s]"
                  % (_label, _grow), not _bad, "mismatched radii %s" % _bad)

    # --- K2: the CIRCULAR (Euclidean) smart-select morphology, denoise, outward feather --
    # These prove the four smart-select fixes at the source, independent of the HTTP layer.
    from PIL import ImageDraw as _IDraw

    def _sel_l(im, th=50):
        return sum(im.histogram()[th + 1:])

    # (a) a lone pixel must grow into a DISC, not a square: axis edge filled, corners empty,
    #     and area near pi*r^2 -- NOT (2r+1)^2. The square _morph is shown for contrast so the
    #     assertion is meaningful (verified: at r=6 the square fills a 13x13=169 block where a
    #     disc fills ~113). This is the regression lock for "everything grows into a square".
    for _r in (4, 8, 12):
        _cov = Image.new("L", SIZE, 0)
        _cx, _cy = SIZE[0] // 2, SIZE[1] // 2
        if min(SIZE) < 2 * _r + 5:
            continue
        _cov.putpixel((_cx, _cy), 255)
        _sq = M._morph(_cov, _r, grow=True)
        _ds = M._morph_disk(_cov, _r, grow=True)
        _d = _ds.load()
        _corner = (_d[_cx - _r, _cy - _r] > 50) or (_d[_cx + _r, _cy + _r] > 50)
        _axis = _d[_cx - _r, _cy] > 50 and _d[_cx, _cy + _r] > 50
        _a_disc, _a_sq = _sel_l(_ds), _sel_l(_sq)
        _ideal = 3.14159 * _r * _r
        check("auto-morph: disc grow leaves the corners EMPTY (a square would fill them) r=%d" % _r,
              _corner is False and _a_sq > _a_disc,
              "disc=%d square=%d corner=%s" % (_a_disc, _a_sq, _corner))
        check("auto-morph: disc grow fills the axis edge and is round (area ~ pi r^2) r=%d" % _r,
              _axis and abs(_a_disc - _ideal) / _ideal < 0.18,
              "area=%d ideal=%d axis=%s" % (_a_disc, int(_ideal), _axis))

    # (b) keep_significant_components: drop a stray speckle, KEEP a deliberately-added part
    #     (a second large island) -- the user's hat/backpack case.
    def _rgba(png_on):
        im = Image.new("RGBA", SIZE, (0, 0, 0, 0))
        dd = ImageDraw.Draw(im)
        for box in png_on:
            dd.rectangle(box, fill=(255, 255, 255, 255))
        buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()

    _big = (10, 10, 50, 50)                 # the object (dominant)
    _part = (60, 60, 90, 88)                # a separate, sizeable added part -> keep
    _speck = (5, 60, 6, 61)                 # a 2x2 decoder-noise speckle -> drop
    _den = M.keep_significant_components(_rgba([_big, _part, _speck]))
    _dcov = M.extract_coverage(M._open(_den))
    _dd = _dcov.load()
    _in = lambda xy: (0 <= xy[0] < SIZE[0] and 0 <= xy[1] < SIZE[1]
                      and _dd[xy[0], xy[1]] > 50)
    check("denoise: the dominant blob survives",
          _in(((30, 30))) and _in(((75, 74))), "big/part pixels missing")
    check("denoise: the stray speckle is removed", not _in(((5, 60))), "speckle survived")
    # A lone selection (single connected region) is the OBJECT — never delete it, however
    # small: dropping the only thing a user selected is worse than a stray speckle. Guards
    # both over-deletion and the background-fill bug (out_rows must be gated on `flat`).
    _only = M.keep_significant_components(_rgba([(5, 5, 6, 6)]))
    _onlycov = M.extract_coverage(M._open(_only))
    check("denoise: a single (even tiny) region is the object and is KEPT, never deleted",
          _sel_l(_onlycov) > 0, _sel_l(_onlycov))
    # and a mask with a dominant blob + noise keeps the blob and drops noise (not fill-to-frame)
    _den2 = M.keep_significant_components(_rgba([_big, _speck]))
    _dcov2 = M.extract_coverage(M._open(_den2))
    _dd2 = _dcov2.load()
    _tot = _sel_l(_dcov2)
    check("denoise: dominant blob kept, speckle dropped, background stays empty",
          (0 <= 30 < SIZE[0] and _dd2[30, 30] > 50) and not _dd2[5, 60] and
          _tot < (SIZE[0] * SIZE[1]),   # not filled to the whole frame
          "total_selected=%d of %d" % (_tot, SIZE[0] * SIZE[1]))

    # (c) outward feather: the >=128 footprint never shrinks vs the hard shape, and coverage
    #     appears OUTSIDE the original box (the old symmetric blur pulled it inward).
    _box = (20, 20, 59, 59)
    _scov = Image.new("L", SIZE, 0)
    ImageDraw.Draw(_scov).rectangle(_box, fill=255)
    _before = sum(1 for y in range(SIZE[1]) for x in range(SIZE[0]) if _scov.getpixel((x, y)) > 128)
    _fc = M._feather_out(_scov, 6)
    _after = sum(1 for y in range(SIZE[1]) for x in range(SIZE[0]) if _fc.getpixel((x, y)) > 128)
    _outside = any(_fc.getpixel((x, y)) > 50
                   for x in (_box[0] - 3,) for y in range(_box[1] + 1) if 0 <= x < SIZE[0])
    check("auto-feather: outward feather never shrinks the >=128 core",
          _after >= _before, "before=%d after=%d" % (_before, _after))
    check("auto-feather: the soft skirt extends BEYOND the original edge",
          _outside, "no coverage found outside the box (feather must bias outward, not inward)")

    # Silent by nature: a mask selecting nothing paints nothing, and the only way the user
    # learns is a 13-170 s render of their untouched photo. `tiny` is the obvious case (a
    # 20 px shape dies at radius 12). `ring` is the case a BBOX-based guard would MISS: its
    # bounding box is 300 px but its material is ~100 px, so it dies far earlier than the box
    # implies -- and it is reachable by the ordinary gesture of painting a blob and erasing
    # the middle out of it.
    _ring = _layer_png(SIZE, lambda d: (d.ellipse([50, 50, 350, 350], fill=255),
                                        d.ellipse([150, 150, 250, 250], fill=0)))
    _tiny = _layer_png(SIZE, lambda d: d.rectangle([190, 190, 210, 210], fill=255))
    for _label, _src in (("ring", _ring), ("tiny", _tiny), ("shape", MASK)):
        _prev, _mono, _alive, _applied = None, True, True, []
        for _e in (-2, -4, -8, -12, -16, -20, -24, -32, -40, -48, -56, -64):
            _c, _i = M.normalize_layers([{"png": _src, "kind": "shape", "edge": _e}], *SIZE)
            _n = _selected(_c)
            _applied.append(_i["layers"][0]["edge_applied"])
            if _prev is not None and _n > _prev:
                _mono = False            # dragging the slider LEFT produced MORE mask
            if _n == 0:
                _alive = False
            _prev = _n
        check("layers: shrink never annihilates the selection [%s]" % _label, _alive, _applied)
        check("layers: shrink stays monotonic in the requested radius [%s]" % _label,
              _mono, _applied)
        check("layers: shrink reports the radius it actually applied [%s]" % _label,
              all(a <= 0 for a in _applied), _applied)

    # --- M: one axis, so the hidden closing state cannot be expressed -----------------
    # grow=12 with shrink=12 used to be reachable, reads as "no change", and is actually a
    # morphological CLOSING: measured, it took a notched shape from 9704 to 10251 px by
    # filling the notch. One signed value leaves exactly one no-op.
    c_plain, _i0 = M.normalize_layers([{"png": MASK, "kind": "shape", "edge": 0}], *SIZE)
    c_neg, _in = M.normalize_layers([{"png": MASK, "kind": "shape", "edge": -12}], *SIZE)
    c_pos, _ip = M.normalize_layers([{"png": MASK, "kind": "shape", "edge": 12}], *SIZE)
    check("layers: one signed axis gives one no-op and both directions move",
          _selected(c_neg) < _selected(c_plain) < _selected(c_pos),
          "%s < %s < %s" % (_selected(c_neg), _selected(c_plain), _selected(c_pos)))
    le = _ip["layers"][0]
    check("layers: a layer reports the object scale its offset was measured against",
          le["obj_min_side"] > 0 and le["edge_pct"] > 0, le)
    check("layers: edge_pct derives from the APPLIED radius, not the requested one",
          abs(le["edge_pct"] - round(100.0 * abs(le["edge_applied"]) / le["obj_min_side"], 1))
          < 0.11, le)
    _bc, _ = M.normalize_layers(
        [{"png": brush_ramp_mask(SIZE, BOX, 0.55), "kind": "brush"}], *SIZE)
    _hc, _hi = M.normalize_layers(
        [{"png": brush_ramp_mask(SIZE, BOX, 0.55), "kind": "brush", "edge": -40}], *SIZE)
    check("layers: a shrink request cannot thin a brush stroke (hardness owns that edge)",
          _selected(_bc) == _selected(_hc) > 0,
          "%s vs %s" % (_selected(_bc), _selected(_hc)))


# ---------------------------------------------------------------- 1. tokens
def test_tokens():
    t = T.mint(SECRET, scope="launch", email="A@B.C", chat_id="c1")
    p = T.verify(SECRET, t, scope="launch")
    check("token roundtrip + email normalised", p["email"] == "a@b.c" and p["chat_id"] == "c1")
    try:
        T.verify(SECRET, t, scope="launch"); check("launch token is single-use", False)
    except T.Replay:
        check("launch token is single-use", True)
    t2 = T.mint(SECRET, scope="launch", email="a@b.c")
    try:
        T.verify(SECRET, t2, scope="job"); check("scope is enforced", False)
    except T.WrongScope:
        check("scope is enforced", True)
    try:
        T.verify("other-secret", t2, scope="launch"); check("signature is enforced", False)
    except T.BadToken:
        check("signature is enforced", True)
    for bad in ("", "x", "v9.a.b", "v1.nope.nope"):
        try:
            T.verify(SECRET, bad, scope="launch"); check(f"malformed/forged refused", False)
        except T.TokenError:
            check(f"malformed/forged refused ({bad[:8]!r})", True)
    expired = T.mint(SECRET, scope="launch", email="a@b.c", ttl_s=-1)
    try:
        T.verify(SECRET, expired, scope="launch", single_use=False)
        check("expiry enforced", False)
    except T.Expired:
        check("expiry enforced", True)
    anon = T.mint(SECRET, scope="launch", email="   ")
    try:
        T.verify(SECRET, anon, scope="launch", single_use=False)
        check("anonymous token refused", False)
    except T.Anonymous:
        check("anonymous token refused", True)
    jt = T.mint(SECRET, scope="job", email="a@b.c", job_id="j1")
    T.verify(SECRET, jt, scope="job"); T.verify(SECRET, jt, scope="job")
    check("job token is reusable by design", True)
    try:
        T.mint("", scope="launch", email="a@b.c"); check("mint refuses an empty secret", False)
    except ValueError:
        check("mint refuses an empty secret", True)


# ---------------------------------------------------------------- 2. mask semantics
def test_masks():
    check("masks: PIL present", M.HAS_PIL)
    area = (300 - 200 + 1) * (250 - 150 + 1) / (640 * 480)
    # the two ways a mask arrives must agree on what is selected
    check("masks: alpha polarity (our painter)", abs(M.coverage(MASK) - area) < 0.005)
    dood = doodle_mask((640, 480), (200, 150, 300, 250))
    check("masks: luminance polarity (markup app)", abs(M.coverage(dood) - area) < 0.005)
    # an opaque-alpha export (Photoshop/screenshot) must fall back to luminance rather
    # than be read as "everything is selected"
    opaque = png_bytes("RGBA", (640, 480), lambda x, y, s:
                       (255, 255, 255, 255) if (200 <= x <= 300 and 150 <= y <= 250) else (0, 0, 0, 255))
    check("masks: opaque alpha ignored, luminance used", abs(M.coverage(opaque) - area) < 0.005)

    canon, info = M.normalize(MASK, 512, 512)
    im = Image.open(io.BytesIO(canon))
    check("masks: resampled to the working canvas", im.size == (512, 512))
    check("masks: canonical form is RGBA", im.mode == "RGBA")
    px = im.convert("RGBA").load()
    # Where the painted box's CENTRE lands after resampling 640x480 -> 512x512 (note it is
    # not (256,256): x scales by 512/640, y by 512/480). Probing the mathematically mapped
    # point is what makes this a real registration test rather than a tautology.
    cx, cy = int(((200 + 300) / 2) * 512 / 640), int(((150 + 250) / 2) * 512 / 480)
    check("masks: painted region lands where the resize maps it", px[cx, cy][3] == 255)
    check("masks: corner stays unselected after resize", px[8, 8][3] == 0)
    check("masks: RGB constant white, alpha carries coverage",
          set(px[cx, cy][:3]) == {255} and set(px[8, 8][:3]) == {255})
    check("masks: coverage reported before and after",
          {"coverage_before", "coverage_paint", "coverage_after"} <= set(info))

    _, inv = M.normalize(MASK, 256, 256, invert=True)
    check("masks: invert flips the selection", inv["coverage_after"] > 0.9)

    _, plain = M.normalize(MASK, 512, 512)
    _, grown = M.normalize(MASK, 512, 512, expand=20)
    _, shrunk = M.normalize(MASK, 512, 512, shrink=20)
    check("masks: expand grows coverage", grown["coverage_after"] > plain["coverage_after"])
    check("masks: shrink reduces coverage", shrunk["coverage_after"] < plain["coverage_after"])

    def hist(**kw):
        return Image.open(io.BytesIO(M.normalize(MASK, 512, 512, **kw)[0])).getchannel("A").histogram()
    # feather is applied LAST (after the hard edge), deliberately: this one canonical mask
    # feeds both the paint extent (which VAEEncodeForInpaint binarizes itself — Bug 1 in
    # imagegen/workflows.py) and the soft-edge composite (which must be a ramp). So the
    # contract is: no feather => strictly binary; feather => ramp present.
    check("masks: feather=0 yields a strictly binary mask", sum(hist(feather=0)[1:255]) == 0)
    check("masks: feather>0 yields the ramp the composite needs", sum(hist(feather=8)[1:255]) > 0)

    _, capped = M.normalize(MASK, 256, 256, expand=10_000)
    check("masks: absurd expand is clamped and flagged", capped["capped"] is True)

    # the regression that motivated the two-tier floor: a blemish-sized mask is VALID
    spot = rect_mask((1024, 768), (420, 320, 468, 356))
    _, spot_info = M.normalize(spot, 1024, 768, feather=6)
    check("masks: blemish-sized mask clears the empty floor",
          spot_info["coverage_after"] >= M.USER_MASK_EMPTY_FLOOR)
    check("masks: ...and its PAINT area sits below the 'small' note line",
          spot_info["coverage_paint"] < M.COVERAGE_FLOOR)
    # The feather number is deliberately larger; asserting the relationship documents it,
    # so nobody "fixes" the tiny-check to use coverage_after and break spot-healing.
    check("masks: feather inflates coverage_after (why the check uses coverage_paint)",
          spot_info["coverage_after"] >= spot_info["coverage_paint"])
    _, none = M.normalize(blank((640, 480)), 640, 480)
    check("masks: truly empty mask is below the empty floor",
          none["coverage_after"] < M.USER_MASK_EMPTY_FLOOR)

    ov = M.overlay(PHOTO, MASK)
    check("masks: overlay is a decodable PNG at source size",
          Image.open(io.BytesIO(ov)).size == (640, 480))
    check("masks: overlay actually tints", ov != M.overlay(PHOTO, blank((640, 480))))
    check("masks: image_size reads the photo", M.image_size(PHOTO) == (640, 480))
    for data, label in ((b"not a png", "garbage"), (rect_mask((4, 4), (0, 0, 3, 3)), "4x4")):
        try:
            M.normalize(data, 512, 512); check(f"masks: {label} refused", False)
        except M.MaskError:
            check(f"masks: {label} refused", True)
    check("masks: blank_mask is a valid empty canonical mask",
          M.coverage(M.blank_mask(64, 64)) == 0.0)



def fresh_token():
    return T.mint(SECRET, scope="launch", email=EMAIL)


# ---------------------------------------------------------------- 3. routes
def test_routes():
    tb = make_tb()
    # health is deliberately tokenless so the harness can tell "down" from "blocked"
    r = get(tb, "/toolbox/health")
    check("health answers without a token", r.status == 200 and r.payload["ok"])
    check("health reports the debt flag M1 must flip", "redeem_on_create" in r.payload)

    # every state-touching route refuses no/bad/forged tokens
    for label, hdrs in (("no token", {}),
                        ("forged token", {"authorization": "Bearer " + TOK[:10] + "x" * 8}),
                        ("wrong-secret token", {"authorization": "Bearer " + T.mint("nope", scope="launch", email=EMAIL)})):
        f = Fake("GET", "/toolbox/embed", hdrs)
        tb.dispatch(f)
        check(f"embed refuses {label}", f.status == 403)
    r = post(tb, "/toolbox/mask/preview", {"mask_png": MASK_B64}, token=None)
    check("preview refuses no token", r.status == 403)

    # the embed document must be self-contained: an opaque-origin srcdoc frame cannot
    # resolve relative asset URLs
    r = get(tb, "/toolbox/embed?image_id=x", token=fresh_token())
    doc = r.raw.decode()
    check("embed serves html", r.status == 200 and "text/html" in (r.ctype or ""))
    check("embed inlines css+js, references nothing relatively",
          "<style>" in doc and "window.__TB__" in doc and 'src="' not in doc and 'href="' not in doc)
    check("embed carries the photo as a data URI (the frame never fetches it)",
          PHOTO_B64 in doc)
    check("embed leaves no unsubstituted tokens", "__TB_SCRIPT__" not in doc)

    # the transport probe: bytes survive the trip, identity comes from the token
    r = post(tb, "/toolbox/echo", {"mask_png": MASK_B64}, token=fresh_token())
    check("echo round-trips the mask", r.status == 200 and r.payload["size"] == [640, 480]
          and r.payload["bytes"] == len(MASK))
    check("echo identifies the caller from the token, not the body", r.payload["email"] == EMAIL)

    # preview: small-but-real is usable, and the overlay comes back
    r = post(tb, "/toolbox/mask/preview",
             {"mask_png": base64.b64encode(spot_mask()).decode(), "spec": {"mask_feather": 6}},
             token=fresh_token())
    check("preview accepts a blemish-sized mask (no false 'nothing painted')",
          r.status == 200 and r.payload["empty"] is False and r.payload.get("tiny") is True)
    check("preview returns a decodable server-side overlay",
          "overlay_png" in r.payload and
          Image.open(io.BytesIO(base64.b64decode(r.payload["overlay_png"]))).size == (640, 480))

    # The overlay "opacity" slider reaches the SERVER bake: a stronger alpha must produce a
    # visibly different overlay, and garbage must fall back to the default rather than 500.
    def _overlay(spec):
        rr = post(tb, "/toolbox/mask/preview",
                  {"mask_png": base64.b64encode(spot_mask()).decode(), "spec": spec},
                  token=fresh_token())
        return rr, (rr.payload or {}).get("overlay_png")
    r0, ov0 = _overlay({"mask_feather": 6, "overlay_alpha": 0.0})
    r1, ov1 = _overlay({"mask_feather": 6, "overlay_alpha": 1.0})
    check("preview: overlay_alpha is honoured (alpha 0 and alpha 1 bake different overlays)",
          r0.status == 200 and r1.status == 200 and ov0 and ov1 and ov0 != ov1)
    rb, ovb = _overlay({"mask_feather": 6, "overlay_alpha": "not-a-number"})
    check("preview: a junk overlay_alpha falls back to the default (no 500, still an overlay)",
          rb.status == 200 and bool(ovb) and
          Image.open(io.BytesIO(base64.b64decode(ovb))).size == (640, 480))

    r = post(tb, "/toolbox/mask/preview",
             {"mask_png": base64.b64encode(blank((640, 480))).decode()}, token=fresh_token())
    check("preview flags an empty mask loudly", r.payload["empty"] is True and "error" in r.payload)

    # Sub-ceiling on purpose: this pins ROUND-TO-NEAREST grid rounding of the working
    # canvas. The over-ceiling case (1030x770 and anything bigger is pulled down to
    # masks.RENDER_MAX_SIDE) is pinned in test_working_size, where it belongs.
    r = post(tb, "/toolbox/mask/preview",
             {"mask_png": MASK_B64, "spec": {"width": 1010, "height": 770}}, token=fresh_token())
    check("preview resamples to 16-aligned working dims",
          r.payload["info"]["size"] == [round(1010 / 16) * 16, round(770 / 16) * 16],
          str(r.payload["info"]["size"]))

    r = post(tb, "/toolbox/mask/preview", {"mask_png": MASK_B64, "spec": {"invert": True}},
             token=fresh_token())
    check("invert is a spec PARAMETER (graph has the node), not baked pixels",
          r.payload["info"]["inverted"] is True and r.payload["coverage"] > 0.9)


    # job create: the M0 contract the editor is already written against
    r = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {"kind": "retouch", "seed": 42}},
             token=fresh_token())
    check("job create accepts a painted mask", r.status == 200 and r.payload["state"] == "queued")
    check("job create echoes seed + working size",
          r.payload["seed"] == 42 and r.payload["working_size"] == [640, 480])
    check("job create issues a job-scope token for polling", bool(r.payload.get("token")))
    job_token = r.payload["token"]
    r2 = post(tb, "/toolbox/jobs/poll", {"job_id": r.payload["job_id"]}, token=job_token)
    check("job poll answers using the job token", r2.status == 200 and r2.payload["job_id"])
    r2 = post(tb, "/toolbox/jobs/poll", {"job_id": r.payload["job_id"]}, token=None)
    check("job poll still requires a token", r2.status == 403)

    r = post(tb, "/toolbox/jobs", {"mask_png": base64.b64encode(blank((640, 480))).decode()},
             token=fresh_token())
    check("job create refuses an empty mask rather than rendering blind",
          r.status == 400 and "nothing" in r.payload["error"])
    r = post(tb, "/toolbox/jobs", {"mask_png": "!!!not base64!!!"}, token=fresh_token())
    check("a garbage mask is a 400, not a 500", r.status == 400)
    # poll auth is scope- AND job-bound, which is stronger than "requires a job_id"
    r = post(tb, "/toolbox/jobs/poll", {"job_id": "x"}, token=fresh_token())
    check("poll refuses a launch token (wrong scope)", r.status == 403)
    other = T.mint(SECRET, scope="job", email=EMAIL, job_id="someone-elses-job")
    r = post(tb, "/toolbox/jobs/poll", {"job_id": "whatever"}, token=other)
    check("a job token cannot read a DIFFERENT job", r.status == 403)
    unbound = T.mint(SECRET, scope="job", email=EMAIL)      # no job_id binding
    r = post(tb, "/toolbox/jobs/poll", {}, token=unbound)
    check("poll still requires a job_id", r.status == 400)
    # auto-mask degrades honestly in every failure mode
    r = post(make_tb(segmenter=None), "/toolbox/mask/auto", {"text": "the cat"}, token=fresh_token())
    check("auto-mask 503s when no segmenter is wired",
          r.status == 503 and r.payload["reason"] == "no_segmenter")
    r = post(make_tb(segmenter=lambda b, t, th: (MASK, {}), image_engine_up=lambda: False),
             "/toolbox/mask/auto", {"text": "the cat"}, token=fresh_token())
    check("auto-mask 503s when the engine is down",
          r.status == 503 and r.payload["reason"] == "no_image_engine")
    r = post(make_tb(segmenter=lambda b, t, th: (MASK, {})), "/toolbox/mask/auto",
             {"text": "the cat"}, token=fresh_token())
    check("auto-mask returns an editable mask", r.status == 200 and "mask_png" in r.payload)
    r = post(make_tb(segmenter=lambda b, t, th: (None, {"error": "nothing matched"})),
             "/toolbox/mask/auto", {"text": "the cat"}, token=fresh_token())
    check("auto-mask FAILS LOUDLY when segmentation finds nothing",
          r.status == 502 and "nothing matched" in r.payload["error"])
    r = post(make_tb(segmenter=lambda b, t, th: (MASK, {})), "/toolbox/mask/auto",
             {"text": "   "}, token=fresh_token())
    check("auto-mask refuses an empty description", r.status == 400)

    # click-to-select (SAM3) degrades honestly, mirroring the text auto-mask contract.
    CLICK = {"points": [[0.5, 0.5]]}
    r = post(make_tb(click_segmenter=None), "/toolbox/mask/click", CLICK, token=fresh_token())
    check("click-select 503s when no click segmenter is wired",
          r.status == 503 and r.payload["reason"] == "no_click_segmenter")
    r = post(make_tb(click_segmenter=lambda s, p, n, **k: (MASK, {}),
                      image_engine_up=lambda: False),
             "/toolbox/mask/click", CLICK, token=fresh_token())
    check("click-select 503s when the engine is down",
          r.status == 503 and r.payload["reason"] == "no_image_engine")
    r = post(make_tb(click_segmenter=lambda s, p, n, **k: (MASK, {})),
             "/toolbox/mask/click", CLICK, token=fresh_token())
    check("click-select returns an editable mask", r.status == 200 and "mask_png" in r.payload)
    r = post(make_tb(click_segmenter=lambda s, p, n, **k: (None, {"error": "engine blew up"})),
             "/toolbox/mask/click", CLICK, token=fresh_token())
    check("click-select FAILS LOUDLY when the engine errors",
          r.status == 502 and "engine blew up" in r.payload["error"])
    r = post(make_tb(click_segmenter=lambda s, p, n, **k: (MASK, {})),
             "/toolbox/mask/click", {"points": []}, token=fresh_token())
    check("click-select refuses an empty point list", r.status == 400)
    r = post(make_tb(click_segmenter=lambda s, p, n, **k: (MASK, {})),
             "/toolbox/mask/click", {"points": [[0.5, 0.5]] * 40}, token=fresh_token())
    check("click-select caps runaway point lists", r.status == 400)
    # the seam receives the points UNMODIFIED (browser-sent normalised coords), so a
    # mis-scaled coordinate can be attributed to the server, not silently absorbed here.
    seen = {}
    def cap_seg(s, p, n, **k):
        seen["pos"] = p; seen["neg"] = n
        return MASK, {}
    post(make_tb(click_segmenter=cap_seg), "/toolbox/mask/click",
         {"points": [[0.25, 0.75]], "negative_points": [[0.1, 0.1]]}, token=fresh_token())
    check("click-select forwards positive+negative points to the seam",
          seen.get("pos") == [[0.25, 0.75]] and seen.get("neg") == [[0.1, 0.1]])

    # CORS, because the in-chat mount is an opaque origin
    r = post(tb, "/toolbox/echo", {"mask_png": MASK_B64}, token=fresh_token())
    check("CORS allow-origin set on POST", r.sent_headers.get("access-control-allow-origin") == "*")
    f = Fake("OPTIONS", "/toolbox/echo", {})
    tb.dispatch(f)
    check("OPTIONS is answered", f.status == 200)

    # source resolution is scoped to the TOKEN'S user
    seen = {}
    def spy(email, ref):
        seen["email"] = email
        seen["ref"] = ref
        return PHOTO
    post(make_tb(source=spy), "/toolbox/mask/preview", {"mask_png": MASK_B64, "image_id": "x"},
         token=fresh_token())
    check("the source lookup uses the token's email, not a caller-supplied identity",
          seen.get("email") == EMAIL)
    r = post(make_tb(source=lambda e, ref: None), "/toolbox/jobs", {"mask_png": MASK_B64},
             token=fresh_token())
    check("a source that is not the caller's is refused", r.status == 400)

    # routing hygiene
    check("unknown toolbox route is a 404", get(tb, "/toolbox/nope").status == 404)
    check("GET on a POST-only route is a 405", get(tb, "/toolbox/echo").status == 405)
    f = Fake("POST", "/toolbox/echo", {"content-type": "text/plain",
                                       "authorization": "Bearer " + fresh_token()}, b"{not json")
    tb.dispatch(f)
    check("a malformed body is a 400, not a 500", f.status == 400)
    check("dispatch ignores non-toolbox paths", tb.dispatch(Fake("GET", "/comfyui/history")) is False)

    def boom(email, ref):
        raise RuntimeError("kaboom")
    r = post(make_tb(source=boom), "/toolbox/jobs", {"mask_png": MASK_B64}, token=fresh_token())
    check("an internal crash becomes a JSON 500, never a traceback into the shared front",
          r.status == 500 and "kaboom" in r.payload["error"])



# ---------------------------------------------------------------- 4. web assets
def test_web():
    for name in ("toolbox.js", "toolbox.css", "harness.html", "probe.js"):
        try:
            raw, ctype = W.asset(name)
            check(f"asset {name} loads", len(raw) > 200)
        except Exception as e:  # noqa: BLE001
            check(f"asset {name} loads ({e})", False)
    for bad in ("../secrets", "/etc/passwd", ".hidden"):
        try:
            W.asset(bad)
            check(f"asset traversal {bad!r} refused", False)
        except W.UnknownAsset:
            check(f"asset traversal {bad!r} refused", True)
    # instrumentation must be opt-in, so feasibility scaffolding cannot leak into the
    # product mount
    real = W.embed_document({"api": "http://x", "token": "t"}, probe=False)
    spike = W.embed_document({"api": "http://x", "token": "t"}, probe=True)
    check("probe absent from a real mount", "tb-probe" not in real)
    check("probe present in the spike only", "tb-probe" in spike)
    nasty = W.embed_document({"image": "</script><script>alert(1)</script>"})
    check("injected JSON cannot break out of its <script>", "</script><script>alert" not in nasty)
    h = W.harness_document("/toolbox/embed?t=1", api="http://x", email=EMAIL, token_state="minted")
    check("harness substitutes its tokens", "__TB_API__" not in h and "__TB_EMAIL__" not in h)
    check("harness mounts the sandboxed panel with the real sandbox attribute",
          'sandbox="allow-scripts"' in h)


def test_contract():
    """The JS<->server seam. Each side can pass its own tests while disagreeing about a
    field name, and that is the classic way an integration fails silently; this seam
    crosses a process, a language and (in the embed mount) an origin boundary, so assert
    it here rather than discovering it on a phone."""
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / "stackd" / "toolbox" / "web" / "toolbox.js").read_text()
    api = (root / "stackd" / "toolbox" / "api.py").read_text()
    m = re.search(r"function params\(\) \{.*?\n  \}", js, re.S)
    check("editor exposes a params() block to check", m is not None)
    # NB: a key can follow a comma on the same line (`width: ..., height: ...`), so a
    # line-anchored regex silently misses it — which is exactly the kind of false failure
    # that would send someone off to "fix" working code.
    sent = set(re.findall(r"(?:^|[,{])\s*(\w+)\s*:", m.group(0), re.M)) if m else set()
    check("editor sends the mask geometry the server reads",
          {"mask_expand", "mask_shrink", "mask_feather", "invert", "width", "height"} <= sent)
    called = set(re.findall(r"req\('(/toolbox/[^']+)'", js))
    served = set(re.findall(r'"(/toolbox/[a-z/]+)"', api))
    check("every endpoint the editor calls exists on the server", bool(called) and not (called - served))
    # --- knob honesty --------------------------------------------------------
    # A control that LOOKS configurable while the render ignores it is worse than
    # no control: the user moves it, sees no change, and concludes the editor is
    # broken. So every value params() sends must be either CONSUMED by the server
    # or explicitly dead-marked (and disabled) in the client.
    #
    # Liveness is MEASURED, not asserted: a spec value is consumed iff the server
    # reads it -- engine.py (what reaches the ComfyUI graph, i.e. _patch_graph)
    # or api.py (what reaches the mask normalizer / job row). Matching the read
    # form (`.get("k"` / `["k"]`) instead of the bare name is deliberate: these
    # names all appear in comments and docstrings, and a substring match would
    # call everything live. Verified against the live job row, whose spec_json
    # still records strength=0.05 that nothing ever read -- the label promised an
    # edit strength the pipeline did not have.
    eng = (root / "stackd" / "toolbox" / "engine.py").read_text()
    srv = eng + "\n" + api

    def server_reads(knob):
        return re.search(r'\.get\(\s*["\']%s["\']|\[\s*["\']%s["\']' % (knob, knob), srv) is not None

    dead_m = re.search(r"var DEAD_KNOBS = \[([^\]]*)\]", js)
    dead = set(re.findall(r"'([^']+)'", dead_m.group(1))) if dead_m else set()
    check("the client keeps a dead-knob registry", dead_m is not None)
    # THE FRESHNESS KEY'S OBJECT TERM: previewSig is what makes a late preview answer
    # stale — compose() refuses to paint an overlay whose sig no longer matches, and it
    # is the objSig() term that invalidates an answer minted BEFORE an object moved.
    # The browser suite kills the dropped term with the DR __PVHOLD race (stale answer
    # released mid-drag); this is the source-level backstop so the term cannot silently
    # vanish again while some future fixture loses the race's timing teeth.
    mps = re.search(r"function previewSig\(\)[\s\S]{0,320}?\.join\(", js)
    check("the preview freshness key covers the SELECTED OBJECT, not just the knobs",
          mps is not None and "objSig()" in mps.group(0),
          mps and '...' + mps.group(0)[-70:])
    check("dead-marked controls are disabled in the built panel",
          re.search(r"node\.disabled\s*=\s*true", js) is not None)
    for knob in ("kind", "prompt", "strength", "opacity", "blend_mode",
                 "color_match", "preserve_detail", "variants", "seed"):
        check(f"editor sends {knob}", knob in sent)
        live = server_reads(knob)
        check(f"{knob}: server-consumed or dead-marked (no silent no-op control)",
              live or knob in dead,
              "wire it in engine._patch_graph / an api spec read, or add it to "
              "DEAD_KNOBS and disable it in the panel")
        # The converse guard: a control the server DOES read must never be greyed
        # out, or the panel quietly takes a working feature away while claiming it
        # was never there. (This is the check that would have caught me disabling
        # the mask edge if I had mis-declared it dead.)
        check(f"{knob}: not disabled while live",
              (not live) or (knob not in dead),
              f"{knob} is read by the server -- remove it from DEAD_KNOBS")
    check("variants is dead-marked (the queue stores exactly one artifact)",
          "variants" in dead,
          "jobs.py keeps one artifact_b64 per row and pollJob renders what the "
          "server sends -- either render variants or keep it disabled")
    check("strength is dead-marked (never wired; it is NOT denoise)",
          "strength" in dead,
          "a live slider labelled edit-strength that nothing reads makes every "
          "erase look like a no-op -- keep it disabled until it is wired")
    # --- the mode dropdown is LIVE: engine reads kind and rewires the graph -------
    # The mode is a real control now (engine._apply_mode): 'edit' keeps the scene-referenced
    # KSampler conditioning, 'replace' drops it so the prompt can do a genuine identity-level
    # swap of the masked region. Pin both halves: that engine actually reads kind (the knob
    # is not theatre -- the converse of the old "engine ignores kind" premise), and that the
    # labels describe the two real paths rather than promising effects that are not there.
    # The functional proof that 'replace' rewires the graph lives in test_graphs.
    check("engine reads spec kind to select the conditioning mode",
          re.search(r'get\(\s*"kind"|spec\[\s*"kind"', eng) is not None,
          "kind is a live mode control -- engine._apply_mode must read it and rewire the graph")
    check("the mode disclosure describes what Edit vs Replace do",
          "reimagine" in js and "Replace" in js and "same repaint graph" not in js)
    # Scoped to the MODE LABELS rather than the whole file: 'GPU' legitimately appears
    # elsewhere (the mask preview really is CPU-side), and an invariant that fires on true
    # statements gets deleted rather than fixed.
    km = re.search(r"select\('kind',\s*\[(.*?)\]\)\)", js, re.S)
    check("the mode dropdown is present to be checked", km is not None)
    modes = km.group(1) if km else ""
    check("no mode LABEL claims a CPU / accelerated path that does not exist",
          'GPU' not in modes and 'AI' not in modes, modes[:70].replace("\n", " "))
    check("the two real modes are the only ones offered (no theatre left behind)",
          "'edit'" in modes and "'replace'" in modes
          and "retouch" not in modes and "darkroom" not in modes
          and "localize_stylize" not in modes and "inpaint" not in modes,
          modes[:90].replace("\n", " "))
    # --- token-threading: the seam that burned a live render. The server requires poll +
    # cancel to authenticate with the JOB-scope token create minted (the launch token is
    # spent by the create, and polling must not be able to redeem anything). If the editor
    # reuses its global launch TOKEN for those calls, the server 403s with "token is for
    # 'launch', not 'job'" — a real bug the server-side contract tests could not see. ---
    check("req() takes an explicit token argument (so reads and job calls can differ)",
          re.search(r"function req\(\s*path,\s*body,\s*tok\s*\)", js) is not None)
    for route in ("/toolbox/jobs/poll", "/toolbox/jobs/cancel"):
        m3 = re.search(re.escape("req('" + route) + r"'\s*,\s*\{[^}]*\}\s*,\s*([A-Za-z_]\w*)\s*\)", js)
        check(f"{route} sends an explicit job token, not the global launch token",
              m3 is not None and m3.group(1) != "TOKEN",
              "poll/cancel must pass create's job token as req()'s 3rd arg")
    check("create hands its returned job token to the poll loop",
          re.search(r"jobToken\s*=\s*r\.token", js) is not None
          and re.search(r"pollJob\(\s*r\.job_id,\s*0,\s*jobToken\s*\)", js) is not None)
    check("the poll retry loop threads the job token forward",
          re.search(r"pollJob\(\s*id,\s*tries\s*\+\s*1,\s*tok\s*\)", js) is not None)
    tb = make_tb()
    spec = {k: 1 for k in ("kind", "prompt", "strength", "opacity", "blend_mode",
                           "color_match", "preserve_detail", "variants")}
    r = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": spec}, token=fresh_token())
    check("job create echoes the whole spec (nothing silently dropped)",
          r.status == 200 and r.payload.get("spec") == spec)
    r = post(tb, "/toolbox/mask/preview", {"mask_png": MASK_B64, "spec": {}}, token=fresh_token())
    check("preview returns the fields the status line reads",
          {"coverage", "empty", "tiny", "info", "overlay_png"} <= set(r.payload))


def test_mint_seam():
    """THE INTERNAL MINT SEAM — the Open WebUI mounts (in-chat Tool, retouch link).
    Pinned: caller-auth deny-first (no bearer / wrong bearer / unconfigured seam all
    403 BEFORE any body handling), the registry gate, chat auto-detect through the
    injected resolver (the same lookup edit_image uses), the loud no-public-base 503
    (a wrong-but-200 mint would hand out an editor that cannot reach its own API), and
    — the part that makes the seam worth having — that the minted token is the SAME
    single-use artifact /toolbox/launch mints: reads ride it, first submit redeems it,
    the replay is refused."""
    # Ref-SENSITIVE on purpose: the suite's default fake hands back PHOTO for ANY ref,
    # including "", which is how 580 checks stayed green while the mint path resolved an
    # empty ref and mounted an editor with no photo at all. A fake that mirrors the real
    # resolver (engine.make_source returns None for a blank ref) is what makes "the
    # pixels actually arrived" assertable rather than inferred from a echoed label.
    tb = make_tb(mint_key="minty", public_base="https://lab.example",
                 source=lambda email, ref: PHOTO if (email and ref) else None)

    def mint(body, key="minty"):
        return post(tb, "/toolbox/mint", body,
                    headers=({"authorization": "Bearer " + key} if key else {}))

    r = mint({"email": EMAIL, "source_ref": "/api/v1/files/x/content"}, key=None)
    check("mint: no bearer is refused outright", r.status == 403
          and r.payload.get("reason") == "mint_requires_caller_auth", r.payload)
    r = mint({"email": EMAIL, "source_ref": "/api/v1/files/x/content"}, key="wrong")
    check("mint: wrong caller-auth bearer is refused", r.status == 403
          and r.payload.get("reason") == "mint_requires_caller_auth", r.payload)
    tb_off = make_tb(public_base="https://lab.example")   # seam never configured
    r = post(tb_off, "/toolbox/mint", {"email": EMAIL, "source_ref": "files/x"},
             headers={"authorization": "Bearer minty"})
    check("mint: an unconfigured seam is dead even with a guessed key (deny-by-default)",
          r.status == 403 and r.payload.get("reason") == "mint_not_configured", r.payload)
    r = mint({"source_ref": "/api/v1/files/x/content"})
    check("mint: no email is a 400, never an unattributable token",
          r.status == 400 and "email" in (r.payload.get("error") or ""), r.payload)
    tb_reg = make_tb(mint_key="minty", public_base="https://lab.example",
                     user_registered=lambda e: e == EMAIL)
    r = post(tb_reg, "/toolbox/mint", {"email": "ghost@x.io", "source_ref": "files/g"},
             headers={"authorization": "Bearer minty"})
    check("mint: an unregistered email is refused with the register-page pointer",
          r.status == 403 and r.payload.get("reason") == "user_not_registered", r.payload)
    r = mint({"email": EMAIL})
    check("mint: neither source_ref nor chat_id is an honest 400",
          r.status == 400 and "source_ref" in (r.payload.get("error") or ""), r.payload)
    tb_nobase = make_tb(mint_key="minty")
    r = post(tb_nobase, "/toolbox/mint", {"email": EMAIL, "source_ref": "files/x"},
             headers={"authorization": "Bearer minty"})
    check("mint: without STACKD_TOOLBOX_PUBLIC_URL the seam 503s its reason",
          r.status == 503 and r.payload.get("reason") == "no_public_base", r.payload)

    # the happy path through an explicit source_ref
    r = mint({"email": EMAIL, "source_ref": "/api/v1/files/photo/content"})
    p = r.payload or {}
    check("mint: source_ref path returns a SHORT link, the token and a full editor document",
          r.status == 200 and p.get("ok")
          and p.get("url", "").startswith("https://lab.example/toolbox/e/")
          and len(p.get("code") or "") == 10
          # The credential must not be in the URL at all: this link is text a model
          # publishes, and a re-typed 250-char base64 body is what caused the live 403
          # (payload `exp` -> `ex`, signature carried over untouched, verified by
          # re-signing the repaired payload and matching the pasted signature).
          and "token=" not in p.get("url", "") and "." not in p.get("url", "").split("/e/")[1]
          and p.get("token") and "createElement('canvas')" in (p.get("html") or "")
          # THE check that matters, and the one the old suite lacked: the photo's OWN
          # bytes in the document, not merely the ref echoing back through the cfg label
          # (and not the data-URI prefix, which shipped JS always contains — see
          # PHOTO_B64).
          and PHOTO_B64 in (p.get("html") or "")
          and "/api/v1/files/photo/content" in (p.get("html") or "")
          and p.get("expires_in") == 900,
          {k: str(v)[:80] for k, v in p.items()})
    tok = p.get("token") or ""
    code = p.get("code") or ""

    # the short link IS the mount: same document, credential resolved server-side
    doc = get(tb, "/toolbox/e/" + code)
    body = (doc.raw or b"").decode("utf-8", "replace")
    check("editor link: the short code serves the editor document with the token inlined",
          doc.status == 200 and "createElement('canvas')" in body and tok in body
          # The photo's own bytes must be IN the document the code route serves. This is
          # the check that pins the live "No source image was handed to the editor" bug:
          # the code route handed _embed_cfg a {"source_ref": ...} dict and the resolver
          # only read image_id from a query, so it mounted empty.
          and PHOTO_B64 in body
          and (doc.ctype or "").startswith("text/html"),
          {"status": doc.status, "ctype": doc.ctype, "len": len(body),
           "has_photo": PHOTO_B64 in body})
    again = get(tb, "/toolbox/e/" + code)
    check("editor link: re-fetching is allowed (a pre-Render refresh must not brick it)",
          again.status == 200, again.status)
    check("editor link: the code avoids the visually ambiguous characters",
          not set(code) & set("0o1il"), code)
    bogus = get(tb, "/toolbox/e/" + ("z" * 10))
    check("editor link: an unknown code is a 404 that tells the holder to ask again",
          bogus.status == 404
          and bogus.payload.get("reason") == "unknown_editor_link"
          and "fresh" in (bogus.payload.get("error") or "").lower(), bogus.payload)
    mangled = get(tb, "/toolbox/e/" + (code[:-1] + ("a" if code[-1] != "a" else "b")))
    check("editor link: a mistyped code is indistinguishable from an unknown one",
          mangled.status == 404 and mangled.payload.get("reason") == "unknown_editor_link",
          mangled.payload)
    post_like = post(tb, "/toolbox/e/" + code, {})
    check("editor link: the code route is GET-only (405, not a silent 404)",
          post_like.status == 405, post_like.status)
    # the token-in-URL mount stays live: the lab harness and the browser suite drive it
    legacy = get(tb, "/toolbox/embed?token=" + urllib.parse.quote(tok, safe=""))
    check("editor link: the legacy /toolbox/embed?token= mount still serves (compat)",
          legacy.status == 200 and "createElement('canvas')" in (legacy.raw or b"").decode(),
          legacy.status)

    # THE failure the operator actually hit, pinned as a message. Rebuild the token the
    # way a bad transcription produces one: same claims, one key mangled (exp -> ex),
    # signature carried over untouched from the real mint.
    _v, _b, _s = tok.split(".")
    _claims = json.loads(T._unb64(_b))
    _claims["ex"] = _claims.pop("exp")
    _tampered_body = T._b64(json.dumps(_claims, separators=(",", ":"), sort_keys=True).encode())
    _tampered = _v + "." + _tampered_body + "." + _s
    r_alt = get(tb, "/toolbox/embed?token=" + urllib.parse.quote(_tampered, safe=""))
    check("a damaged token says the link was altered, not merely 'bad signature'",
          r_alt.status == 403
          and "signature does not match" in (r_alt.payload.get("error") or "")
          and "fresh" in (r_alt.payload.get("error") or ""), r_alt.payload)
    # ...and the generic message survives for a body that never looked like a claim set
    # we would sign, so the two rejections stay distinguishable.
    _strange = _v + "." + T._b64(json.dumps({"hello": "world"}).encode()) + "." + _s
    r_bad = get(tb, "/toolbox/embed?token=" + urllib.parse.quote(_strange, safe=""))
    check("a body that never looked minted still reads plainly as 'bad signature'",
          r_bad.status == 403 and "bad signature" in (r_bad.payload.get("error") or ""),
          r_bad.payload)
    r_junk = get(tb, "/toolbox/embed?token=" + urllib.parse.quote("v1.just-one-part", safe=""))
    check("a nonsense token still reads as malformed",
          r_junk.status == 403 and "malformed" in (r_junk.payload.get("error") or ""),
          r_junk.payload)
    _jti = json.loads(T._unb64(_b)).get("jti")
    check("the log hint surfaces a jti for correlation, and never a signature",
          tb._jti_hint(Fake("GET", "/toolbox/embed", {}, b""), {"token": tok}) == _jti
          and tb._jti_hint(Fake("GET", "/toolbox/embed", {}, b""), {"token": "junk"}) == "n/a")

    def _raises(exc, fn, *a):
        try:
            fn(*a)
            return False
        except exc:
            return True
    check("mint_link refuses to stand in for garbage or an expired token",
          _raises(T.BadToken, T.mint_link, "not-a-token")
          and _raises(T.Expired, T.mint_link,
                      T.mint(SECRET, scope="launch", email=EMAIL, ttl_s=-5))
          and T.resolve_link("never-minted") is None)
    # Two gates that exist ONLY to kill specific mutants in h_editor_link, which is new
    # attack surface: a handler that resolved the code and served the document without
    # re-verifying the token would still pass every check above (the token we registered
    # is a good one). So pin the two ways a resolved-but-unverified token is dangerous.
    _job_tok = T.mint(SECRET, scope="job", email=EMAIL, job_id="j1")
    _job_code = T.mint_link(_job_tok)
    r_job = get(tb, "/toolbox/e/" + _job_code)
    check("editor link: a resolved code never outranks the token's own scope",
          r_job.status == 403 and not r_job.raw
          and "not 'launch'" in (r_job.payload.get("error") or ""),
          r_job.payload)
    _live_tok = T.mint(SECRET, scope="launch", email=EMAIL, ttl_s=60)
    _live_code = T.mint_link(_live_tok)
    import time as _time_mod, types as _types
    _real_time, T.time = T.time, _types.SimpleNamespace(
        time=lambda: _time_mod.time() + 3600)
    try:
        r_gone = get(tb, "/toolbox/e/" + _live_code)
    finally:
        T.time = _real_time
    check("editor link: the code expires with its token, it does not outlive it",
          r_gone.status == 404
          and r_gone.payload.get("reason") == "unknown_editor_link", r_gone.payload)
    # A photo that FAILS to fetch mid-mint (deleted file, OWU hiccup — _ingest_source
    # raises ValueError, unlike the fake resolver's None) must still produce a loadable
    # editor document. Live-found: the fallback branch left `before` unbound and the
    # whole mount 500'd with UnboundLocalError.
    def fetch_refuses(email, ref):
        raise ValueError("OWU fetch failed")
    tbf = make_tb(mint_key="minty", public_base="https://lab.example", source=fetch_refuses)
    r = post(tbf, "/toolbox/mint", {"email": EMAIL, "source_ref": "/api/v1/files/gone/content"},
             headers={"authorization": "Bearer minty"})
    pf = r.payload or {}
    check("mint: a photo whose fetch raises still yields a loadable editor document",
          r.status == 200 and pf.get("ok")
          and "createElement('canvas')" in (pf.get("html") or ""), r.payload)
    # The single-use launch contract bites ONLY when a queue is mounted — the stub
    # echo path deliberately does not redeem (there is no GPU spend to protect; see
    # h_job_create). Mount the established no-GPU fake so this really tests the
    # redemption, not just the echo.
    from stackd.toolbox import jobs as J
    tbw = make_tb(worker=J.JobQueue(J.JobStore(":memory:"),
                                    render=lambda job, s, m, **kw: (None, None)))
    soft = brush_ramp_mask((640, 480), (20, 20, 70, 70), 0.5)
    prev = post(tbw, "/toolbox/mask/preview",
                {"mask_png": base64.b64encode(soft).decode(), "spec": {}},
                headers={"authorization": "Bearer " + tok})
    check("minted token: a mask preview (a read) rides it without spending it",
          prev.status == 200 and prev.payload.get("overlay_png"), prev.payload)
    job_body = {"mask_png": base64.b64encode(soft).decode(),
                "spec": {"prompt": "a calm lake at dusk"}}
    c1 = post(tbw, "/toolbox/jobs", job_body, headers={"authorization": "Bearer " + tok})
    check("minted token: the first render submit redeems it",
          c1.status == 200 and c1.payload.get("state") == "queued", c1.payload)
    c2 = post(tbw, "/toolbox/jobs", job_body, headers={"authorization": "Bearer " + tok})
    check("minted token: a replayed submit is refused (single-use is the launch contract)",
          c2.status == 403, c2.payload)

    # chat auto-detect runs THROUGH the injected resolver (serve wires the OWU walk)
    seen = {}
    def resolver(email, chat_id, message_id):
        seen.update(email=email, chat_id=chat_id, message_id=message_id)
        return "/api/v1/files/from-chat/content"
    tb_chat = make_tb(mint_key="minty", public_base="https://lab.example",
                      chat_source=resolver,
                      source=lambda email, ref: PHOTO if (email and ref) else None)
    r = post(tb_chat, "/toolbox/mint",
             {"email": EMAIL, "chat_id": "CH1", "message_id": "M2"},
             headers={"authorization": "Bearer minty"})
    check("mint: chat_id path resolves the branch image through the ONE lookup seam",
          r.status == 200 and seen.get("chat_id") == "CH1"
          and seen.get("message_id") == "M2"
          # The ref is BOUND (token claim + inlined cfg), not printed in the URL: the
          # whole point of the short link is that nothing model-visible carries it.
          and (r.payload or {}).get("source_ref") == "/api/v1/files/from-chat/content"
          and "from-chat" in (r.payload or {}).get("html", "")
          # pixels, not the label — see the source_ref/query asymmetry in _source_ref
          and PHOTO_B64 in (r.payload or {}).get("html", "")
          and "from-chat" in (lambda _p: T._unb64(_p[1]).decode()
                              if len(_p) == 3 else "")(
                              (r.payload or {}).get("token", "").split(".")),
          (seen, {k: str(v)[:60] for k, v in (r.payload or {}).items() if k != "html"}))

    # ---- CHAT HAND-BACK: chat_id is a SERVER-ASSERTED claim -----------------------
    # The user's live defect #1: the finished render never reached the conversation.
    # The return channel is comfy_render's post_chat_message, driven by spec["chat_id"]
    # — which may ONLY come from the verified launch token's claim, never the request
    # body (the browser could type anything). These pins make a regression of that
    # precedence a red gate.
    rows = {}
    tbw2 = make_tb(mint_key="minty", public_base="https://lab.example",
                   chat_source=lambda e, c, m: "/api/v1/files/in-chat/content",
                   source=lambda email, ref: PHOTO if (email and ref) else None,
                   worker=J.JobQueue(J.JobStore(":memory:"),
                                     render=lambda job, s, m, **kw: (None, None)))
    rm = post(tbw2, "/toolbox/mint", {"email": EMAIL, "chat_id": "CH-HAND"},
              headers={"authorization": "Bearer minty"})
    tok_h = (rm.payload or {}).get("token") or ""
    claim = (lambda _p: T._unb64(_p[1]).decode() if len(_p) == 3 else "")(tok_h.split("."))
    check("mint: chat_id rides the launch token as a signed claim",
          rm.status == 200 and '"chat_id":"CH-HAND"' in claim, claim[:120])
    c = post(tbw2, "/toolbox/jobs",
             {"mask_png": base64.b64encode(soft).decode(),
              "source_ref": "/api/v1/files/in-chat/content",
              "spec": {"prompt": "x", "chat_id": "FORGED-BY-BROWSER"}},
             headers={"authorization": "Bearer " + tok_h})
    rows["job_id"] = (c.payload or {}).get("job_id")
    row = tbw2._worker.store.get(rows["job_id"]) if rows.get("job_id") else None
    persisted = json.loads((row or {}).get("spec_json") or "{}")
    check("job create: the persisted chat_id is the TOKEN's claim, not the body's "
          "(forged body value overwritten)",
          c.status == 200 and persisted.get("chat_id") == "CH-HAND", persisted)
    check("job create: the create echo shows the same truth the worker will see",
          (c.payload or {}).get("spec", {}).get("chat_id") == "CH-HAND")
    # A token minted WITHOUT the claim (source_ref mount) must carry no hand-back,
    # even if the browser smuggles one into the body — otherwise any XSS in the
    # editor could post the user's renders into an arbitrary chat id.
    rn = post(tbw2, "/toolbox/mint",
              {"email": EMAIL, "source_ref": "/api/v1/files/in-chat/content"},
              headers={"authorization": "Bearer minty"})
    tok_n = (rn.payload or {}).get("token") or ""
    c = post(tbw2, "/toolbox/jobs",
             {"mask_png": base64.b64encode(soft).decode(),
              "source_ref": "/api/v1/files/in-chat/content",
              "spec": {"prompt": "x", "chat_id": "SMUGGLED"}},
             headers={"authorization": "Bearer " + tok_n})
    row = tbw2._worker.store.get((c.payload or {}).get("job_id"))
    persisted = json.loads((row or {}).get("spec_json") or "{}")
    check("job create: a smuggled spec.chat_id with no token claim is DROPPED, not honoured",
          c.status == 200 and "chat_id" not in persisted, persisted)
    # /toolbox/source.png is the harness panel A, and it reads the SAME resolver with a
    # query dict — broken by the same one-key omission, so it is pinned here too.
    sp = get(tb, "/toolbox/source.png?source_ref="
             + urllib.parse.quote("/api/v1/files/panel/content", safe=""), token=tok)
    check("source.png: a query source_ref really fetches the photo (not a 0-byte body)",
          sp.status == 200 and (sp.raw or b"") == PHOTO
          and (sp.ctype or "").startswith("image/"),
          {"status": sp.status, "bytes": len(sp.raw or b""), "ctype": sp.ctype})
    tb_none = make_tb(mint_key="minty", public_base="https://lab.example")
    r = post(tb_none, "/toolbox/mint", {"email": EMAIL, "chat_id": "CH1"},
             headers={"authorization": "Bearer minty"})
    check("mint: resolution unmounted is a 400 with its reason, not a photo-less editor",
          r.status == 400 and r.payload.get("reason") == "chat_resolution_not_mounted",
          r.payload)
    tb_empty = make_tb(mint_key="minty", public_base="https://lab.example",
                       chat_source=lambda e, c, m: None)
    r = post(tb_empty, "/toolbox/mint", {"email": EMAIL, "chat_id": "CH1"},
             headers={"authorization": "Bearer minty"})
    check("mint: a chat with no image says so (attach/generate first), never mints blind",
          r.status == 400 and r.payload.get("reason") == "no_chat_image", r.payload)

    # health tells the operator WHICH half of the seam is missing
    r = get(make_tb(mint_key="k"), "/toolbox/health")
    check("health: mint_key on with no public base reads mint:false / mint_key:true",
          r.payload.get("mint") is False and r.payload.get("mint_key") is True, r.payload)
    r = get(tb, "/toolbox/health")
    check("health: a fully configured seam reads mint:true",
          r.payload.get("mint") is True, r.payload)

    # ---- the two MOUNTS, pinned at source (imagegen.tools drags mcp/anyio/uvicorn;
    # owu_tool.py drags fastapi/pydantic — both run only in their own containers) ----
    import pathlib as _p
    root = _p.Path(__file__).resolve().parents[1] / "stackd"
    it_src = (root / "imagegen" / "tools.py").read_text()
    sv_src = (root / "serve.py").read_text()
    check("the MCP retouch tool mints through the ONE mint seam, in-process",
          "def retouch_image(" in it_src and "tb.mint_editor_link(email, image_ref, chat_id=" in it_src
          and "def set_toolbox_ref(" in it_src
          and "set_toolbox_ref(toolbox)" in sv_src)
    check("retouch hands out the SHORT link, never a token in a URL (model-retypeable)",
          "toolbox/embed?token=" not in it_src
          and "urllib.parse.quote(token" not in it_src.split("def retouch_image(")[-1][:4000])
    check("retouch never grows its own token code (only toolbox.api may mint)",
          "_tokens.mint(" not in it_src.split("def retouch_image(")[-1][:4000])
    owu = (root / "toolbox" / "owu_tool.py").read_text()
    check("the OWU Tool speaks the verified 0.11.3 embed contract",
          "HTMLResponse" in owu
          and 'headers={"Content-Disposition": "inline"}' in owu
          and "/toolbox/mint" in owu
          and 'headers.get("x-openwebui-user-email")' in owu)
    check("the OWU Tool hands the MODEL a neutral context, not the editor HTML",
          "result_context" in owu or "mask editor is open" in owu)
    check("the OWU Tool carries the mint key from server-side sources only",
          "os.environ.get(\"OPENAI_API_KEY\"" in owu and "self.valves.mint_key" in owu
          and "self.valves.mint_key_file" in owu)
    check("no caller-auth ever comes from a browser-supplied value",
          "_mint_caller_auth(self)" in owu
          and "request" not in owu.split("def _mint_caller_auth")[1].split("def ")[0])

    # BEHAVIOURAL, not grep: the resolver alone is exec'd (the module drags
    # fastapi/pydantic, but _mint_caller_auth needs only os + a valves object), so the
    # precedence, the blank-is-not-a-hit rule and the fall-through-past-a-missing-file
    # are actually RUN. The live container's OPENAI_API_KEY= (present, EMPTY) is case 5.
    import ast as _ast
    import os as _os
    import tempfile as _tf
    _cls = next(n for n in _ast.parse(owu).body
                if isinstance(n, _ast.ClassDef) and n.name == "Tools")
    _fn = next(n for n in _cls.body
               if isinstance(n, _ast.FunctionDef) and n.name == "_mint_caller_auth")
    _ns = {"os": _os}
    exec(compile(_ast.Module(body=[_fn], type_ignores=[]), "<resolver>", "exec"), _ns)
    resolve = _ns["_mint_caller_auth"]

    class _V:
        mint_key = ""
        mint_key_file = ""

    class _Self:
        def __init__(self, valves):
            self.valves = valves

    def resolve_with(mint_key="", mint_key_file="", **env):
        v = _V()
        v.mint_key, v.mint_key_file = mint_key, mint_key_file
        saved = dict(_os.environ)
        for k in ("STACKD_MINT_KEY", "OPENAI_API_KEY"):
            _os.environ.pop(k, None)
        _os.environ.update({k: v2 for k, v2 in env.items()})
        try:
            return resolve(_Self(v))
        finally:
            _os.environ.clear()
            _os.environ.update(saved)

    with _tf.TemporaryDirectory() as d:
        secret = d + "/ollama_token"
        with open(secret, "w") as f:
            f.write("  FILEKEY  \n")          # compose secret files end in \n
        empty = d + "/empty"
        open(empty, "w").close()
        gone = d + "/no-such-mount"
        check("caller-auth: an explicit valve beats every other source",
              resolve_with(mint_key="VALVE", STACKD_MINT_KEY="E", OPENAI_API_KEY="O")
              == ("VALVE", "valve mint_key"),
              resolve_with(mint_key="VALVE", STACKD_MINT_KEY="E", OPENAI_API_KEY="O"))
        check("caller-auth: STACKD_MINT_KEY env wins over the mounted secret",
              resolve_with(mint_key_file=secret, STACKD_MINT_KEY="E") == ("E", "env STACKD_MINT_KEY"))
        check("caller-auth: the mounted secret is used, whitespace-stripped, and named",
              resolve_with(mint_key_file=secret, OPENAI_API_KEY="O")
              == ("FILEKEY", "secret file " + secret))
        check("caller-auth: a MISSING secret file falls through to OPENAI_API_KEY "
              "(the no-compose-secrets host must keep working)",
              resolve_with(mint_key_file=gone, OPENAI_API_KEY="O") == ("O", "env OPENAI_API_KEY"))
        check("caller-auth: an EMPTY secret file also falls through",
              resolve_with(mint_key_file=empty, OPENAI_API_KEY="O") == ("O", "env OPENAI_API_KEY"))
        check("caller-auth: no source at all yields NO key — never a blank bearer",
              resolve_with(mint_key_file=gone)
              == ("", "unreadable secret file %s (FileNotFoundError)" % gone),
              resolve_with(mint_key_file=gone))
        check("caller-auth: the deployed container's OPENAI_API_KEY= (present, EMPTY) "
              "is not treated as a hit",
              resolve_with(OPENAI_API_KEY="")[0] == "")
        check("caller-auth: a whitespace-only valve is not a hit either",
              resolve_with(mint_key="   ", STACKD_MINT_KEY="E") == ("E", "env STACKD_MINT_KEY"))
        check("caller-auth: the failure label names the FILE, not the env it fell through to",
              "secret file" in resolve_with(mint_key_file=empty)[1]
              and resolve_with(mint_key_file=empty)[0] == "")


def test_prompt_captioning():
    """The MASKED-PATH CAPTIONING CONTRACT, shared with MCP edit_image. Flux.2 is a
    caption model: an instruction-phrased prompt keeps the source object's tokens in
    the conditioning and drags the inpaint back toward the photo, and the CLIP-class
    encoder attends ~77 tokens, so both masked paths reduce the instruction frame to
    a caption of the WANTED result and cap the length — through the ONE module
    (stackd/imagegen/captioning.py), never a copy. Pinned: reducer unit behaviour,
    that api._create applies it BEFORE persisting (row/echo/graph carry the words
    that actually ran), that the user's own words survive as prompt_raw, that an
    already-good prompt mutates NOTHING (the exact-spec echo contract depends on it),
    and the negative controls."""
    import time as _t2
    from stackd.imagegen import captioning as cap
    from stackd.toolbox import jobs as J
    SIZE = (640, 480)          # the module-level PHOTO's dims; specs send them explicitly

    check("captioning: substitution frame reduces to the wanted-result caption",
          cap.describe_edit_target("Replace the red Lamborghini with a blue Audi R8.")
          == "a blue Audi R8")
    check("captioning: make-it frame reduces",
          cap.describe_edit_target("make it a snowy evening") == "a snowy evening")
    check("captioning: add/remove phrasing left alone (no instruction frame to shed)",
          cap.describe_edit_target("remove the trash cans") == "remove the trash cans")
    check("captioning: a plain caption is untouched",
          cap.describe_edit_target("a red bicycle leaning on a wall")
          == "a red bicycle leaning on a wall")
    check("captioning: a too-short reduction is noise, prompt survives intact",
          cap.describe_edit_target("turn it to ok") == "turn it to ok")
    sent_w, note_w = cap.caption_prompt("replace the wall with a sunlit stone garden")
    check("captioning: pipeline returns the reduction and says so",
          sent_w == "a sunlit stone garden" and "caption" in note_w, (sent_w, note_w))
    sent_s, note_s = cap.caption_prompt("a calm lake at dawn")
    check("captioning: unchanged text reports no change with an empty note",
          sent_s == "a calm lake at dawn" and note_s == "", (sent_s, note_s))
    longp = " ".join("w%d" % i for i in range(90))
    sent_l, note_l = cap.caption_prompt(longp)
    check("captioning: caption capped at the model's attention window (~77)",
          len(sent_l.split()) == cap.PROMPT_TOKEN_CAP == 77
          and sent_l.startswith("w0 w1") and "77" in note_l, note_l)
    check("captioning: empty/whitespace prompts are inert, never crash the route",
          cap.caption_prompt("") == ("", "") and cap.caption_prompt("   ") == ("", ""))

    # -- THE ROUTE: api._create captions BEFORE persisting --
    tb = make_tb()
    r = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {
        "width": SIZE[0], "height": SIZE[1], "kind": "replace",
        "prompt": "replace the car with a sunlit red mustang"}}, token=fresh_token())
    sp = (r.payload or {}).get("spec") or {}
    check("create: an instruction ships as the caption the model can follow",
          r.status == 200 and sp.get("prompt") == "a sunlit red mustang", sp)
    check("create: the user's own words are preserved beside it, never overwritten",
          sp.get("prompt_raw") == "replace the car with a sunlit red mustang"
          and sp.get("prompt_note"), sp)
    r2 = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {
        "width": SIZE[0], "height": SIZE[1], "prompt": "a calm lake at dusk"}},
        token=fresh_token())
    check("create: a caption already good is echoed EXACTLY as sent (no keys added)",
          r2.status == 200 and r2.payload.get("spec") == {
              "width": SIZE[0], "height": SIZE[1], "prompt": "a calm lake at dusk"},
          r2.payload.get("spec"))
    r3 = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {
        "width": SIZE[0], "height": SIZE[1], "prompt": ""}}, token=fresh_token())
    check("create: empty prompt (bare cleanup) passes through inert",
          r3.status == 200 and r3.payload.get("spec", {}).get("prompt") == ""
          and "prompt_raw" not in r3.payload.get("spec", {}), r3.payload.get("spec"))

    # -- THE ROW HOLDS THE SAME TRUTH as the echo --
    captured = []
    def render_cap(job, source, mask, *, on_prompt_id=None):
        captured.append(job)
        return (base64.b64encode(b"R").decode(), "image/png")
    qw = J.JobQueue(J.JobStore(":memory:"), render=render_cap)
    qw.start()
    tbw = make_tb(worker=qw)
    rw = post(tbw, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {
        "width": SIZE[0], "height": SIZE[1], "kind": "edit",
        "prompt": "change the sky to a stormy dusk"}}, token=fresh_token())
    _end = _t2.time() + 3.0
    while _t2.time() < _end and not captured:
        _t2.sleep(0.02)
    jspec = (captured[0].get("spec") if captured else {}) or {}
    check("the persisted job row carries the reduced caption the render ran on",
          rw.status == 200 and bool(captured) and jspec.get("prompt") == "a stormy dusk",
          jspec)
    check("and the row keeps the user's raw words next to it (no silent rewrite)",
          jspec.get("prompt_raw") == "change the sky to a stormy dusk", jspec)

    # -- NEGATIVE CONTROLS: the checks above must be able to fail --
    orig = cap.describe_edit_target
    cap.describe_edit_target = lambda p: p
    try:
        ttm = make_tb()
        rm = post(ttm, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {
            "width": SIZE[0], "height": SIZE[1], "prompt":
                "replace the car with a sunlit red mustang"}}, token=fresh_token())
        check("MUTANT identity-reducer: the raw instruction survives (so the route "
              "checks really test the reducer)",
              rm.payload.get("spec", {}).get("prompt")
              == "replace the car with a sunlit red mustang")
    finally:
        cap.describe_edit_target = orig
    orig_cap_n = cap.PROMPT_TOKEN_CAP
    cap.PROMPT_TOKEN_CAP = 10000
    try:
        ttc = make_tb()
        rc = post(ttc, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {
            "width": SIZE[0], "height": SIZE[1], "prompt": longp}}, token=fresh_token())
        check("MUTANT uncapped encoder: the 90-word essay survives (the cap bites)",
              rc.payload.get("spec", {}).get("prompt") == longp)
    finally:
        cap.PROMPT_TOKEN_CAP = orig_cap_n

    # -- SHARED BRAIN, not a copy: imagegen.tools is heavyweight (anyio/mcp/uvicorn,
    # NOT a toolbox dependency and not installed in this offline env), so pin by
    # SOURCE that MCP's masked path routes through the one captioning module — the
    # reducer's behaviour is pinned above, the alias' existence is pinned here, and
    # the regexes existing in exactly ONE file is what "same words, same behaviour"
    # means mechanically. A forked copy in tools.py fails the third clause.
    import pathlib as _p
    it_src = (_p.Path(__file__).resolve().parents[1] / "stackd" / "imagegen" /
              "tools.py").read_text()
    check("MCP edit_image masked path captions through the ONE module (no forked regex)",
          "_captioning.describe_edit_target(prompt)" in it_src
          and "captioning as _captioning" in it_src
          and "_EDIT_INSTR_RE = re.compile" not in it_src)


def test_graphs():
    """M1 painted-mask graph: derived from imagegen's canonical masked-inpaint graph, so the
    ONLY difference allowed is the mask-source node. A silent second difference (a drifted
    node id, a dropped edge into GrowMask, a wrong channel) is exactly the defect that passes
    every unit test and then edits the wrong region on real GPU time -- so pin it here."""
    try:
        from stackd.toolbox import graphs as G
        from stackd.imagegen import workflows as WF
    except Exception as e:  # noqa: BLE001  (imagegen extra not installed on a plain host)
        print(f"  SKIP  graphs checks (imagegen not importable: {e})")
        return
    _n, canon = WF.load_inpaint_graph("flux2-klein")
    painted = G.painted_mask_graph(mask_filename="abc123.png")

    check("painted graph passes its own layout validator",
          G.validate_painted_mask_graph(painted) == [])
    check("mask source is LoadImageMask, not the CLIPSeg text node",
          painted[G.SEGMENT_NODE]["class_type"] == "LoadImageMask")
    check("channel is ALPHA -- normalize() carries the selection in alpha; reading red would "
          "read the constant white fill and inpaint the WHOLE photo",
          painted[G.SEGMENT_NODE]["inputs"]["channel"] == "alpha")
    check("uploaded mask filename is threaded to the mask node",
          painted[G.SEGMENT_NODE]["inputs"]["image"] == "abc123.png")
    check("GrowMask still consumes the mask node output 0 (downstream chain preserved)",
          painted[G.GROW_NODE]["inputs"]["mask"] == [G.SEGMENT_NODE, 0])

    # Drift guard: every node EXCEPT the swapped source (and the CLIPSeg-only prompt node it
    # orphans) must be byte-identical to the canonical graph imagegen owns.
    allowed = {G.SEGMENT_NODE, WF.MASK_REGION_NODE}
    sym = (set(painted) ^ set(canon)) - allowed
    check("node-id set is canonical modulo the intended swap", not sym, str(sym))
    diverged = sorted(k for k in (set(painted) & set(canon)) - allowed if painted[k] != canon[k])
    check("no node other than the mask source diverged: %s" % (",".join(diverged) or "none"),
          not diverged)
    check("the CLIPSeg-only text-prompt node was dropped (fed only the replaced node)",
          WF.MASK_REGION_NODE not in painted)
    check("photo node + save node preserved (graph still loads the image and saves output)",
          G.LOAD_IMAGE_NODE in painted and G.SAVE_NODE in painted)

    # The validator must actually catch the two ways this graph rots.
    bad = G.painted_mask_graph(mask_filename="m.png")
    bad[G.SEGMENT_NODE]["inputs"]["channel"] = "red"
    check("validator rejects channel != alpha",
          any("alpha" in p for p in G.validate_painted_mask_graph(bad)))
    bad2 = G.painted_mask_graph(mask_filename="m.png")
    bad2[G.GROW_NODE]["inputs"]["mask"] = ["99", 0]
    check("validator rejects a broken GrowMask edge",
          any("consume" in p for p in G.validate_painted_mask_graph(bad2)))
    import json as _json
    check("graph is JSON-serialisable (what gets POSTed to /prompt)",
          isinstance(_json.dumps(painted), str))

    # The mode is a live control, not theatre: engine._apply_mode must rewire the KSampler
    # conditioning so 'edit' and 'replace' are genuinely different graphs. 'edit' keeps the
    # scene-referenced chain (positive -> node 23, the ReferenceLatent built from the whole
    # original image); 'replace' drops that full-scene pass (nodes 21 + 23) and conditions on
    # the masked-region latent (node 22) -- the same rewiring imagegen's _submit_edit
    # preserve_scene_context=False performs. If this drifts (a node id changes, the wrong
    # latent is wired), the two modes silently collapse back into one graph and 'replace'
    # stops removing anything -- the exact defect the user hit on a real render.
    from stackd.toolbox.engine import _apply_mode
    edit_g = G.painted_mask_graph(mask_filename="m.png")
    _apply_mode(edit_g, {"kind": "edit"})
    check("edit mode keeps the scene-referenced KSampler conditioning",
          edit_g[WF.MASK_KSAMPLER_NODE]["inputs"]["positive"] == [WF.MASK_FULL_REFLATENT_NODE, 0]
          and WF.MASK_FULL_REFLATENT_NODE in edit_g and WF.MASK_FULL_ENCODE_NODE in edit_g)
    rep_g = G.painted_mask_graph(mask_filename="m.png")
    _apply_mode(rep_g, {"kind": "replace"})
    check("replace mode conditions on the masked region and drops the full-scene pass",
          rep_g[WF.MASK_KSAMPLER_NODE]["inputs"]["positive"] == [WF.MASK_REGION_REFLATENT_NODE, 0]
          and WF.MASK_FULL_REFLATENT_NODE not in rep_g and WF.MASK_FULL_ENCODE_NODE not in rep_g)
    check("replace mode keeps the prompt + region latent the KSampler now depends on",
          WF.MASK_POSITIVE_NODE in rep_g and WF.MASK_REGION_REFLATENT_NODE in rep_g
          and isinstance(_json.dumps(rep_g), str))

    # Painted-mask GEOMETRY (the live dog: a 1.8% paint came back with half the dog
    # repainted). The scaffold's CLIPSeg-era mask-branch constants (expand 12, blur 28,
    # clamp-grow 28, grow_mask_by 6) dilate a PAINTED mask the user already sized —
    # the exact balloon imagegen's _submit_edit abandoned ("a fixed 28 px here is what
    # made small objects balloon"). engine._apply_mask_geometry must zero the growth,
    # rewire the composite to the painted silhouette, and never leave a dangling edge
    # into a deleted node. These are the red gates for that regression.
    from stackd.toolbox import engine as _eng_g, graphs as _G_g
    _gp = G.painted_mask_graph("flux2-klein", mask_filename="m.png")
    _eng_g._patch_graph(_gp, {"prompt": "p", "mask_expand": 0, "mask_edge": 0,
                              "color_match": 0.9},
                      source_filename="s.png", mask_filename="m.png", w=64, h=64, seed=1)
    check("painted mask defaults to HARD geometry: no grow, no blur, no latent dilate",
          _gp[WF.MASK_GROW_NODE]["inputs"]["expand"] == 0
          and [n for n in _gp.values() if n.get("class_type") == "VAEEncodeForInpaint"][0]
                ["inputs"]["grow_mask_by"] == 0
          and WF.MASK_BLUR_NODE not in _gp and "34" not in _gp
          and WF.MASK_CLAMP_MARGIN_NODE not in _gp
          and _gp[WF.MASK_COMPOSITE_NODE]["inputs"]["mask"] == [WF.MASK_GROW_NODE, 0]
          and _gp["28"]["inputs"]["mask"] == [WF.MASK_GROW_NODE, 0],
          repr(_gp.get(WF.MASK_COMPOSITE_NODE))[:110])
    check("patched graph keeps zero dangling references into the deleted soft-edge chain",
          all(v[0] in _gp for nd in _gp.values() for v in (nd.get("inputs") or {}).values()
              if isinstance(v, list) and len(v) == 2)
          and _G_g.validate_painted_mask_graph(_gp) == [])
    _gp2 = G.painted_mask_graph("flux2-klein", mask_filename="m.png")
    _eng_g._patch_graph(_gp2, {"mask_expand": 10, "mask_edge": 30, "color_match": 0.0},
                      source_filename="s", mask_filename="m", w=64, h=64, seed=1)
    check("mask_expand/mask_edge restore the growth honestly (scale with the knobs)",
          _gp2[WF.MASK_GROW_NODE]["inputs"]["expand"] == 10
          and [n for n in _gp2.values() if n.get("class_type") == "VAEEncodeForInpaint"][0]
                ["inputs"]["grow_mask_by"] == 10
          and WF.MASK_BLUR_NODE in _gp2
          and _gp2[WF.MASK_BLUR_NODE]["inputs"]["blur_radius"] == 9
          and _gp2[WF.MASK_CLAMP_MARGIN_NODE]["inputs"]["expand"] == 9
          and _gp2[WF.MASK_COMPOSITE_NODE]["inputs"]["mask"] == ["34", 0])
    check("the graph's global ColorMatchV2 is off the painted path (server paste owns "
          "colour match; color_match 0 means OFF end to end)",
          WF.MASK_COLOR_STRENGTH_NODE not in _gp and "26" not in _gp
          and _gp[WF.MASK_COMPOSITE_NODE]["inputs"]["source"] == [_G_g.DECODE_NODE, 0]
          and _gp2[WF.MASK_COMPOSITE_NODE]["inputs"]["source"] == [_G_g.DECODE_NODE, 0])

    # Mask polarity: the graph edits where the mask is 0 (the CLIPSeg convention the
    # canonical path relies on), but the server mask is 255 = edit-here (what the preview
    # and coverage the user sees are built from). engine._graph_mask flips the alpha at the
    # graph boundary to reconcile the two. If this ever stops inverting, the paint is
    # re-inverted and the user paints a person but the BACKGROUND is replaced instead --
    # verified on a live render (painted half vs its complement). Guard the flip here.
    from stackd.toolbox import engine as _eng
    from PIL import Image as _Img
    import io as _io
    _im = _Img.new("RGBA", (8, 8), (255, 255, 255, 0))
    for _x in range(4):
        for _y in range(8):
            _im.putpixel((_x, _y), (255, 255, 255, 255))
    _b = _io.BytesIO(); _im.save(_b, "PNG"); _src_mask = _b.getvalue()

    def _alpha_mean(png, x0, x1):
        _a = _Img.open(_io.BytesIO(png)).convert("RGBA").getchannel("A")
        _px = _a.load()
        return sum(_px[x, y] for y in range(_a.height) for x in range(x0, x1)) / (_a.height * (x1 - x0))

    _inv = _eng._graph_mask(_src_mask)
    check("graph mask flips the alpha (server 255=edit-here -> graph edit-where-0)",
          _alpha_mean(_src_mask, 0, 4) > 200 and _alpha_mean(_src_mask, 4, 8) < 60
          and _alpha_mean(_inv, 0, 4) < 60 and _alpha_mean(_inv, 4, 8) > 200)

    # Click-to-select graph (SAM3): a standalone tiny graph, GPU-free testable. Pin the
    # wiring that, if it drifted, would silently return the wrong pixels (or none) on paid
    # GPU time -- exactly the discipline validate_painted_mask_graph enforces upstream.
    seg = G.sam3_segment_graph(source_filename="src_abc.png",
                               points=[{"x": 10, "y": 20}],
                               negative_points=[{"x": 30, "y": 40}])
    check("sam3 graph passes its own layout validator",
          G.validate_sam3_segment_graph(seg) == [])
    check("sam3 graph is JSON-serialisable (what gets POSTed to /prompt)",
          isinstance(_json.dumps(seg), str))
    check("SAM3_Detect consumes the CheckpointLoader MODEL (output 0) and LoadImage IMAGE",
          seg[G.SEG_DETECT]["inputs"]["model"] == [G.SEG_CKPT, 0]
          and seg[G.SEG_DETECT]["inputs"]["image"] == [G.SEG_LOAD, 0])
    check("source filename threaded to the LoadImage node",
          seg[G.SEG_LOAD]["inputs"]["image"] == "src_abc.png")
    check("positive+negative clicks reach SAM3_Detect as JSON coord lists",
          _json.loads(seg[G.SEG_DETECT]["inputs"]["positive_coords"]) == [{"x": 10, "y": 20}]
          and _json.loads(seg[G.SEG_DETECT]["inputs"]["negative_coords"]) == [{"x": 30, "y": 40}])
    check("mask reaches SaveImage through MaskToImage (fetchable like any render)",
          seg[G.SEG_MASKIMG]["inputs"]["mask"] == [G.SEG_DETECT, 0]
          and seg[G.SEG_SAVE]["inputs"]["images"] == [G.SEG_MASKIMG, 0])
    check("a pure click does NOT load the CLIP text encoder (point path needs none)",
          G.SEG_CLIP not in seg and "conditioning" not in seg[G.SEG_DETECT]["inputs"])
    txt = G.sam3_segment_graph(source_filename="s.png", points=[], text="the cat")
    check("text segmentation adds the CLIPTextEncode conditioning edge",
          G.SEG_CLIP in txt and txt[G.SEG_DETECT]["inputs"].get("conditioning") == [G.SEG_CLIP, 0])
    bad3 = G.sam3_segment_graph(source_filename="s.png", points=[{"x": 1, "y": 2}])
    bad3[G.SEG_DETECT]["inputs"]["model"] = ["99", 0]
    check("sam3 validator catches a mis-wired model edge",
          any("model" in p for p in G.validate_sam3_segment_graph(bad3)))


def main() -> int:
    test_tokens()
    test_masks()
    test_layers()
    test_routes()
    test_web()
    test_contract()
    test_prompt_captioning()
    test_mint_seam()
    test_spike_wiring()
    test_graphs()
    test_jobs()
    test_working_size()
    test_crop_pipeline()
    test_crop_wiring()
    test_paste_back_resolution()
    test_render_seam()
    test_render_timeout()
    bad = [(n, d) for n, ok, d in CHECKS if not ok]
    for n, d in bad:
        print(f"  FAIL  {n}" + (f"\n          → {d}" if d else ""))
    print(f"\n  {len(CHECKS)-len(bad)}/{len(CHECKS)} toolbox checks passed"
          + ("" if not bad else "  <-- FIX THESE"))
    return 1 if bad else 0


def test_spike_wiring():
    """The M0 harness bugs that made the spike unreadable, now pinned.

    The probe was never wired to the route that serves it, the harness resized only panel
    B (so panel A sat at 240px with an internal scrollbar and looked broken), the HTML cell
    ids did not match what the JS wrote to, and height was measured exactly once, before the
    photo decoded. All four presented as "the iframe mount is failing" while being our own
    bugs — which is the cost of not testing the test rig.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    web = root / "stackd" / "toolbox" / "web"
    js = (web / "toolbox.js").read_text()
    hjs = (web / "harness.js").read_text()
    hhtml = (web / "harness.html").read_text()
    probe = (web / "probe.js").read_text()

    # (a) The probe is instrumentation: reachable on the spike, impossible on a real mount.
    r = get(make_tb(spike_enabled=True), "/toolbox/embed?probe=1&token=" + fresh_token())
    check("probe=1 attaches instrumentation on the spike (otherwise every cell reads ?)",
          r.status == 200 and b"tb-probe" in (r.raw or b""))
    r2 = get(make_tb(spike_enabled=False), "/toolbox/embed?probe=1&token=" + fresh_token())
    check("probe=1 cannot force instrumentation onto a production mount",
          r2.status == 200 and b"tb-probe" not in (r2.raw or b""))
    r3 = get(make_tb(), "/toolbox/embed?token=" + fresh_token())
    check("the spike without ?probe=1 stays clean (default is off)",
          r3.status == 200 and b"tb-probe" not in (r3.raw or b""))

    # (b) Every id the harness writes to must exist. Without this a cell silently shows "?"
    #     forever and gets misread as a failed experiment. Ids the harness looks up split
    #     into two namespaces — the harness page itself, and the editor document inside the
    #     frame — so each is checked against the document that actually owns it.
    wanted = set(re.findall(r"""put\(\s*['"]([\w-]+)['"]""", hjs))
    missing = sorted(i for i in wanted if ('id="%s"' % i) not in hhtml)
    check("harness writes only to ids that exist in its document: %s"
          % (",".join(missing) or "all present"), not missing)
    looked = set(re.findall(r"""getElementById\(\s*['"]([\w-]+)['"]""", hjs))
    embed_doc = (r.raw or b"").decode("utf-8", "replace") if r.status == 200 else ""
    orphans = sorted(i for i in looked if ('id="%s"' % i) not in hhtml
                     and ("'%s'" % i) not in js and ('"%s"' % i) not in js
                     and i not in embed_doc)
    check("every id the harness looks up exists in the harness page or in the frame: %s"
          % (",".join(orphans) or "all present"), not orphans)
    check("harness binds only buttons that exist",
          all(('id="%s"' % b) in hhtml for b in ("t-a", "t-b", "t-all", "reload", "log", "verdict")))
    check("spike serves the same probe URL to BOTH panels so they differ only in mounting",
          "probe=1" in hjs and "frames.A.src = probed" in hjs)

    # (c) Both panels sized; messages attributed by source, not by arrival order.
    check("harness sizes BOTH panels (resizing only B made A look broken to a human)",
          "applyHeight(k || 'B'" in hjs and "A: document.getElementById('fa')" in hjs)
    check("harness attributes frames by e.source",
          "which(e.source)" in hjs and "contentWindow === win" in hjs)

    # (d) Height measured once, before the photo decodes, under-reports and strands the
    #     frame with an internal scrollbar — what the user actually saw. NB: rsplit on
    #     purpose — there are two `im.onload` handlers in the editor and the boot one is the
    #     second, so a first-occurrence window looks at the wrong function entirely.
    check("editor reports height on a ladder, not once", "reportHeightSoon" in js and "1600" in js)
    # Bounded STRUCTURALLY, not by a character budget: the boot handler is the last
    # `im.onload` and the statement right after it is `im.onerror`. A fixed window had to be
    # re-widened every time the onload body grew (a disclosure line about the render ceiling
    # pushed reportHeightSoon() past 1200 once already), and a window that silently grows
    # past its function is a window that eventually reads the wrong function again.
    _tail = js.rsplit("im.onload", 1)[1] if "im.onload" in js else ""
    onload = _tail.split("im.onerror", 1)[0]
    check("editor re-reports after the photo decodes", "reportHeightSoon()" in onload)
    check("probe reports content height AND viewport separately, so 'too tall' and 'frame too "
          "short' are distinguishable instead of both reading as 'scrollable'",
          "vh: window.innerHeight" in probe and "overflowY" in probe
          and "innerH: de.scrollHeight" in probe)
    check("editor observes the host element, not only body", "ro.observe(_host)" in js)

    # (f) The placeholder contract, read FROM web.py rather than from anyone's memory of
    # it. The bug that made the spike silently measure nothing: harness.js was rewritten
    # against a `?embed=` query param while web.harness_document injects the URL by
    # replacing the literal token __TB_EMBED_URL__. `embed` came out null, neither iframe
    # was ever given a src, and the page displayed its own "?" placeholders as if they were
    # experimental results — exactly how a dead rig gets mistaken for a failed mount.
    websrc = (root / "stackd" / "toolbox" / "web.py").read_text()
    tokens_provided = set(re.findall(r"['\"](__TB_[A-Z_]+__)['\"]", websrc))
    check("web.py's substitution contract is discoverable (this test is not vacuous)",
          len(tokens_provided) >= 4, str(sorted(tokens_provided)))
    unconsumed = sorted(t for t in tokens_provided if t not in hhtml and t not in hjs)
    check("harness consumes EVERY placeholder web.py injects: %s"
          % (",".join(unconsumed) or "all consumed"), not unconsumed)
    # Substitution must land end to end, not merely be wired in the abstract.
    hd = W.harness_document("/toolbox/embed?token=ABC123", api="http://x:1",
                            email=EMAIL, token_state="minted")
    check("harness_document really injects the embed URL the frames need",
          "/toolbox/embed?token=ABC123" in hd and "__TB_EMBED_URL__" not in hd)
    check("no placeholder survives into the served page", "__TB_" not in hd)
    # str.replace substitutes EVERY occurrence, and the substitution site must not be
    # quoted in the template: _json_for_script() (json.dumps) already supplies the quotes.
    # Wrapping the token in `'...'` here yielded `embed = '"/toolbox/embed?token=..."'` — a
    # string beginning with a literal double-quote — so both iframes requested a bogus path
    # and the panels rendered blank. Assert no quote character touches the token, and that
    # all occurrences live on the one assignment line (the typeof guard needs two).
    lines = [l for l in hjs.splitlines() if "__TB_EMBED_URL__" in l]
    check("every embed-URL token sits on the single assignment line",
          bool(lines) and all(l.strip().startswith("var embed =") for l in lines),
          " | ".join(l.strip()[:60] for l in lines))
    quoted = [l.strip()[:70] for l in lines
              if "'__TB_EMBED_URL__'" in l or '"__TB_EMBED_URL__"' in l]
    check("the token is NOT quoted in the template (json.dumps supplies quotes)",
          not quoted, " ;; ".join(quoted))
    hd_q = W.harness_document("/toolbox/embed?token=ABC", api="http://x", email=EMAIL,
                              token_state="minted")
    check("substitution yields a clean string literal, not a doubled-quote one",
          'var embed = (typeof "/toolbox/embed?token=ABC" === \'string\')' in hd_q
          and "'\"/toolbox" not in hd_q)

    # (g) Fail-loud: an unfilled cell must be impossible, and the harness must be able to
    #     tell "the sandbox blocked the frame" from "the harness never started".
    check("harness stamps unfilled cells NO DATA instead of leaving them blank",
          "NO DATA" in hjs and "CELLS" in hjs)
    check("harness distinguishes 'never loaded' from 'loaded but silent'",
          "NEITHER PANEL EVER LOADED" in hjs and "frame fired load" in hjs)
    check("harness refuses a confident reading for a frame that never loaded",
          "if (!loaded.A) return;" in hjs)
    check("a missing embed URL is a loud failure, not a silent return",
          "HARNESS BROKEN BEFORE THE TEST STARTED" in hjs)

    # (e) No JS parser on this host, so a brace-balance tripwire beats shipping broken JS.
    def _balanced(src):
        s = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        s = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`", "''", s)
        return (s.count("{") == s.count("}") and s.count("(") == s.count(")")
                and s.count("[") == s.count("]"))
    # (h) The controller must be EXECUTABLE, not merely injected. The bug this pins: an
    # edit to harness.html left the __TB_SCRIPT__ placeholder outside any <script> element,
    # so harness_document() spliced ~7KB of controller code into <body> as TEXT. The page
    # looked healthy, the suite passed, and both panels rendered blank because no code ran
    # and no iframe was ever given a src. Substitution tests cannot see this; only an
    # "everything executable is inside a script element" test can.
    hd2 = W.harness_document("/toolbox/embed?token=Z", api="http://x:1",
                             email=EMAIL, token_state="minted")
    residue = re.sub(r"<script[^>]*>.*?</script>", "", hd2, flags=re.S)
    leaked = [t for t in ("function (", "addEventListener", "__TB_EMBED_URL__",
                          "location.reload") if t in residue]
    check("no controller code leaks into the body as text: %s" % (",".join(leaked) or "clean"),
          not leaked, residue[-300:])
    # Span-based, not rfind-based: harness.js legitimately mentions the text `<script>` in
    # a comment, so locating the "last <script>" naively lands INSIDE the controller and
    # measures a fragment. Only real (open,close) spans can prove where code lives.
    def _in_script(doc, needle):
        spans = [(m.start(), m.end()) for m in
                 re.finditer(r"<script[^>]*>.*?</script>", doc, flags=re.S)]
        at = doc.find(needle)
        return at != -1 and any(s <= at < e for s, e in spans)
    check("the controller is wrapped in a <script> element",
          _in_script(hd2, "addEventListener('message'"))
    check("the injected embed URL is inside that executable element too",
          _in_script(hd2, "/toolbox/embed?token=Z"))
    check("the reload handler is inside the controller, not a bare inline script",
          _in_script(hd2, "rel.addEventListener"))
    check("harness_document refuses to serve a template that dropped its placeholder",
          "__TB_SCRIPT__" in (web / "harness.html").read_text())
    # And the same invariant for the embed document the frames actually load.
    ed = W.embed_document({"api": "http://x", "token": "t"}, probe=True)
    ed_residue = re.sub(r"<script[^>]*>.*?</script>", "", ed, flags=re.S)
    check("embed document keeps all JS inside script elements too",
          "function (" not in ed_residue and "tb-probe" not in ed_residue)
    check("editor code sits inside a script element in the embed document",
          _in_script(ed, "function reportHeight"))
    check("embed document mounts the container the editor boots into",
          "id='tb'" in ed or 'id="tb"' in ed)

    # (i) Every cell must have a working instrument behind it. The server round-trip cells
    # read d.lastFetch, but probe.js never collected or posted it — so those cells could
    # NEVER fill, and the deadman then blamed the probe's silence on the sandbox. A missing
    # sensor is not a negative result, so both sides of the seam are asserted.
    check("probe collects network results AND reports them on the wire",
          "window.fetch = function" in probe and "lastFetch: fetches.last" in probe)
    check("probe wraps fetch without swallowing the editor's own response",
          "return res;" in probe and "throw err;" in probe
          and "res.clone().json()" in probe)
    check("fetches is declared before post() could read it",
          probe.index("var fetches") < probe.index("function post("))
    check("harness reads the field the probe actually sends",
          "d.lastFetch" in hjs and "lastFetch: fetches.last" in probe)
    # Cells are classified by source, so "user hasn't done the step" can never be reported
    # as "the browser blocked us".
    check("harness separates auto-reported cells from user-action-gated ones",
          "kind: 'probe'" in hjs and "kind: 'action'" in hjs and "missingOf(" in hjs)
    check("harness names missing cells instead of printing an anonymous count",
          "c.what" in hjs and "Not yet done:" in hjs)
    check("a pending user step is reported as healthy-but-waiting, not as a failure",
          "RIG IS HEALTHY" in hjs)
    # The internal-scrollbar cause: UA body margin sits outside the reported height.
    css = (web / "toolbox.css").read_text()
    check("embed document zeroes UA body margin so reported height == real height",
          re.search(r"html,\s*body\s*\{[^}]*margin:\s*0", css) is not None)

    # (j) The scrollbar feedback loop, and the rig blind spot that hid it. A classic vertical
    # scrollbar steals ~15px of WIDTH; everything here is laid out at 100%, so the page then
    # overflows sideways, grows a horizontal scrollbar, and the two chase each other. It was
    # reported as "no (fits)" because the probe and the A cross-check only measured scrollHeight.
    check("probe measures the X axis, not just Y (the rig must be able to see a horizontal "
          "scrollbar it is claiming is absent)",
          "overflowX: de.scrollWidth - window.innerWidth" in probe
          and "scrollWidth" in probe and "clientWidth" in probe)
    check("harness consumes overflowX and names which axis overflows",
          "d.overflowX" in hjs and "overX" in hjs and "→ " in hjs)
    check("the same-origin A cross-check measures width too",
          "sw - cw" in hjs and "de.scrollWidth" in hjs)
    check("embed reserves the scrollbar gutter so width never changes when a bar appears",
          "scrollbar-gutter: stable" in css and "overflow-x: clip" in css)
    check("harness page reserves its own gutter (two flex panels + a page bar also fight)",
          "scrollbar-gutter:stable" in hhtml and "overflow-x:clip" in hhtml)
    check("overflow-x uses clip not hidden, so the fix cannot itself mint a second scroll box",
          "overflow-x: clip" in css and "overflow-x: hidden" not in css)

    check("images re-report height on DECODE, not on append (an <img> has no height until it loads)",
          "i.onload = reportHeightSoon" in js and js.count("im.onload = reportHeightSoon") >= 1)
    # ...and the height ladder must not be the only thing that ever fires, since previews and
    # renders land long after 1600ms.
    check("preview/result append triggers a fresh report", js.count("reportHeightSoon()") >= 4)

    # (e) run the balance tripwire last: it is the only thing standing between a typo and
    #     shipping JS that cannot parse, since this host has no node to check it with.

    for name in ("toolbox.js", "harness.js", "probe.js"):
        check("%s braces/parens/brackets balance" % name,
              _balanced((web / name).read_text()))


# ---------------------------------------------------------------- 8. M1 job queue
def test_jobs():
    """M1: the worker state machine, driven entirely with INJECTED fakes — no GPU, no
    httpx, no ComfyUI, no browser. The whole reason submit/cancel are injected seams in
    jobs.JobQueue is so this suite can assert the transitions the daemon relies on."""
    import threading
    import time as _t
    from stackd.toolbox import jobs as J

    def wait_for(store, j, timeout=3.0):
        end = _t.time() + timeout
        while _t.time() < end:
            if store.get(j)["state"] in ("done", "error", "cancelled"):
                break
            _t.sleep(0.02)
        return store.get(j)

    # -- JobStore CRUD + the state vocabulary --
    st = J.JobStore(":memory:")
    jid = st.create(email="a@b.c", kind="retouch", w=640, h=480,
                    spec={"seed": 7, "prompt": "blue"}, mask_info={"coverage_paint": 0.1})
    check("store.create returns a queued row", st.get(jid)["state"] == "queued")
    check("store round-trips the spec json",
          json.loads(st.get(jid)["spec_json"])["prompt"] == "blue")
    try:
        st.set(jid, bogus="x"); check("store.set refuses an unknown field", False)
    except ValueError:
        check("store.set refuses an unknown field", True)
    try:
        st.set(jid, state="exploded"); check("store.set refuses a bad state", False)
    except ValueError:
        check("store.set refuses a bad state", True)

    # -- happy path: queued -> running (records prompt_id) -> done(artifact) --
    seen = {}
    def render_ok(job, source, mask, *, on_prompt_id=None):
        seen["email"] = job["email"]; seen["spec"] = job["spec"]
        seen["src"] = source; seen["mask"] = mask
        on_prompt_id("prompt-123", "http://comfy:8188")
        return (base64.b64encode(b"PNGDATA").decode(), "image/png")
    store = J.JobStore(":memory:")
    q = J.JobQueue(store, render=render_ok, logger=None); q.start()
    jid = store.create(email="a@b.c", kind="retouch", w=640, h=480,
                       spec={"seed": 7, "prompt": "blue"}, mask_info={"coverage_paint": 0.1})
    q.enqueue(jid, source=b"SOURCEBYTES", mask=b"MASKPNG")
    row = wait_for(store, jid)
    check("render reaches done", row["state"] == "done", row["state"])
    check("prompt_id recorded for cancel", row["prompt_id"] == "prompt-123")
    check("engine_base recorded", row["engine_base"] == "http://comfy:8188")
    check("artifact persisted as b64",
          row["artifact_b64"] == base64.b64encode(b"PNGDATA").decode())
    check("render saw the caller's email + spec",
          seen["email"] == "a@b.c" and seen["spec"]["prompt"] == "blue")
    check("render saw the in-flight blobs",
          seen["src"] == b"SOURCEBYTES" and seen["mask"] == b"MASKPNG")
    check("in-flight blobs freed after done", jid not in q._blobs)

    # -- NoEngine / UserNotRegistered / crash all become honest error rows --
    def rn(job, source, mask, *, on_prompt_id=None): raise J.NoEngine("no serveable image engine right now")
    def ru(job, source, mask, *, on_prompt_id=None): raise J.UserNotRegistered("no key on record")
    def rb(job, source, mask, *, on_prompt_id=None): raise RuntimeError("kaboom")
    store = J.JobStore(":memory:")
    qn = J.JobQueue(store, render=rn); qn.start()
    j1 = store.create(email="a@b.c", kind="r", w=64, h=64, spec={}, mask_info={})
    qn.enqueue(j1, source=b"s", mask=b"m")
    r1 = wait_for(store, j1)
    check("NoEngine becomes an error row, not a stall",
          r1["state"] == "error" and "engine" in (r1["error"] or "").lower())
    store2 = J.JobStore(":memory:")
    qu = J.JobQueue(store2, render=ru); qu.start()
    j2 = store2.create(email="x@y.z", kind="r", w=64, h=64, spec={}, mask_info={})
    qu.enqueue(j2, source=b"s", mask=b"m")
    check("UserNotRegistered is an honest error", wait_for(store2, j2)["state"] == "error")
    store3 = J.JobStore(":memory:")
    qb = J.JobQueue(store3, render=rb); qb.start()
    j3 = store3.create(email="a@b.c", kind="r", w=64, h=64, spec={}, mask_info={})
    qb.enqueue(j3, source=b"s", mask=b"m")
    check("a render crash becomes an error row", wait_for(store3, j3)["state"] == "error")
    j3b = store3.create(email="a@b.c", kind="r", w=64, h=64, spec={}, mask_info={})
    qb._render = render_ok
    qb.enqueue(j3b, source=b"s", mask=b"m")
    check("worker SURVIVES a crashed job and runs the next",
          wait_for(store3, j3b)["state"] == "done")
    qn.stop(); qu.stop(); qb.stop()

    # -- cancel a QUEUED job (never started; _process re-checks state and skips it) --
    store = J.JobStore(":memory:")
    cancel_calls = []
    gate = threading.Event()
    def render_slow(job, source, mask, *, on_prompt_id=None):
        on_prompt_id("p-run", "http://comfy:8188")
        gate.wait(2.0)
        return (base64.b64encode(b"X"), "image/png")
    q = J.JobQueue(store, render=render_slow,
                   cancel=lambda pid, base: cancel_calls.append((pid, base)))
    j = store.create(email="a@b.c", kind="r", w=64, h=64, spec={}, mask_info={})
    q.enqueue(j, source=b"s", mask=b"m")           # worker NOT started yet -> still queued
    r = q.cancel(j)
    check("cancelling a queued job reports cancelled", r == "cancelled")
    check("cancelling a queued job drops its blobs", j not in q._blobs)
    q.start()
    check("a queued-then-cancelled job is skipped, never rendered",
          wait_for(store, j)["state"] == "cancelled")
    gate.set()

    # -- cancel a RUNNING job (the injected interrupt fires with the recorded prompt_id) --
    store4 = J.JobStore(":memory:")
    gate2 = threading.Event()
    def render_run(job, source, mask, *, on_prompt_id=None):
        on_prompt_id("p-live", "http://comfy:8188")
        gate2.wait(2.0)
        return (base64.b64encode(b"X"), "image/png")
    calls = []
    q4 = J.JobQueue(store4, render=render_run,
                    cancel=lambda pid, base: calls.append((pid, base)))
    q4.start()
    j = store4.create(email="a@b.c", kind="r", w=64, h=64, spec={}, mask_info={})
    q4.enqueue(j, source=b"s", mask=b"m")
    end = _t.time() + 3.0
    while _t.time() < end and store4.get(j)["state"] != "running":
        _t.sleep(0.02)
    r = q4.cancel(j)
    check("cancelling a running job reports cancelled", r == "cancelled")
    check("running cancel fires the interrupt with the prompt_id",
          ("p-live", "http://comfy:8188") in calls, str(calls))
    gate2.set()
    check("a cancelled-running job stays cancelled after the render returns",
          store4.get(j)["state"] == "cancelled")
    q.stop(); q4.stop()

    # -- orphan sweep: a running row from a dead worker -> error on reconcile_orphans --
    store5 = J.JobStore(":memory:")
    jd = store5.create(email="a@b.c", kind="r", w=64, h=64, spec={}, mask_info={})
    store5.set(jd, state="running")
    swept = store5.reconcile_orphans()
    check("reconcile_orphans sweeps a dead running row", swept == 1)
    check("a swept orphan reads error with a resubmit note, not a spinner",
          store5.get(jd)["state"] == "error" and "again" in (store5.get(jd)["error"] or ""))

    # -- API WITH a mounted queue: create enqueues a real job, poll reads it back --
    def render_echo(job, source, mask, *, on_prompt_id=None):
        on_prompt_id("pid-echo", "http://comfy:8188")
        return (base64.b64encode(b"RESULT-IMG").decode(), "image/png")
    sq = J.JobStore(":memory:")
    qw = J.JobQueue(sq, render=render_echo); qw.start()
    tb = make_tb(worker=qw)
    r = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {"kind": "heal", "seed": 5}},
             token=fresh_token())
    check("with a queue, create returns queued + a job token",
          r.status == 200 and r.payload["state"] == "queued" and r.payload.get("token"))
    check("create is NOT a stub once a queue is mounted", r.payload.get("stub") is False)
    jt = r.payload["token"]; jid = r.payload["job_id"]
    end = _t.time() + 3.0
    last = None
    while _t.time() < end:
        last = post(tb, "/toolbox/jobs/poll", {"job_id": jid}, token=jt)
        if last.payload.get("state") in ("done", "error"):
            break
        _t.sleep(0.02)
    check("poll reaches done with the artifact",
          last.payload["state"] == "done" and last.payload["artifacts"]
          and last.payload["artifacts"][0]["png"] == base64.b64encode(b"RESULT-IMG").decode(),
          str(last.payload))

    # -- REDEEM_ON_CREATE=True is now ENFORCED: a replayed launch token is refused --
    check("REDEEM_ON_CREATE flag is the single source of truth",
          tb_api.REDEEM_ON_CREATE is True)
    replay = T.mint(SECRET, scope="launch", email=EMAIL)
    a = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {}}, token=replay)
    b = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {}}, token=replay)
    check("a launch token creates exactly one job",
          a.status == 200 and b.status == 403, f"{a.status}/{b.status}")
    qw.stop()

    # -- poll/cancel of another user's job, or an unknown id, are both 404 (no id leak) --
    sq2 = J.JobStore(":memory:")
    qv = J.JobQueue(sq2, render=render_echo); qv.start()
    tbv = make_tb(worker=qv)
    jv = sq2.create(email="victim@b.c", kind="r", w=64, h=64, spec={}, mask_info={})
    mine = T.mint(SECRET, scope="job", email=EMAIL, job_id=jv)   # my token, their row
    check("polling a job whose row is owned by a different email is 404",
          post(tbv, "/toolbox/jobs/poll", {"job_id": jv}, token=mine).status == 404)
    check("polling an unknown job id is 404",
          post(tbv, "/toolbox/jobs/poll", {"job_id": "nope"},
               token=T.mint(SECRET, scope="job", email=EMAIL, job_id="nope")).status == 404)
    jc = sq2.create(email=EMAIL, kind="r", w=64, h=64, spec={}, mask_info={})
    ctok = T.mint(SECRET, scope="job", email=EMAIL, job_id=jc)
    rc = post(tbv, "/toolbox/jobs/cancel", {"job_id": jc}, token=ctok)
    check("the cancel route reaches the queue",
          rc.status == 200 and rc.payload["state"] in ("cancelled", "done"), str(rc.payload))
    check("cancel needs a token", post(tbv, "/toolbox/jobs/cancel", {"job_id": jc}).status == 403)
    check("cancel without a mounted queue is a 409",
          post(make_tb(), "/toolbox/jobs/cancel", {"job_id": jc}, token=ctok).status == 409)
    qv.stop()

    # -- /toolbox/launch mints a launch token from a TRUSTED header, and only that --
    tbl = make_tb(worker=J.JobQueue(J.JobStore(":memory:"), render=render_ok))
    f = Fake("GET", "/toolbox/launch?image_id=abc", {})          # no forward-auth header
    tbl.dispatch(f)
    check("launch with no verified identity is refused 403",
          f.status == 403 and f.payload.get("reason") == "no_trusted_identity")
    f2 = Fake("GET", "/toolbox/launch?image_id=abc", {"x-auth-request-user": EMAIL})
    tbl.dispatch(f2)
    loc = (f2.sent_headers or {}).get("location", "")
    check("launch with a verified identity 302s to the embed carrying a minted token",
          f2.status == 302 and loc.startswith("/toolbox/embed?token=v1."), loc)


def test_working_size():
    """The render ceiling: NOTHING is handed to a process above masks.RENDER_MAX_SIDE on
    the long edge — not the browser's <img>, not the SAM3 segmenter, not the ComfyUI
    upload. Measured reason in masks.py: a 2048x1584 Replace at 1.07% coverage sampled for
    486 s on the resident iGPU and was still decoding when the 600 s client deadline fired,
    so a finished render was reported to the user as an error.

    Every assertion is RELATIVE to masks.RENDER_MAX_SIDE, never to the literal 1024, so an
    operator who raises the ceiling with TOOLBOX_RENDER_MAX_SIDE cannot turn this suite red
    for the wrong reason — what it pins is that all four handovers obey ONE number.
    """
    import io as _io
    from stackd.toolbox import jobs as J

    CEIL = M.RENDER_MAX_SIDE
    BIG_SIDE = max(2048, CEIL * 2)                 # always over the ceiling, whatever it is
    BIG = flat_png((BIG_SIDE, int(BIG_SIDE * 0.77)))
    BIG_NAT = M.image_size(BIG)

    # ---------------- fit_within: the sizing rule, pure math, no decoder ----------------
    w, h = M.fit_within(BIG_SIDE, int(BIG_SIDE * 0.77), CEIL)
    check("fit_within caps the long edge at the ceiling", max(w, h) <= CEIL, f"{w}x{h}")
    check("fit_within keeps every side on the 16-px latent grid",
          w % 16 == 0 and h % 16 == 0, f"{w}x{h}")
    drift = abs((w / h) - 1.0 / 0.77) / (1.0 / 0.77)
    check("fit_within preserves aspect to within a grid step", drift < 0.03, f"drift {drift:.3f}")
    w2, h2 = M.fit_within(BIG_SIDE, BIG_SIDE, 1000)         # a deliberately unaligned cap
    check("fit_within never exceeds an unaligned ceiling (rounding cannot push it back over)",
          max(w2, h2) <= 1000 and w2 % 16 == 0 and h2 % 16 == 0, f"{w2}x{h2}")
    check("fit_within leaves a photo that already fits at its own width",
          M.fit_within(800, 608, CEIL)[0] == 800, str(M.fit_within(800, 608, CEIL)))
    check("fit_within honours a ceiling below its own min-side floor",
          M.fit_within(100, 80, 64) == (64, 64), str(M.fit_within(100, 80, 64)))
    try:
        M.fit_within(0, 500, CEIL)
        zero_raised = False
    except ValueError:
        zero_raised = True
    check("fit_within refuses a non-positive size loudly, not as a silent 0x0", zero_raised)

    # ---------------- shrink_to_max_side: the one ingest resize of REAL pixels ----------
    out, sz = M.shrink_to_max_side(BIG, CEIL)
    dec = Image.open(_io.BytesIO(out))
    check("shrink_to_max_side brings an oversized photo inside the ceiling",
          max(sz) <= CEIL and dec.size == (sz[0], sz[1]), f"{sz} decodes {dec.size}")
    check("shrink_to_max_side actually loses pixels (it resized, not just relabelled)",
          out is not BIG and len(out) != len(BIG))
    edge_in = flat_png((1030, 770))
    _o3, sz3 = M.shrink_to_max_side(edge_in, CEIL)
    check("shrink_to_max_side never grows either edge beyond the original",
          sz3[0] <= 1030 and sz3[1] <= 770, f"1030x770 -> {sz3}")
    # The real shape of the pre-fix bug, which "not bigger than the original" CANNOT see:
    # grid-ROUNDING a 2048x1584 photo gives 1024x800 — smaller than the original, yet 8 rows
    # ABOVE the proportional 792, i.e. invented detail, and toolbox edits chain so the next
    # edit inherits it. Pinned at fixed numbers on purpose: this checks the resize RULE,
    # not the configured ceiling, and 792 = 49*16 + 8 sits exactly on the floor/round seam.
    round_probe = flat_png((2048, 1584))
    _o5, sz5 = M.shrink_to_max_side(round_probe, 1024)
    check("shrink_to_max_side FLOORS the short edge (2048x1584 -> 1024x784, not 1024x800)",
          sz5 == (1024, 784), f"2048x1584 -> {sz5}; round-to-nearest gives (1024, 800)")
    small = flat_png((800, 600))
    _o4, sz4 = M.shrink_to_max_side(small, CEIL)
    check("shrink_to_max_side hands back the SAME bytes for a photo already inside",
          _o4 == small and sz4 == (800, 600), f"{sz4}")


    # ---------------- the routes obey it, at every handover ---------------------------
    class FakeWorker:
        """Records exactly what the queue is handed, because THAT is what the GPU gets."""
        def __init__(self):
            self.store = J.JobStore(":memory:")
            self.enqueued = []

        def enqueue(self, job_id, *, source, mask):
            self.enqueued.append((job_id, source, mask))

    fw = FakeWorker()
    tb_big = make_tb(source=lambda email, ref: BIG, worker=fw)
    r = post(tb_big, "/toolbox/jobs",
             # MASK_B64, not a 2048-wide mask: the server resamples the mask to the working
             # size, so the mask's own pixel size is not what is under test here — and
             # building one at 3.2 MP with the per-pixel fixture would cost seconds.
             {"mask_png": MASK_B64,
              "spec": {"kind": "replace", "width": BIG_SIDE, "height": int(BIG_SIDE * 0.77)}},
             token=fresh_token())
    check("job create caps what the CLIENT asked for (the old 4096-wide hole)",
          r.status == 200 and max(r.payload["working_size"]) <= CEIL, str(r.payload))
    enq_size = Image.open(_io.BytesIO(fw.enqueued[0][1])).size if fw.enqueued else None
    # The bytes handed to the QUEUE are deliberately NOT ceiling-capped any more: they are
    # BOTH the graph input AND the photo the crop-and-paste composites back into, and the
    # graph sizes itself (engine._prepare_source / crop_for_render). A pre-shrunk enqueue
    # made the ceiling the OUTPUT resolution too — the paste-back ratchet defect. What must
    # stay capped is the size the GRAPH runs at: pinned by working_size below, by the
    # upload checks in test_render_seam / test_paste_back_resolution, and enforced at the
    # handover itself by engine.render_size().
    check("job create enqueues the RAW photo (the paste-back base), unshrunk",
          enq_size == BIG_NAT and fw.enqueued[0][1] == BIG, str(enq_size))
    check("job create SAYS it shrank, and echoes the size it shrank from",
          "auto-shrunk" in (r.payload.get("size_note") or "")
          and r.payload.get("source_size") == list(BIG_NAT), str(r.payload.get("size_note")))
    check("job create reports the ceiling it applied",
          r.payload.get("max_side") == CEIL, str(r.payload.get("max_side")))

    fw2 = FakeWorker()
    tb_ok = make_tb(worker=fw2)                      # the default PHOTO is 640x480, inside
    r2 = post(tb_ok, "/toolbox/jobs",
              {"mask_png": MASK_B64, "spec": {"kind": "heal"}}, token=fresh_token())
    check("a photo that already fits is NOT re-encoded or shrunk on the way to the queue",
          fw2.enqueued and fw2.enqueued[0][1] == PHOTO,
          str(len(fw2.enqueued[0][1]) if fw2.enqueued else None))
    check("a photo that already fits gets no shrink notice",
          r2.status == 200 and r2.payload.get("size_note") == "",
          str(r2.payload.get("size_note")))

    # The browser: the data: URI in the embed document IS the photo it paints and the
    # reference the mask is drawn against, so an uncapped one costs four O(pixels) canvases.
    emb = get(make_tb(source=lambda email, ref: BIG),
              "/toolbox/embed?image_id=x", token=fresh_token())
    mdoc = re.search(rb"window\.__TB__=(\{.*?\});</script>", emb.raw or b"", re.S)
    cfg_j = json.loads(mdoc.group(1).decode()) if mdoc else {}
    img_b64 = (cfg_j.get("image") or "").split(",", 1)[-1]
    handed = Image.open(_io.BytesIO(base64.b64decode(img_b64))) if img_b64 else None
    check("embed hands the browser the photo ALREADY inside the ceiling",
          handed is not None and max(handed.size) <= CEIL,
          str(handed.size if handed else None))
    check("embed advertises the same ceiling it enforces (canvas and render cannot drift)",
          cfg_j.get("max_side") == CEIL, str(cfg_j.get("max_side")))
    check("the ceiling is ONE number read from masks, not re-hardcoded per route",
          tb_api._masks is M and tb_api._masks.RENDER_MAX_SIDE == CEIL)


    # ---------------- the belt at the actual handover to ComfyUI -----------------------
    from stackd.toolbox import engine as E
    cw, ch, capped = E.render_size({"working_w": BIG_SIDE, "working_h": int(BIG_SIDE * 0.77)})
    check("engine caps a row that predates the ceiling, at the GPU handover",
          max(cw, ch) <= CEIL and capped is True, f"{cw}x{ch}")
    small_row = {"working_w": 640, "working_h": 480}
    cw2, ch2, capped2 = E.render_size(small_row)
    check("engine leaves an already-capped row exactly alone (a belt that always bites is a bug)",
          capped2 is False and (cw2, ch2) == (640, 480), f"{cw2}x{ch2} capped={capped2}")
    cw3, ch3, _c3 = E.render_size({"working_w": "junk", "working_h": None})
    check("engine survives a junk row rather than throwing at the GPU",
          max(cw3, ch3) <= CEIL and cw3 > 0 and ch3 > 0, f"{cw3}x{ch3}")

    # A helper nobody calls is exactly the objSig()/tooltip class of unenforced promise this
    # package keeps getting bitten by, so the WIRING is measured, in the comfy_render body
    # only (a bare-name grep would also be satisfied by a comment or by the helper itself).
    eng_src = (pathlib.Path(__file__).resolve().parent.parent
               / "stackd" / "toolbox" / "engine.py").read_text()
    cr_body = eng_src.split("def comfy_render", 1)[1].split("\ndef ", 1)[0]
    check("the ceiling is WIRED: comfy_render actually calls render_size(job)",
          "render_size(job)" in cr_body, "helper exists, nobody calls it")
    check("when the belt bites, the mask is rescaled so source and mask cannot disagree",
          "resize_canonical(" in cr_body, "mask would stay at the oversized dims")
    check("comfy_render cannot fall back to reading the row size directly (render_size owns it)",
          "working_w" not in cr_body, "an uncapped direct read of the row is back")

    # resize_canonical must rescale WITHOUT re-applying geometry: normalize_layers already
    # grew/feathered each object, and doing it twice is the double-grow bug fixed once.
    src_mask = rect_mask((1024, 768), (300, 200, 700, 560))
    frac_before = M.coverage(src_mask)
    smaller = M.resize_canonical(src_mask, 512, 384)
    frac_after = M.coverage(smaller)
    check("resize_canonical rescales the mask to the size asked for",
          Image.open(_io.BytesIO(smaller)).size == (512, 384),
          str(Image.open(_io.BytesIO(smaller)).size))
    # Tolerance is LANCZOS edge ringing, NOT slack for a morph: a re-applied grow/feather
    # moves the perimeter by its radius, which at these sizes is >>2% of the canvas.
    check("resize_canonical changes NOTHING but size (coverage fraction preserved)",
          frac_before is not None and abs(frac_after - frac_before) < 0.02,
          f"{frac_before:.5f} -> {frac_after:.5f}")
    check("resize_canonical is a byte-for-byte no-op when the size already agrees",
          M.resize_canonical(src_mask, 1024, 768) == src_mask)

    # the segmenter shares the rule: its docstring claimed 16-alignment and did not do it
    sw, sh = E._seg_size(BIG, 1024)
    check("_seg_size puts the SAM3 mask on the same 16-px grid as the render",
          sw % 16 == 0 and sh % 16 == 0 and max(sw, sh) <= 1024, f"{sw}x{sh}")

def test_crop_pipeline():
    """The resolution pipeline: crop small selections, render at the ceiling, paste back at
    the PHOTO's resolution.

    Measured reason: the 1024 ceiling that stopped 486 s iGPU stalls also caps the DETAIL of
    every output, so re-editing a pasted result re-encodes an already-downsampled frame and
    the image ratchets toward mush. plan_crop keeps the box in CANVAS px and maps
    canvas->source proportionally only at use time, which is what lets a 4000x3000 phone
    photo be crop-rendered at the ceiling and pasted back at full resolution.

    Thresholds are read off masks.* (LATENT_GRID / MIN_CROP_SIDE / CROP_AFFORDABLE), never a
    literal, for the same reason test_working_size does it: an operator who raises
    TOOLBOX_RENDER_MAX_SIDE must not turn this suite red for the wrong reason.

    The knob assertions check DIRECTION, not survival. "It ran" is not evidence here: an
    AttributeError on the band API made color_match crash outright, while the LAB blends fell
    through their own except into `normal` and merely returned a note string -- dead knobs
    that a suite checking only for exceptions reports green. Each knob is therefore compared
    against its own knob-off baseline and must measurably move pixels.
    """
    from PIL import ImageChops, ImageStat
    if not M.HAS_PIL:
        check("crop pipeline: PIL present", False, "skipped")
        return
    import random
    G = M.LATENT_GRID
    CW, CH = G * 32, G * 24                                # 512x384 canvas, grid-aligned

    def textured(size, seed=11):
        """Texture with real per-channel stddev, and no per-pixel Python loop: noise and
        gradient are both C-level, so this stays cheap at megapixel sizes (see flat_png's
        note). A FLAT band has no stddev to transfer, and a suite built on flat images is
        exactly how a dead color_match knob stayed green."""
        n = Image.effect_noise(size, 70).convert("RGB")
        g = Image.linear_gradient("L").resize(size).convert("RGB")
        im = ImageChops.add(ImageChops.multiply(n, g), n)
        d = ImageDraw.Draw(im)
        rnd = random.Random(seed)
        for _ in range(24):
            x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
            d.ellipse([x, y, x + rnd.randrange(8, 48), y + rnd.randrange(8, 48)],
                      fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
        return im

    def save(im):
        buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()

    def band_mean(b, box=None):
        im = Image.open(io.BytesIO(b)).convert("RGB")
        if box:
            im = im.crop(box)
        return sum(ImageStat.Stat(im).mean) / 3.0

    def diff(a, b):
        x = Image.open(io.BytesIO(a)).convert("RGB")
        y = Image.open(io.BytesIO(b)).convert("RGB")
        if x.size != y.size:
            return float("inf")
        return sum(ImageStat.Stat(ImageChops.difference(x, y)).mean) / 3.0

    def mask_at(size, box):
        im = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(im).rectangle(list(box), fill=(255, 255, 255, 255))
        return save(im)

    # ---------------- plan_crop: alignment is a contract, not a nicety --------------
    # An unaligned width/height is a flat rejection by a Flux graph, so this is the
    # difference between a render and an exception after real GPU time.
    plan = M.plan_crop(mask_at((CW, CH), (120, 100, 210, 190)))
    check("plan_crop returns a plan for a small selection", plan is not None)
    if plan:
        bx0, by0, bx1, by1 = plan["box"]
        rw, rh = plan["size"]
        check("plan_crop: origins grid-aligned", bx0 % G == 0 and by0 % G == 0, plan["box"])
        check("plan_crop: sizes are whole grid steps", rw % G == 0 and rh % G == 0, plan["size"])
        check("plan_crop: box and size agree", bx1 - bx0 == rw and by1 - by0 == rh,
              (plan["box"], plan["size"]))
        check("plan_crop: box stays inside the frame",
              bx0 >= 0 and by0 >= 0 and bx1 <= CW and by1 <= CH, plan["box"])
        check("plan_crop: box clears the model-comfort floor",
              rw >= M.MIN_CROP_SIDE and rh >= M.MIN_CROP_SIDE, plan["size"])
        check("plan_crop: the margin ring surrounds the selection",
              bx0 < 120 and by0 < 100 and bx1 > 210 and by1 > 190, plan["box"])
        check("plan_crop: frame is the mask's own size", plan["frame"] == (CW, CH), plan["frame"])
    check("plan_crop bails on an empty mask",
          M.plan_crop(mask_at((CW, CH), (0, 0, 0, 0))) is None)
    check("plan_crop bails when the box saves nothing (no seam for nothing)",
          M.plan_crop(mask_at((CW, CH), (6, 6, CW - 6, CH - 6))) is None)

    # ---------------- render_budget: the use-time spend of the photo's detail ----------
    # The defect it retires: rendering at CANVAS-space plan size when the photo under the
    # box holds more real pixels, then upscaling that soft artifact at paste time — and
    # because the artifact seeds the NEXT edit, each round trip lost detail (the ratchet).
    RB = {"box": (96, 96, 384, 384), "frame": (512, 384), "size": (288, 288)}
    check("render_budget: a photo SMALLER than the frame never shrinks the planned box",
          M.render_budget(RB, (128, 96)) == (288, 288), str(M.render_budget(RB, (128, 96))))
    check("render_budget: source==frame renders the box itself (no gratuitous upscale)",
          M.render_budget(RB, (512, 384)) == (288, 288), str(M.render_budget(RB, (512, 384))))
    rb4 = M.render_budget(RB, (2048, 1536))
    check("render_budget: a 4x photo spends the whole ceiling on the crop",
          rb4 == (M.RENDER_MAX_SIDE // G * G,) * 2, "%s vs ceiling %s" % (rb4, M.RENDER_MAX_SIDE))
    # truncation, never round-up: 256 x 1177/512 = 588.5 -> 576. Round-up (592) would
    # fabricate a column the photo never had — the 2048x1584 -> 1024x800 incident again,
    # documented at shrink_to_max_side. This check exists for that, not for the numbers.
    rbx = M.render_budget({"box": (0, 0, 256, 160), "frame": (512, 384),
                           "size": (256, 160)}, (1177, 883))
    check("render_budget FLOORS to the grid (588.5x367.9 maps to 576x352, never up)",
          rbx == (576, 352), "%s; round-up would give (592, 368)" % (rbx,))
    check("render_budget falls back to the plan on junk input instead of guessing",
          M.render_budget(RB, None) == (288, 288))

    # The margin ring, asserted where it is the ONLY thing that can produce the result.
    # A small selection cannot test this: the widen-to-MIN_CROP_SIDE step inflates the box
    # past the selection all by itself, so "the box surrounds the selection" passes even with
    # ring=0 -- a mutant that proved dead here. A selection whose long edge ALREADY clears
    # MIN_CROP_SIDE removes that escape, so the box width is selection + 2*ring or nothing.
    # A visible seam is the failure mode: the model can only continue a texture or a light
    # gradient from context it was actually given.
    WIDE = (100, 140, 380, 240)
    wide_plan = M.plan_crop(mask_at((CW, CH), WIDE))
    check("plan_crop: a wide selection is planned", wide_plan is not None)
    if wide_plan:
        wx0, wy0, wx1, wy1 = wide_plan["box"]
        sel_w = WIDE[2] - WIDE[0]
        check("plan_crop: the ring adds real context, not just the comfort floor",
              (wx1 - wx0) >= sel_w + 2 * M.CROP_MARGIN_MIN - 3 * G,
              "box w=%d for a %d-wide selection (ring=%d)"
              % (wx1 - wx0, sel_w, M.CROP_MARGIN_MIN))
        check("plan_crop: a wide selection stays on the grid",
              wide_plan["size"][0] % G == 0 and wide_plan["size"][1] % G == 0,
              wide_plan["size"])
    # ---------------- the anti-ratchet claim, at 4x phone resolution -----------------
    CANVAS, PHOTO = (CW, CH), (CW * 4, CH * 4)
    photo = save(textured(PHOTO))
    big_plan = M.plan_crop(mask_at(CANVAS, (150, 110, 330, 290)))
    check("plan_crop needs only the canvas mask (no photo => no staleness)",
          big_plan is not None)
    if big_plan:
        crop_bytes, crop_size = M.crop_for_render(photo, big_plan)
        # crop_for_render hands back the size it cropped AT, so the caller patches the
        # graph's width/height nodes with the same numbers rather than re-deriving them.
        check("crop_for_render returns bytes at exactly the plan's render size",
              isinstance(crop_bytes, bytes) and crop_size == tuple(big_plan["size"])
              and M.image_size(crop_bytes) == tuple(big_plan["size"]),
              "%s vs plan %s" % (crop_size, big_plan["size"]))
        check("crop_for_render obeys the ceiling on a huge photo (nothing oversized reaches a process)",
              max(crop_size) <= M.RENDER_MAX_SIDE, "%s from a %s photo" % (crop_size, PHOTO))
        out, note = M.paste_back(photo, save(Image.new("RGB", big_plan["size"], (9, 9, 9))),
                                 big_plan)
        check("paste_back returns the PHOTO's resolution, not the canvas's (ratchet ended)",
              M.image_size(out) == PHOTO, "%s note=%r" % (M.image_size(out), note))
        check("crop_mask stays registered with the crop",
              M.image_size(M.crop_mask(photo, big_plan)) == tuple(big_plan["size"]),
              M.image_size(M.crop_mask(photo, big_plan)))

    # ---------------- the four knobs, by direction ----------------------------------
    PLAN = {"box": (G * 4, G * 4, G * 28, G * 20), "frame": (CW, CH),
            "size": (G * 24, G * 20)}
    PHOTO_S = save(textured((CW, CH), seed=3))
    # artefact deliberately dark against a bright surround, so "did the histogram move"
    # has an unambiguous sign rather than just "did it differ"
    ART_S = save(ImageChops.multiply(textured(PLAN["size"], seed=5),
                                     Image.new("RGB", PLAN["size"], (30, 36, 46))))
    base_out, base_note = M.paste_back(PHOTO_S, ART_S, PLAN)
    check("paste_back: plain path records no complaint", base_note == "", base_note)
    check("paste_back: plain path actually pastes", diff(PHOTO_S, base_out) > 1.0)

    try:
        cm_out, cm_note = M.paste_back(PHOTO_S, ART_S, PLAN, color_match=1.0)
        check("color_match: no silent degradation", cm_note == "", cm_note)
        off, on = band_mean(base_out, PLAN["box"]), band_mean(cm_out, PLAN["box"])
        surr = band_mean(PHOTO_S, PLAN["box"])
        check("color_match: pulls a dark crop toward its bright surround",
              on > off + 3.0, "off=%.1f on=%.1f surround=%.1f" % (off, on, surr))
        check("color_match: does not stop at the surround's level (it transfers, not clamps)",
              abs(on - surr) < abs(off - surr), "off->surr=%.1f on->surr=%.1f"
              % (abs(off - surr), abs(on - surr)))
        # The flat-band case is the one the old stddev guard silently skipped: a solid grey
        # patch has no scale to transfer, only a LEVEL, so mean-only fallback is the whole
        # difference between a working knob and a no-op.
        grey = save(Image.new("RGB", PLAN["size"], (128, 128, 128)))
        g_plain, _ = M.paste_back(PHOTO_S, grey, PLAN)
        g_cm, g_note = M.paste_back(PHOTO_S, grey, PLAN, color_match=1.0)
        check("color_match: works on a FLAT band (the case the stddev guard used to bail on)",
              band_mean(g_cm, PLAN["box"]) != band_mean(g_plain, PLAN["box"])
              and g_note == "",
              "flat cm=%.1f plain=%.1f note=%r"
              % (band_mean(g_cm, PLAN["box"]), band_mean(g_plain, PLAN["box"]), g_note))
    except Exception as e:  # noqa: BLE001
        check("color_match does not raise", False, "%s: %s" % (type(e).__name__, e))

    for mode in ("multiply", "screen", "overlay", "soft_light", "hard_light",
                 "luminosity", "color"):
        try:
            mo, mn = M.paste_back(PHOTO_S, ART_S, PLAN, blend_mode=mode)
            no, _n = M.paste_back(PHOTO_S, ART_S, PLAN, blend_mode="normal")
            check("blend_mode %s: no silent degradation" % mode, mn == "", mn)
            check("blend_mode %s: differs from normal (the knob bites)" % mode,
                  diff(mo, no) > 1.0, "diff=%r" % diff(mo, no))
        except Exception as e:  # noqa: BLE001
            check("blend_mode %s applies" % mode, False, "%s: %s" % (type(e).__name__, e))

    _bad, bad_note = M.paste_back(PHOTO_S, ART_S, PLAN, blend_mode="technically_perfect")
    check("unknown blend_mode degrades WITH a note rather than a crash",
          "normal" in bad_note, bad_note)

    full_o, _ = M.paste_back(PHOTO_S, ART_S, PLAN, opacity=1.0)
    none_o, n_note = M.paste_back(PHOTO_S, ART_S, PLAN, opacity=0.0)
    half_o, _ = M.paste_back(PHOTO_S, ART_S, PLAN, opacity=0.5)
    check("opacity 0 leaves the photo untouched", diff(PHOTO_S, none_o) < 0.5,
          "diff=%r note=%r" % (diff(PHOTO_S, none_o), n_note))
    check("opacity 0.5 lands between untouched and full",
          0.5 < diff(PHOTO_S, half_o) < diff(PHOTO_S, full_o) + 0.5,
          "half=%r full=%r" % (diff(PHOTO_S, half_o), diff(PHOTO_S, full_o)))

    d0, _ = M.paste_back(PHOTO_S, ART_S, PLAN, preserve_detail=0.0)
    d1, _ = M.paste_back(PHOTO_S, ART_S, PLAN, preserve_detail=1.0)
    check("preserve_detail re-injects the original's high frequencies",
          diff(d0, d1) > 1.0, "diff=%r" % diff(d0, d1))

    # A dead knob must be impossible to re-introduce quietly: if the LAB swap ever falls
    # back to normal again, these two lines go red rather than the suite shrugging.
    lum, lum_note = M.paste_back(PHOTO_S, ART_S, PLAN, blend_mode="luminosity")
    col, col_note = M.paste_back(PHOTO_S, ART_S, PLAN, blend_mode="color")
    check("luminosity and color are distinct operations, not one shared fallback",
          diff(lum, col) > 1.0 and lum_note == "" and col_note == "",
          "diff=%r %r %r" % (diff(lum, col), lum_note, col_note))
def test_paste_back_resolution():
    """create -> queue -> render, E2E, on the bytes the REAL route produced.

    Why a third crop test, after test_crop_pipeline (masks.py in isolation) and
    test_render_seam (comfy_render driven directly): the paste-back base defect never
    lived in comfy_render — it lived in the ONE handover before it, api._ingest_source
    shrinking the photo BEFORE enqueue. A seam test that feeds comfy_render its own
    full-resolution photo physically cannot see an API that never delivers one. This
    drives the real POST /toolbox/jobs, captures what the queue was handed, and carries
    THOSE bytes to the render: photo raw in, artifact at photo resolution out.
    """
    import copy
    import types
    from stackd.toolbox import engine as E
    from stackd.toolbox import jobs as J
    if not M.HAS_PIL:
        check("paste-back resolution: PIL present", False, "skipped")
        return
    CEIL = M.RENDER_MAX_SIDE
    SIDE = max(2048, CEIL * 2)                       # comfortably over the ceiling, whatever it is
    BIGPNG = flat_png((SIDE, int(SIDE * 0.77)))
    BIG_NAT = M.image_size(BIGPNG)

    class CaptureWorker:
        """The FakeWorker pattern from test_working_size: records exactly what the queue
        is handed, because THAT is what the render will composite against."""
        def __init__(self):
            self.store = J.JobStore(":memory:")
            self.enqueued = []
        def enqueue(self, job_id, *, source, mask):
            self.enqueued.append((job_id, source, mask))

    fw = CaptureWorker()
    tb = make_tb(source=lambda email, ref: BIGPNG, worker=fw)
    r = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {"kind": "heal"}},
             token=fresh_token())
    check("paste-back resolution: the job created through the real route",
          r.status == 200 and bool(fw.enqueued), str(r.payload)[:140])
    if not fw.enqueued:
        for n in ("the queue is given the unshrunk photo",
                  "that mask crop-renders (this is the crop path)",
                  "the render seam ran on the enqueued bytes",
                  "the upload spends the budget on the crop",
                  "the ARTIFACT returns at the photo's resolution (anti-ratchet)",
                  "provenance states the render size it actually ran"):
            check("paste-back resolution: " + n, False, "skipped: nothing enqueued")
        return
    src_bytes, msk_bytes = fw.enqueued[0][1], fw.enqueued[0][2]
    check("paste-back resolution: the queue is given the UNSHRUNK photo",
          M.image_size(src_bytes) == BIG_NAT, "%s vs %s" % (M.image_size(src_bytes), BIG_NAT))
    check("paste-back resolution: the canonical mask sits on the working grid, not the photo",
          M.image_size(msk_bytes) == tuple(r.payload["working_size"]),
          "%s vs %s" % (M.image_size(msk_bytes), r.payload["working_size"]))
    plan = M.plan_crop(msk_bytes)
    check("paste-back resolution: that mask crop-renders (this is the crop path)",
          plan is not None)

    def absent(reason):
        for n in ("the render seam ran on the enqueued bytes",
                  "the upload spends the budget on the crop",
                  "the ARTIFACT returns at the photo's resolution (anti-ratchet)",
                  "provenance states the render size it actually ran"):
            check("paste-back resolution: " + n, False, reason)
    if plan is None:
        absent("skipped: no crop plan — pixels cannot be probed")
        return

    # fake GPU clients, same shapes as test_render_seam's (signatures mirror the real ones)
    state = {}
    cc = types.ModuleType("stackd.imagegen.comfyui_client")

    async def upload_to_comfy(image_bytes, filename_prefix, base):
        state.setdefault("uploads", []).append(image_bytes)
        return "%s-%d.png" % (filename_prefix, len(state["uploads"]))

    async def submit_workflow(workflow, base):
        return "pid-pbr"

    async def wait_and_fetch(prompt_id, include_node_ids, base, timeout_s=None, on_poll=None):
        w, h = M.image_size(state["uploads"][0])
        return {node: [flat_png((w, h))] for node in include_node_ids}

    def downscale_to_exact_size(image_bytes, width, height):
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize(
            (width, height), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()

    cc.upload_to_comfy = upload_to_comfy
    cc.submit_workflow = submit_workflow
    cc.wait_and_fetch = wait_and_fetch
    cc.downscale_to_exact_size = downscale_to_exact_size
    cc.get_json = lambda *a, **k: None
    ow = types.ModuleType("stackd.imagegen.openwebui_client")

    async def save_image(image_bytes, filename, api_key):
        return "http://owu/x/" + filename
    ow.save_image = save_image

    pkg = sys.modules.get("stackd.imagegen") or __import__("stackd.imagegen")
    saved = {k: sys.modules.get(k) for k in
             ("stackd.imagegen.comfyui_client", "stackd.imagegen.openwebui_client")}
    saved_attr = (getattr(pkg, "comfyui_client", "$"), getattr(pkg, "openwebui_client", "$"))
    sys.modules["stackd.imagegen.comfyui_client"] = cc
    sys.modules["stackd.imagegen.openwebui_client"] = ow
    pkg.comfyui_client, pkg.openwebui_client = cc, ow

    class FakeRuntime:
        def resolve_user_key(self, email):
            return "key-for-" + (email or "")
    real_runtime, real_base = E._runtime, E.comfyui_base
    E._runtime = lambda: FakeRuntime()
    E.comfyui_base = lambda: ("http://comfy:8188", "")
    job = {"email": EMAIL, "spec": {"kind": "heal"},
           "working_w": r.payload["working_size"][0],
           "working_h": r.payload["working_size"][1]}
    art = None
    try:
        b64, _t = E.comfy_render(job, src_bytes, msk_bytes, on_prompt_id=lambda pid, b: None)
        art = base64.b64decode(b64) if b64 else None
    except Exception as e:  # noqa: BLE001 — reported as a red check, not a traceback
        state["raised"] = e
    finally:
        E._runtime, E.comfyui_base = real_runtime, real_base
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        pkg.comfyui_client, pkg.openwebui_client = saved_attr

    check("paste-back resolution: the render seam ran on the enqueued bytes",
          art is not None, repr(state.get("raised")))
    if art is None:
        absent("skipped: seam raised")
        return
    budget = M.render_budget(plan, BIG_NAT)
    up = M.image_size(state["uploads"][0])
    check("paste-back resolution: the upload spends the budget on the crop",
          up == tuple(budget) and max(up) > max(plan["size"]) and max(up) <= CEIL,
          "upload %s budget %s plan %s" % (up, budget, plan["size"]))
    check("paste-back resolution: the ARTIFACT returns at the photo's resolution (anti-ratchet)",
          M.image_size(art) == BIG_NAT,
          "%s vs photo %s — smaller means the queue was handed a shrunk base again"
          % (M.image_size(art), BIG_NAT))
    cj = json.loads(job.get("_crop_json") or "{}")
    check("paste-back resolution: provenance states the render size it actually ran",
          bool(cj.get("cropped")) and tuple(cj.get("rendered_at") or ()) == up
          and tuple(cj.get("size") or ()) == up and cj.get("composited") is True,
          str(cj)[:160])


def test_render_seam():
    """comfy_render's geometry, driven end-to-end with FAKE GPU clients.

    Why this exists separately: the masks-level suite proves paste_back is correct, and the
    store-level checks prove provenance persists, but neither proves the RENDER SEAM chooses
    the right geometry. Two mutants survived the first mutation run precisely because the
    only assertions there were string greps over engine.py -- disabling the crop branch
    outright kept every name present and the suite green. A check that cannot fail is worse
    than none, so this drives the real function and measures the bytes it uploads and returns.

    No GPU and no httpx are needed: engine imports comfyui_client/openwebui_client LAZILY
    inside _run (the documented [imagegen] extra), so fakes injected into sys.modules reach
    the seam while the rest of the package stays stdlib-only.
    """
    import copy
    import types
    from stackd.toolbox import engine as E
    from stackd.toolbox import graphs as GG
    if not M.HAS_PIL:
        check("render seam: PIL present", False, "skipped")
        return

    G_ = M.LATENT_GRID
    CANVAS = (G_ * 32, G_ * 24)                       # 512x384
    PHOTO = (CANVAS[0] * 4, CANVAS[1] * 4)            # a phone-resolution original

    def solid_png(size, rgb):
        buf = io.BytesIO()
        Image.new("RGB", size, rgb).save(buf, "PNG")
        return buf.getvalue()

    def mask_png(size, box):
        im = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(im).rectangle(list(box), fill=(255, 255, 255, 255))
        buf = io.BytesIO(); im.save(buf, "PNG")
        return buf.getvalue()

    def build_fakes(state):
        cc = types.ModuleType("stackd.imagegen.comfyui_client")

        async def upload_to_comfy(image_bytes, filename_prefix, base):
            state.setdefault("uploads", []).append(image_bytes)
            return "%s-%d.png" % (filename_prefix, len(state["uploads"]))

        async def submit_workflow(workflow, base):
            state["graph"] = copy.deepcopy(workflow)
            state["base"] = base
            return "pid-seam-1"

        async def wait_and_fetch(prompt_id, include_node_ids, base, timeout_s=None,
                                 on_poll=None):
            # Mirrors the REAL signature. The seam gained a per-call deadline and a liveness
            # hook, and this fake not matching them is precisely the drift a suite exists to
            # catch -- it raised TypeError here until both sides agreed.
            state["timeout_s"] = timeout_s
            state.setdefault("timeout_calls", []).append(timeout_s)
            if on_poll is not None:
                for e in (1.0, 2.0, 3.0):    # the progress callback must actually be driven
                    on_poll(e)
            # the model returns whatever the graph was TOLD to render, so a seam that
            # uploads the wrong geometry produces a visibly wrong artifact size here
            w, h = M.image_size(state["uploads"][0]) if state.get("uploads") else (64, 64)
            state["model_size"] = (w, h)
            # Timeout behaviour, so the seam's give-up path can be driven end to end:
            #   "late"   the primary wait blows its deadline, but the render lands during the
            #            grace window -- which is exactly what the grace exists to catch
            #   "always" it never lands at all, and we must give up AND free the GPU
            # The harvest call is distinguishable by timeout_s == 0 (see _harvest_once).
            beh = state.get("behaviour")
            if beh == "always":
                raise TimeoutError("never in /history")
            if beh == "late" and timeout_s:
                raise TimeoutError("past the deadline")
            img = solid_png((w, h), (250, 10, 10))
            return {node: [img] for node in include_node_ids}

        def downscale_to_exact_size(image_bytes, width, height):
            im = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize(
                (width, height), Image.LANCZOS)
            buf = io.BytesIO(); im.save(buf, "PNG")
            return buf.getvalue()

        async def get_json(url, timeout=30):
            return {}

        async def post_json(url, body, timeout=30):
            # recorded, because the give-up path must interrupt EXACTLY OUR prompt id and
            # the only proof either way is what actually went over the wire
            state.setdefault("interrupts", []).append((url, body))
            return {}

        cc.upload_to_comfy = upload_to_comfy
        cc.submit_workflow = submit_workflow
        cc.wait_and_fetch = wait_and_fetch
        cc.downscale_to_exact_size = downscale_to_exact_size
        cc.get_json = get_json
        cc.post_json = post_json

        ow = types.ModuleType("stackd.imagegen.openwebui_client")

        async def save_image(image_bytes, filename, api_key):
            state["saved"] = image_bytes
            state["saved_name"] = filename
            state["saved_key"] = api_key
            return "http://owu/x/" + filename

        ow.save_image = save_image

        async def post_chat_message(chat_id, content, api_key):
            # Records WHAT would land in the conversation. A state["chatpost_fail"]
            # (or behaviour "chatpost_fail") makes it report failure the way a dead
            # OWU would — the render must survive it.
            state.setdefault("posts", []).append((chat_id, content, api_key))
            return not (state.get("chatpost_fail") or
                        state.get("behaviour") == "chatpost_fail")

        ow.post_chat_message = post_chat_message
        return cc, ow

    class FakeRuntime:
        def resolve_user_key(self, email):
            return "key-for-" + (email or "")

    def drive(mask_bytes, spec=None, behaviour=None):
        """Run the real comfy_render with fakes; returns (state, artifact_bytes, job)."""
        state = {}
        if behaviour:
            state["behaviour"] = behaviour
        # The PACKAGE, never the submodules: naming them in a fromlist would import the real
        # httpx-backed clients, which this suite must not need. Plain package import is
        # already satisfied via workflows.
        pkg = sys.modules.get("stackd.imagegen") or __import__("stackd.imagegen")
        cc, ow = build_fakes(state)
        saved = {k: sys.modules.get(k) for k in
                 ("stackd.imagegen.comfyui_client", "stackd.imagegen.openwebui_client")}
        saved_attr = (getattr(pkg, "comfyui_client", "$"), getattr(pkg, "openwebui_client", "$"))
        sys.modules["stackd.imagegen.comfyui_client"] = cc
        sys.modules["stackd.imagegen.openwebui_client"] = ow
        pkg.comfyui_client, pkg.openwebui_client = cc, ow
        real_runtime, real_base = E._runtime, E.comfyui_base
        E._runtime = lambda: FakeRuntime()
        E.comfyui_base = lambda: ("http://comfy:8188", "")
        job = {"email": "a@b.c", "spec": spec or {}, "working_w": CANVAS[0],
               "working_h": CANVAS[1]}
        try:
            b64, _type = E.comfy_render(job, solid_png(PHOTO, (10, 200, 10)), mask_bytes,
                                       on_prompt_id=lambda pid, b: state.setdefault(
                                           "prompt_id", pid))
            art = base64.b64decode(b64) if b64 else None
        except Exception as e:  # noqa: BLE001 — the give-up path is a RESULT under test
            state["raised"] = e
            art = None
        finally:
            E._runtime, E.comfyui_base = real_runtime, real_base
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            pkg.comfyui_client, pkg.openwebui_client = saved_attr
        return state, art, job

    def _with_short_grace(fn):
        """Shrink the post-deadline grace window for the give-up test only.

        Without this the suite sleeps the real RENDER_GRACE_S (45 s), which is exactly why
        nobody runs slow suites. The constant is still exercised — the code under test reads
        it, this only sets it low for one case.
        """
        real = E.RENDER_GRACE_S
        E.RENDER_GRACE_S = 0.2
        try:
            return fn()
        finally:
            E.RENDER_GRACE_S = real


    from stackd.imagegen import workflows as WF

    # ---------------- the crop path -------------------------------------------------
    small = mask_png(CANVAS, (120, 100, 210, 190))
    plan = M.plan_crop(small)
    check("render seam: a small selection produces a crop plan", plan is not None)
    if plan:
        state, art, job = drive(small)
        up_w, up_h = M.image_size(state["uploads"][0])
        # The OLD pin here read `(up_w, up_h) == plan["size"]` — it PINS the ratchet defect:
        # the plan's size is CANVAS space, so a 4x photo crop rendered at a quarter of the
        # detail it held and came back soft through paste_back's upscale. The graph is now
        # given the use-time SOURCE-MAPPED budget; the plan stays the FLOOR, the ceiling
        # the cap, and the latent grid binds both.
        budget = M.render_budget(plan, PHOTO)
        check("render seam: the graph is GIVEN the crop at its source-mapped budget",
              (up_w, up_h) == tuple(budget) and up_w % G_ == 0 and up_h % G_ == 0
              and up_w >= plan["size"][0] and up_h >= plan["size"][1],
              "uploaded %dx%d budget %s plan %s" % (up_w, up_h, budget, plan["size"]))
        check("render seam: a 4x photo SPENDS the ceiling on the crop (the ratchet is shut)",
              max(up_w, up_h) > max(plan["size"]) and max(up_w, up_h) == M.RENDER_MAX_SIDE,
              "uploaded %dx%d, plan %s, canvas %dx%d" % (up_w, up_h, plan["size"], CANVAS[0], CANVAS[1]))
        msk_w, msk_h = M.image_size(state["uploads"][1])
        check("render seam: the mask is cropped to the SAME geometry as the source",
              (msk_w, msk_h) == (up_w, up_h), "mask %dx%d vs source %dx%d" % (msk_w, msk_h, up_w, up_h))
        # The PrimitiveInt front-ends store under "value" (set_node resolves the key from
        # the class_type), so read it the way the graph actually holds it -- and assert the
        # FRAME size is absent, which is what makes "patched at the crop, not the frame" a
        # real claim rather than a tautology about whichever key happens to exist.
        wnode = state["graph"][WF.MASK_WIDTH_NODE]["inputs"]
        hnode = state["graph"][WF.MASK_HEIGHT_NODE]["inputs"]
        gw, gh = wnode["value"], hnode["value"]
        check("render seam: the graph's width/height nodes are the crop size, not the frame",
              (gw, gh) == (up_w, up_h), "graph %sx%s uploaded %dx%d" % (gw, gh, up_w, up_h))
        check("render seam: the frame size never leaks into the graph (no silent full-frame render)",
              CANVAS[0] not in wnode.values() and CANVAS[1] not in hnode.values()
              if plan["size"] != CANVAS else True,
              "w=%s h=%s frame=%s" % (wnode, hnode, CANVAS))
        check("render seam: nothing oversized reaches the process",
              max(up_w, up_h) <= M.RENDER_MAX_SIDE, "%dx%d" % (up_w, up_h))
        check("render seam: prompt id is recorded for cancel",
              state.get("prompt_id") == "pid-seam-1", repr(state.get("prompt_id")))
        # The deadline actually handed to ComfyUI, measured rather than grepped: this is the
        # check that would go red if someone re-merged the toolbox onto imagegen's TIMEOUT_S.
        check("render seam: the render is given the toolbox's OWN deadline",
              state.get("timeout_s") == E.RENDER_TIMEOUT_S,
              "handed %r, toolbox default %r" % (state.get("timeout_s"), E.RENDER_TIMEOUT_S))
        check("render seam: the seam reports progress while waiting",
              "_progress_json" in job and json.loads(job["_progress_json"])["elapsed_s"] > 0,
              repr(job.get("_progress_json"))[:110])
        check("render seam: the artifact saved to OWU is the one returned",
              state.get("saved") == art)
        # registration: the model's crop must land where the plan says, on the photo.
        # Guarded on size deliberately: main() prints FAIL lines only after EVERY test has
        # run, so probing pixels on a wrong-sized artifact would raise IndexError and hide
        # every named failure that had already failed -- the suite aborts with a traceback
        # and reports nothing about WHAT broke.
        out_w, out_h = M.image_size(art)
        right_size = (out_w, out_h) == PHOTO
        check("render seam: the ARTIFACT comes back at the photo's resolution (anti-ratchet)",
              right_size, "%dx%d from a %dx%d render" % (out_w, out_h, up_w, up_h))
        if right_size:
            px = Image.open(io.BytesIO(art)).convert("RGB").load()
            bx0, by0, bx1, by1 = plan["box"]
            fx, fy = PHOTO[0] / float(CANVAS[0]), PHOTO[1] / float(CANVAS[1])
            cx, cy = int((bx0 + bx1) / 2 * fx), int((by0 + by1) / 2 * fy)
            check("render seam: the model output lands inside the planned box",
                  px[cx, cy] == (250, 10, 10), "centre %s = %s" % ((cx, cy), px[cx, cy]))
            check("render seam: everything outside the box stays the user's photo",
                  px[4, 4] == (10, 200, 10) and px[PHOTO[0] - 5, PHOTO[1] - 5] == (10, 200, 10),
                  "corner=%s %s" % (px[4, 4], px[PHOTO[0] - 5, PHOTO[1] - 5]))
            # THE DOG GUARD (live: a 1% paint on a dog came back with the fur beside it
            # repainted). The ring INSIDE the box — past the paint, past the 12px seam
            # fade — is model-output-adjacent only through the server's knob pass, which
            # fits its color_match affine to the WHOLE box. With knobs at the shipped
            # defaults and NO gate, these pixels re-grade (measured +34 mean-abs live);
            # gated, the photo must survive them BYTE-EXACT. Deep-in-the-ring sampling is
            # what keeps this honest: at the box border the seam fade already zeroes the
            # paste, so a border probe would pass even un-gated.
            _stR, _aR, _jR = drive(small, spec={"color_match": 0.9,
                                                "preserve_detail": 0.35})
            if _aR is not None and M.image_size(_aR) == PHOTO:
                pxR = Image.open(io.BytesIO(_aR)).convert("RGB").load()
                sb = M._scale_box(plan["box"], plan["frame"], PHOTO)
                probe_y = int((by0 + by1) / 2 * fy)
                ringL = pxR[sb[0] + 30, probe_y]      # 30 photo-px inside the box edge
                ringR = pxR[sb[2] - 30, probe_y]
                # (the paint spans canvas x 120..210 of a box starting ~96: +30 lands
                # between box edge and paint edge at 4x scale — ring, not paint, not fade)
                check("render seam: the RING inside the box, outside the paint, stays the "
                      "user's photo even with colour-match/detail knobs ON (the dog)",
                      ringL == (10, 200, 10) and ringR == (10, 200, 10),
                      "ringL=%s ringR=%s box=%s" % (ringL, ringR, sb))
            else:
                check("render seam: the RING inside the box, outside the paint, stays the "
                      "user's photo even with colour-match/detail knobs ON (the dog)",
                      False, "ring artefact not probeable (size %s)"
                      % (M.image_size(_aR) if _aR else None,))
        else:
            check("render seam: the model output lands inside the planned box", False,
                  "skipped: artifact is %dx%d, not %s -- cannot probe pixels"
                  % (out_w, out_h, PHOTO))
            check("render seam: everything outside the box stays the user's photo", False,
                  "skipped: artifact is %dx%d, not %s" % (out_w, out_h, PHOTO))
        prov = json.loads(job["_crop_json"])
        check("render seam: provenance says this was a crop, and records the geometry",
              prov["cropped"] is True and prov["composited"] is True
              and prov["rendered_at"] == [up_w, up_h] and prov["artifact"] == list(PHOTO),
              repr(prov)[:150])

    # ---------------- the full-frame path -------------------------------------------
    full = mask_png(CANVAS, (2, 2, CANVAS[0] - 2, CANVAS[1] - 2))
    check("render seam: a whole-frame selection plans no crop", M.plan_crop(full) is None)
    st2, art2, job2 = drive(full)
    up2 = M.image_size(st2["uploads"][0])
    check("render seam: the full-frame path uploads the working size",
          up2 == CANVAS, "%s vs %s" % (up2, CANVAS))
    out2 = M.image_size(art2)
    check("render seam: a full-frame artifact stays at the working size (no needless upscale)",
          out2 == CANVAS, "%s" % (out2,))
    # THE WHOLE-PHOTO REGRADE GUARD (the live sailboat: paint the sail, the sky blew out).
    # This selection stops 2 px short of every border, so the outermost ring is UNPAINTED
    # — pasting the model's (red) output over it, with the compositing knobs' whole-frame
    # histogram ops riding along, is exactly the shipped defect. Gated by the selection,
    # the ring must come back as the user's ORIGINAL (green) and the painted interior as
    # the model's (red). This check fails loudly (4 checks, in fact) if the paste ever
    # goes opaque at full frame again — the version of this pin that predates the fix
    # asserted red corners here and thereby PINNED THE BUG.
    px2 = Image.open(io.BytesIO(art2)).convert("RGB")
    corners = [px2.getpixel(p) for p in [(0, 0), (out2[0] - 1, 0), (0, out2[1] - 1),
                                         (out2[0] - 1, out2[1] - 1)]]
    centre = px2.getpixel((out2[0] // 2, out2[1] // 2))
    check("render seam: a full-frame render leaves UNPAINTED pixels at the border alone "
          "(selection-gated paste — the sailboat regrade)",
          all(c == (10, 200, 10) for c in corners) and centre == (250, 10, 10),
          "corners=%s centre=%s" % (corners, centre))
    # The halo mutant still dies: with the paint reaching the edges, EVERY pixel —
    # including the corners — must be the model's output; a stray border fade at full
    # frame would keep a green ring around a wholly painted image. (The blur inside the
    # gate is ~2px; at the extreme corner a hard-edged 0..W selection keeps it model-red.)
    edge = mask_png(CANVAS, (0, 0, CANVAS[0], CANVAS[1]))
    _st3, art3, _job3 = drive(edge)
    px3 = Image.open(io.BytesIO(art3)).convert("RGB")
    flat = px3.resize((1, 1)).getpixel((0, 0))
    corners3 = [px3.getpixel(p) for p in [(0, 0), (out2[0] - 1, 0), (0, out2[1] - 1),
                                          (out2[0] - 1, out2[1] - 1)]]
    check("render seam: an edge-to-edge selection still pastes to the very edge "
          "(no full-frame border fade / halo mutant)",
          all(c == (250, 10, 10) for c in corners3) and flat == (250, 10, 10),
          "corners=%s mean=%s" % (corners3, flat))
    prov2 = json.loads(job2["_crop_json"])
    check("render seam: a full-frame render records cropped=false",
          prov2["cropped"] is False, repr(prov2)[:120])
    # ---- chat hand-back through the REAL render seam (GPU-free) --------------------
    # spec["chat_id"] set (the claim h_job_create stamped) => exactly one post to the
    # OWU chat carrying the saved-file URL; absent => no post at all. A failed post
    # must never cost the render (artifact still returned, note recorded).
    st_p, art_p, job_p = drive(full, spec={"chat_id": "CH-HAND"})
    posts = st_p.get("posts") or []
    check("render seam: a chat-bound job posts the finished render back to the chat",
          art_p is not None and len(posts) == 1 and posts[0][0] == "CH-HAND"
          and "http://owu/x/toolbox-edit.png" in posts[0][1] and posts[0][2] == "key-for-a@b.c",
          repr([(c, x[:40]) for c, x, _k in posts])[:160])
    check("render seam: the hand-back outcome is recorded on the job for the log",
          job_p.get("_chat_post") == "posted", repr(job_p.get("_chat_post")))
    check("render seam: the hand-back state rides the persisted provenance (the editor's "
          "status line reads crop.chat_post — the 'it just sits in the window' fix)",
          json.loads(job_p["_crop_json"]).get("chat_post") == "posted",
          repr(job_p.get("_crop_json"))[:160])
    st_n, art_n, job_n = drive(full)
    check("render seam: a job with no chat binding posts NOTHING to any chat",
          art_n is not None and not st_n.get("posts")
          and "_chat_post" not in job_n, repr(st_n.get("posts")))
    st_f, art_f, job_f = drive(full, spec={"chat_id": "CH-HAND"}, behaviour="chatpost_fail")
    check("render seam: a failed chat post is a NOTE, never a lost render "
          "(the artifact still comes back)",
          art_f is not None and job_f.get("_chat_post") == "failed"
          and len(st_f.get("posts") or []) == 1, repr(job_f.get("_chat_post")))

    # ---- post_chat_message's SHAPE, against OWU's OWN tree semantics ---------------
    # Live defect round 2 of the hand-back: the POST returned 200, the row landed in
    # webui.db, and the chat showed NOTHING — this build renders the thread as
    # get_message_list(messages_map, history.currentId), a walk of camelCase
    # `parentId` links. Round 1 appended a snake_case `parent_id: None` detached
    # root: saved, reconciled, UNREACHABLE from the tip — invisible even on reload.
    # So the fake below mimics the deployed 0.11.x contract exactly (camelCase
    # fixture read from the live DB) and the walk re-implements misc.py:182
    # server-side. A hand-back that cannot be walked from the tip is NOT posted.
    _FIX = {"chat": {"history": {"currentId": "U2", "messages": {
        "U1": {"id": "U1", "role": "user", "content": "make me a photo",
               "parentId": None, "childrenIds": ["A1"]},
        "A1": {"id": "A1", "role": "assistant", "content": "here you go",
               "parentId": "U1", "childrenIds": ["U2"], "model": "TakacsAI-med",
               "done": True},
        "U2": {"id": "U2", "role": "user", "content": "let me retouch",
               "parentId": "A1", "childrenIds": []},
    }}, "messages": [{"id": "U1", "role": "user", "content": "make me a photo"}]}}

    class _Resp:
        def __init__(self, status=200, body=None): self.status_code, self._b = status, body or {}
        def json(self): return self._b

    class _FakeClient:
        posts, posts_status = [], 200
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            if not url.endswith("/CH-OK"):
                return _Resp(404)
            import copy
            return _Resp(200, copy.deepcopy(_FIX))
        async def post(self, url, json=None, headers=None):
            type(self).posts.append(json)
            return _Resp(type(self).posts_status)

    # Load the REAL httpx-backed client FROM SOURCE under a temp name — drive() left
    # its no-httpx stub cached under the canonical name, so an ordinary import (even
    # after a pop) would hand back the stub. Module level imports are network-free,
    # so this is safe; the base url is pointed at an unroutable port as a
    # belt-and-braces guarantee this block can never POST to the live OWU even if
    # the client stub ever stops covering a path.
    import importlib.util as _ilu
    import types as _types
    import stackd.imagegen.config as _igc
    _spec = _ilu.spec_from_file_location(
        "owu_client_shape_probe",
        str(pathlib.Path(__file__).resolve().parent.parent
            / "stackd" / "imagegen" / "openwebui_client.py"))
    _owu_real = _ilu.module_from_spec(_spec)
    # The host suite runs WITHOUT httpx (by design — the fake-registry contract).
    # The from-source load needs the name to exist, and it binds `import httpx` at
    # module level; the stub carries exactly what this module's functions touch.
    _httpx_stub = _types.ModuleType("httpx")
    _httpx_stub.AsyncClient = _FakeClient
    _httpx_err = type("HTTPStatusError", (Exception,), {})
    _httpx_stub.HTTPStatusError = _httpx_err
    _saved_httpx = sys.modules.get("httpx")
    sys.modules["httpx"] = _httpx_stub
    try:
        _spec.loader.exec_module(_owu_real)
    finally:
        if _saved_httpx is not None:
            sys.modules["httpx"] = _saved_httpx
        else:
            sys.modules.pop("httpx", None)   # the host runs without httpx — restore that
    _real_ac = _owu_real.httpx.AsyncClient
    _real_base = _igc.OPENWEBUI_BASE_URL
    _igc.OPENWEBUI_BASE_URL = "http://127.0.0.1:1"
    _owu_real.httpx.AsyncClient = _FakeClient
    try:
        import asyncio as _aio

        def _walk(hms, tip):        # open_webui/utils/misc.py get_message_list, verbatim
            out, seen, cur = [], set(), tip
            m = hms.get(cur)
            while m and cur not in seen:
                seen.add(cur); out.append(m)
                cur = m.get("parentId"); m = hms.get(cur) if cur else None
            out.reverse(); return out

        _FakeClient.posts.clear()
        ok = _aio.run(_owu_real.post_chat_message("CH-OK", "Toolbox render — ![x](/f/c)", "k"))
        posted = _FakeClient.posts[0]["chat"] if _FakeClient.posts else {}
        hm = (posted.get("history") or {}).get("messages") or {}
        new = [m for m in hm.values() if m.get("model") == "Comfy Toolbox"]
        tip = (posted.get("history") or {}).get("currentId")
        walk = _walk(hm, tip) if new else []
        check("post_chat_message: True on a 200 POST (the honest happy path)", ok is True)
        check("post_chat_message: the append is REACHABLE by OWU's own tip-walk "
              "(the invisible-hand-back guard)",
              len(new) == 1 and new[0]["id"] == tip
              and [w.get("role") for w in walk] == ["user", "assistant", "user", "assistant"],
              repr([w.get("role") for w in walk]))
        check("post_chat_message: parent/children link BOTH ways — the tip's children "
              "list must name the append, or the SPA's branch UI never shows it",
              len(new) == 1 and new[0].get("parentId") == "U2"
              and new[0]["id"] in hm["U2"].get("childrenIds", []),
              repr(new[0].get("parentId") if new else None))
        check("post_chat_message: the legacy top-level list is mirrored (doc keeps both shapes)",
              isinstance(posted.get("messages"), list)
              and posted["messages"][-1].get("model") == "Comfy Toolbox")
        _FakeClient.posts.clear()
        ok404 = _aio.run(_owu_real.post_chat_message("CH-MISSING", "x", "k"))
        check("post_chat_message: an unknown chat is False, not a raise", ok404 is False)
        _FakeClient.posts.clear(); _FakeClient.posts_status = 500
        ok500 = _aio.run(_owu_real.post_chat_message("CH-OK", "x", "k"))
        _FakeClient.posts_status = 200
        check("post_chat_message: a 5xx POST is False — _chat_post turns that into the "
              "editor's honest 'did NOT go through' line", ok500 is False)
        # broken tip (not in the map): degrade to a fresh ROOT the way the SPA's first
        # message does — and STILL be the tip, so it is visible.
        _FakeClient.posts.clear()
        _saved = _FIX["chat"]["history"]["currentId"]
        _FIX["chat"]["history"]["currentId"] = "GHOST"
        _aio.run(_owu_real.post_chat_message("CH-OK", "x", "k"))
        _FIX["chat"]["history"]["currentId"] = _saved
        posted_b = _FakeClient.posts[0]["chat"]
        hmb = posted_b["history"]["messages"]
        newb = [m for m in hmb.values() if m.get("model") == "Comfy Toolbox"][0]
        check("post_chat_message: an unresolvable tip degrades to a root that is STILL "
              "the currentId (visible), never a detached orphan (invisible)",
              newb["parentId"] is None
              and posted_b["history"]["currentId"] == newb["id"]
              and _walk(hmb, newb["id"])[-1]["id"] == newb["id"])
    finally:
        _owu_real.httpx.AsyncClient = _real_ac
        _igc.OPENWEBUI_BASE_URL = _real_base
    # ---- the give-up path, END TO END through comfy_render ------------------------
    # test_render_timeout exercises the helper functions; these prove the RENDER SEAM wires
    # them, which is where two mutants slipped through GREEN. The order is the feature: if
    # the seam interrupts before harvesting, a render that lands one poll late is destroyed.
    st_late, art_late, job_late = drive(small, behaviour="late")
    check("render seam: a render that lands during the grace window is RETURNED, not lost",
          art_late is not None, repr(st_late.get("raised"))[:90])
    check("render seam: a render we harvested was never interrupted "
          "(interrupting first is the bug that lost the 616s render)",
          not st_late.get("interrupts"), repr(st_late.get("interrupts"))[:110])
    check("render seam: the harvest went through the toolbox's own deadline path",
          (st_late.get("timeout_calls") or [None])[0] == E.RENDER_TIMEOUT_S
          and 0 in (st_late.get("timeout_calls") or []),
          repr(st_late.get("timeout_calls"))[:90])

    st_gone, art_gone, job_gone = _with_short_grace(
        lambda: drive(small, behaviour="always"))
    raised = st_gone.get("raised")
    check("render seam: a render that never lands surfaces an honest timeout",
          art_gone is None and isinstance(raised, TimeoutError), repr(raised)[:90])
    check("render seam: on genuine give-up we interrupt ComfyUI exactly once",
          len(st_gone.get("interrupts") or []) == 1,
          repr(st_gone.get("interrupts"))[:110])
    ids = [b.get("prompt_id") for _u, b in (st_gone.get("interrupts") or [])]
    check("render seam: the interrupt names OUR prompt id, so a concurrent render survives",
          ids == [st_gone.get("prompt_id")] and ids != [None],
          "interrupted=%s ours=%r" % (ids, st_gone.get("prompt_id")))
    check("render seam: the failed job still records progress for the UI (elapsed is truth)",
          "_progress_json" in job_gone, repr(job_gone.get("_progress_json"))[:80])




def test_render_timeout():
    """Task 3: the toolbox owns its render deadline, harvests late finishers, and frees the
    GPU when it gives up.

    Measured reason: the deployment that lost a render ran with TIMEOUT_S=600 injected in
    AI-STACK/docker-compose.yml, and a job needing ~616 s was reported as an error while
    ComfyUI went on finishing an image nobody collected. Two bugs live in that story: one
    shared timeout for two different workloads, and a give-up path that neither waited a
    little longer nor told the GPU to stop.
    """
    import asyncio
    import time as _t
    from stackd.toolbox import engine as E
    from stackd.toolbox import jobs as J
    root = pathlib.Path(__file__).resolve().parent.parent
    eng = (root / "stackd" / "toolbox" / "engine.py").read_text()

    check("toolbox owns a render deadline separate from imagegen's",
          hasattr(E, "RENDER_TIMEOUT_S") and hasattr(E, "RENDER_GRACE_S"))
    check("both are operator-tunable by env, like TOOLBOX_RENDER_MAX_SIDE",
          "TOOLBOX_RENDER_TIMEOUT_S" in eng and "TOOLBOX_RENDER_GRACE_S" in eng)
    try:
        from stackd.imagegen import config as IG
        # Asserted at the seam, not by grepping the text: an earlier version of this check
        # greped for "config.TIMEOUT_S" and was tripped by the COMMENT that explains why we
        # do not use it. Words in prose are not evidence; the number handed to ComfyUI is.
        check("the toolbox deadline is its own number, not imagegen's by inheritance",
              E.render_deadline_s() == E.RENDER_TIMEOUT_S
              and "timeout_s=RENDER_TIMEOUT_S" in eng,
              f"toolbox={E.RENDER_TIMEOUT_S} imagegen={getattr(IG, 'TIMEOUT_S', None)}")
    except ImportError:
        pass

    # ---- /queue -> stage: the pure half, no network, no httpx ----------------------
    pend = [[2, "other", {}, {}, {}], [3, "third", {}, {}, {}]]
    check("stage: our prompt in queue_running reads as running",
          E._stage_from_queue({"queue_running": [[1, "mine", {}, {}, {}]],
                               "queue_pending": pend}, "mine")["stage"] == "running")
    check("stage: queued counts how many prompts are ahead of us",
          E._stage_from_queue({"queue_running": [], "queue_pending": pend}, "third")["ahead"] == 1)
    # THE honesty rule of this feature: /queue can say where a prompt is, never how far
    # through a render it is. Anything not in either list is 'vanished' -- it finished, or
    # never ran -- and must not be dressed up as progress.
    check("stage: a prompt in neither list is 'vanished', never a percentage",
          E._stage_from_queue({"queue_running": [], "queue_pending": pend},
                              "gone")["stage"] == "vanished")
    check("stage: an unreachable ComfyUI yields NO stage rather than a wrong one",
          E.progress_of("http://127.0.0.1:1", "x") == {})
    # ---- the give-up path: wait FIRST, interrupt SECOND ---------------------------
    class FakeClient:
        def __init__(self):
            self.calls, self.interrupts = [], []
            self.missing = True

        async def wait_and_fetch(self, pid, nodes, base, timeout_s=None, on_poll=None):
            self.calls.append(timeout_s)
            if self.missing:
                raise TimeoutError("not in /history")
            return {next(iter(nodes)): [b"PNG"]}

        async def post_json(self, url, body, timeout=10):
            self.interrupts.append(body)

    # The render finished on the far side of the deadline: it must be COLLECTED, and we
    # must never have interrupted it.
    c1 = FakeClient()
    ticks = {"n": 0}

    async def late():
        ticks["n"] += 1
        c1.missing = ticks["n"] >= 2          # lands on the second grace poll
        return await E._harvest_after_deadline(c1, "p1", "http://x", grace=1.0)
    got = asyncio.run(late())
    check("a render that finishes just after the deadline is HARVESTED, not discarded",
          bool(got), repr(got)[:80])
    check("a late render that we harvested was never interrupted", c1.interrupts == [],
          repr(c1.interrupts))

    # Genuinely never lands: grace expires, THEN we interrupt, THEN one last look.
    c2 = FakeClient()

    async def never():
        h = await E._harvest_after_deadline(c2, "p2", "http://x", grace=0.1)
        if not h:
            await E._interrupt_prompt(c2, "p2", "http://x")
            h = await E._harvest_once(c2, "p2", "http://x")
        return h
    out2 = asyncio.run(never())
    check("a render that never lands is not reported as a success", out2 is None)
    check("we INTERRUPT exactly once we truly give up (no GPU burning after we quit)",
          len(c2.interrupts) == 1 and c2.interrupts[0].get("prompt_id") == "p2",
          repr(c2.interrupts))
    check("the interrupt is scoped to OUR prompt id, never the whole queue",
          all(list(i.keys()) == ["prompt_id"] for i in c2.interrupts), repr(c2.interrupts))
    check("grace is spent BEFORE the interrupt (ordering is the feature, not cosmetics)",
          len(c2.calls) > len(c2.interrupts), "polls=%d interrupts=%d"
          % (len(c2.calls), len(c2.interrupts)))

    # ---- progress is offered only to seams that accept it -------------------------
    def old_seam(job, source, mask, *, on_prompt_id=None):
        return None, None

    def new_seam(job, source, mask, *, on_prompt_id=None, on_progress=None):
        return None, None

    def star_seam(job, source, mask, **kw):
        return None, None

    check("a seam without on_progress is still callable (a TypeError here would be the user's failed job)",
          J._accepts_progress(old_seam) is False)
    check("a seam that takes on_progress is handed it", J._accepts_progress(new_seam) is True)
    check("**kwargs seams get progress too", J._accepts_progress(star_seam) is True)

    seen = {}

    def reporting(job, source, mask, *, on_prompt_id=None, on_progress=None):
        on_prompt_id("pid-x", "http://comfy:8188")



        seen["kwarg"] = on_progress is not None
        if on_progress is not None:
            on_progress({"elapsed_s": 12.5, "stage": "running", "ahead": 0,
                         "render_size": [512, 512]})
        return (base64.b64encode(b"X").decode(), "image/png")

    store = J.JobStore(":memory:")
    jq = J.JobQueue(store, render=reporting)
    jq.start()
    jid = store.create(email="a@b.c", kind="retouch", w=512, h=512, spec={}, mask_info={})
    jq.enqueue(jid, source=b"S", mask=b"M")
    end = _t.time() + 3
    while _t.time() < end and store.get(jid)["state"] not in ("done", "error", "cancelled"):
        _t.sleep(0.02)
    row = store.get(jid)
    jq.stop()
    check("the seam was actually handed the on_progress kwarg", seen.get("kwarg") is True)
    check("progress reaches the row, so a reopened page shows the same truth",
          row.get("progress_json") is not None
          and json.loads(row["progress_json"])["stage"] == "running",
          repr(row.get("progress_json"))[:110])
    check("a progress-reporting render still completes", row["state"] == "done", row["state"])











    # ---- the client may not claim more than the server knows ----------------------
    js = (root / "stackd" / "toolbox" / "web" / "toolbox.js").read_text()
    check("progress line is defined AND called (a dead helper is a dead knob)",
          "function progressLine(" in js and "status(progressLine(r, tries))" in js)
    check("the UI never invents a percent-complete for a render",
          "%" not in js.split("function progressLine(")[1].split("function renderBar(")[0]
          or "Math.min(95" in js)
    check("an ETA is labelled as an estimate, never as a fact",
          "est." in js and "no timing history at this size yet" in js)
    check("the bar stays indeterminate without a calibrated ETA",
          "if (p && p.eta_s)" in js and "opacity = '.35'" in js)
    check("the bar is capped short of 100% because it is an estimate",
          "Math.min(95" in js)
    check("the bar is cleared on every terminal path (done, error, timeout, cancel)",
          js.count("clearBar()") >= 3)
    # ...and specifically on the DONE path: a count-only assertion let a mutant that removed
    # just this one call pass GREEN, leaving a bar parked at 95% beside a finished image.
    done_branch = (js.split("if (r.state === 'done' || r.state === 'error') {")[1]
                   .split("status(progressLine")[0])
    check("the bar is cleared inside the done/error branch itself, not merely somewhere",
          "clearBar()" in done_branch, done_branch[:100])
    check("the determinate bar is capped below 100% because it is an estimate",
          "Math.min(95" in js)
    check("stage names come from /queue reality, not from a timer",
          "'generating'" in js and "'queued'" in js and "'finishing'" in js)

    # ---- the poll route serves progress, calibrated or silent --------------------
    store3 = J.JobStore(":memory:")
    tb3 = make_tb(worker=J.JobQueue(store3, render=lambda job, s, m, **kw: (None, None)))
    r3 = post(tb3, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {"seed": 1}},
              token=fresh_token())
    j3, tok3 = r3.payload.get("job_id"), r3.payload.get("token")
    store3.set(j3, state="running", progress_json=json.dumps(
        {"elapsed_s": 20.0, "stage": "running", "ahead": 0, "render_size": [512, 384]}))
    p3 = post(tb3, "/toolbox/jobs/poll", {"job_id": j3}, token=tok3)
    prog3 = p3.payload.get("progress") or {}
    check("poll exposes progress with the true stage and elapsed",
          prog3.get("stage") == "running" and prog3.get("elapsed_s") == 20.0,
          repr(prog3)[:110])
    check("with no timing history at this size the ETA is None, NOT a guess",
          prog3.get("eta_s") is None, repr(prog3.get("eta_s")))
    # seed history at the same working size, then the ETA must appear and be labelled
    now = _t.time()
    # Seed at the job's REAL working size: the create path derives w/h from the fixture
    # photo, so hard-coding 512x384 here tested the wrong geometry and the ETA legitimately
    # stayed None -- a test bug, not a product bug, but the kind that hides a real feature.
    WW, WH = store3.get(j3)["working_w"], store3.get(j3)["working_h"]
    jh = store3.create(email="a@b.c", kind="retouch", w=WW, h=WH, spec={}, mask_info={})
    store3.set(jh, state="done", artifact_b64="eA==", artifact_type="image/png")
    store3.conn.execute("UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                        (now - 40, now, jh))
    p4 = post(tb3, "/toolbox/jobs/poll", {"job_id": j3}, token=tok3)
    prog4 = p4.payload.get("progress") or {}
    check("with history at the same size the ETA is calibrated and labelled",
          prog4.get("eta_s") == 40.0 and prog4.get("eta_basis")
          and prog4.get("eta_samples") == 1, repr(prog4)[:140])
    # ...and history at a DIFFERENT size must not calibrate this one: an ETA borrowed from
    # another geometry is the same false promise wearing a different hat.
    jh2 = store3.create(email="a@b.c", kind="retouch", w=WW + 16, h=WH, spec={}, mask_info={})
    store3.set(jh2, state="done", artifact_b64="eA==", artifact_type="image/png")
    store3.conn.execute("UPDATE jobs SET created_at=?, updated_at=? WHERE id=?",
                        (now - 900, now, jh2))
    check("the ETA is calibrated only from renders at the SAME size",
          sorted(store3.recent_durations(WW, WH)) == [40.0],
          repr(store3.recent_durations(WW, WH))[:80])
    store3.set(j3, state="done", artifact_b64="eA==", artifact_type="image/png")
    p5 = post(tb3, "/toolbox/jobs/poll", {"job_id": j3}, token=tok3)
    check("progress stops being offered once the job is done",
          "progress" not in p5.payload, repr(sorted(p5.payload))[:130])


def test_crop_wiring():
    """The crop/paste pipeline is WIRED, not merely implemented: provenance must survive
    engine -> queue -> row -> poll payload, and the compositing knobs must reach the paste
    pass on BOTH render paths.

    Counterpart to test_crop_pipeline, which tests masks.py in isolation. What this pins is
    the SEAMS: a correct paste_back whose provenance the job never persists is the same class
    of bug as the dead knobs this package has already been bitten by twice.
    """
    import tempfile
    import time as _t
    from stackd.toolbox import jobs as J
    if not M.HAS_PIL:
        check("crop wiring: PIL present", False, "skipped")
        return
    PROV = json.dumps({"cropped": True, "box": [0, 0, 256, 256], "size": [256, 256],
                       "composited": True, "note": ""})

    def settle(store, jid, timeout=3.0):
        end = _t.time() + timeout
        while _t.time() < end:
            row = store.get(jid)
            if row["state"] in ("done", "error", "cancelled"):
                return row
            _t.sleep(0.02)
        return store.get(jid)

    def render_crop(job, source, mask, *, on_prompt_id=None):
        # mirrors engine.comfy_render: the seam sets job["_crop_json"] and the queue has to
        # carry it to the row rather than drop it next to the artifact.
        job["_crop_json"] = PROV
        on_prompt_id("p-crop", "http://comfy:8188")
        return (base64.b64encode(b"PNGDATA").decode(), "image/png")

    store = J.JobStore(":memory:")
    q = J.JobQueue(store, render=render_crop)
    q.start()
    jid = store.create(email="a@b.c", kind="retouch", w=512, h=384,
                       spec={"seed": 3}, mask_info={"coverage_paint": 0.1})
    q.enqueue(jid, source=b"SRC", mask=b"MSK")
    row = settle(store, jid)
    q.stop()
    check("crop wiring: a crop render reaches done", row["state"] == "done", row["state"])
    check("crop wiring: crop_json persists on the row",
          row.get("crop_json") == PROV, repr(row.get("crop_json"))[:90])
    check("crop wiring: the persisted provenance parses and says cropped",
          json.loads(row["crop_json"])["cropped"] is True)

    store2 = J.JobStore(":memory:")
    q2 = J.JobQueue(store2, render=lambda job, s, m, **kw: (
        kw["on_prompt_id"]("p2", "http://x") or (base64.b64encode(b"X").decode(), "image/png")))
    q2.start()
    j2 = store2.create(email="a@b.c", kind="retouch", w=512, h=384, spec={}, mask_info={})
    q2.enqueue(j2, source=b"S", mask=b"M")
    row2 = settle(store2, j2)
    q2.stop()
    check("crop wiring: a render that never cropped stores NULL provenance",
          row2.get("crop_json") is None, repr(row2.get("crop_json"))[:60])

    # The column must be ADDED to a database an older deploy already wrote: CREATE TABLE
    # IF NOT EXISTS does not alter an existing table, so without the migration every
    # already-deployed jobs.db raises on its first crop render.
    with tempfile.TemporaryDirectory() as td:
        path = str(pathlib.Path(td) / "jobs.db")
        s1 = J.JobStore(path)
        s1.create(email="e@f.g", kind="k", w=8, h=8, spec={}, mask_info={})
        s1.conn.close()
        s2 = J.JobStore(path)
        cols = {r[1] for r in s2.conn.execute("PRAGMA table_info(jobs)")}
        check("crop wiring: reopening an existing DB gains crop_json (ALTER, not just CREATE)",
              "crop_json" in cols, sorted(cols))
        s2.conn.close()

    # ---- feather: the full-frame paste must not leave a halo ---------------------
    # ---- the poll payload exposes provenance, and omits it when there is none ----
    tb = make_tb(worker=J.JobQueue(J.JobStore(":memory:"),
                                  render=lambda job, s, m, **kw: (None, None)))
    r = post(tb, "/toolbox/jobs", {"mask_png": MASK_B64, "spec": {"seed": 1}},
             token=fresh_token())
    job_id, job_tok = r.payload.get("job_id"), r.payload.get("token")
    worker = getattr(tb, "_worker", None)
    check("poll provenance: a worker is mounted for this fixture", worker is not None,
          "job create returned %r" % (r.payload or {}) if not worker else "")
    if worker is not None and job_id and job_tok:
        worker.store.set(job_id, state="done",
                         artifact_b64=base64.b64encode(b"Z").decode(),
                         artifact_type="image/png", crop_json=PROV)
        pr = post(tb, "/toolbox/jobs/poll", {"job_id": job_id}, token=job_tok)
        crop = pr.payload.get("crop")
        check("poll returns the crop provenance as an object, not a raw string",
              isinstance(crop, dict) and crop.get("cropped") is True, repr(crop)[:90])
        worker.store.set(job_id, crop_json=None)
        pr2 = post(tb, "/toolbox/jobs/poll", {"job_id": job_id}, token=job_tok)
        check("poll omits crop entirely for a full-frame render (absence, not emptiness)",
              "crop" not in pr2.payload, repr(sorted(pr2.payload))[:130])
        # Corrupt provenance must not take the whole poll down: the artifact is the
        # valuable thing on the row, and a lost note is not worth a 500.
        worker.store.set(job_id, crop_json="{not json")
        pr3 = post(tb, "/toolbox/jobs/poll", {"job_id": job_id}, token=job_tok)
        check("unparseable provenance degrades without losing the poll or the artifact",
              pr3.status == 200 and pr3.payload.get("artifacts"),
              "status=%s keys=%s" % (pr3.status, sorted(pr3.payload or {}))[:110])

    # ---- the panel must no longer grey out what is now wired ----------------------
    root = pathlib.Path(__file__).resolve().parent.parent
    js = (root / "stackd" / "toolbox" / "web" / "toolbox.js").read_text()
    dead_m = re.search(r"var DEAD_KNOBS = \[([^\]]*)\]", js)
    dead = set(re.findall(r"'([^']+)'", dead_m.group(1))) if dead_m else set()
    for knob in ("opacity", "blend_mode", "color_match", "preserve_detail"):
        check("%s is no longer dead-marked (it is wired now)" % knob,
              knob not in dead, str(sorted(dead)))
    for knob in ("strength", "variants"):
        check("%s stays dead-marked (still not wired)" % knob, knob in dead, str(sorted(dead)))
    check("the panel hint no longer claims ONLY prompt/seed/geometry affect the image",
          "only prompt, seed and the mask geometry" not in js)
    # Provenance must REACH THE USER. A helper that exists but is never called is the same
    # failure as a dead knob, so the call site is asserted, not just the definition.
    check("client renders crop provenance (cropNote defined and actually called)",
          "function cropNote(" in js and "cropNote(r.crop)" in js)
    check("client provenance states what was rendered and what it was composited into",
          "crop-rendered" in js and "composited into" in js)
    check("client provenance stays silent for a full-frame render (no false claim)",
          "if (!crop.cropped) return '';" in js)
    check("client flags a failed composite loudly rather than hiding it",
          "compositing FAILED" in js)
    check("client provenance survives an absent crop object",
          "if (!crop || typeof crop !== 'object') return '';" in js)
    # engine must actually pass the knobs through, or the registry edit is a lie: this is
    # the check that makes "removed from DEAD_KNOBS" mean wired, not merely un-dulled.
    eng = (root / "stackd" / "toolbox" / "engine.py").read_text()
    check("engine passes all four compositing knobs to paste_back",
          all(("spec.get(\"%s\")" % k) in eng or ('spec.get(\'%s\')' % k) in eng
              for k in ("opacity", "blend_mode", "color_match", "preserve_detail")))
    check("the crop path is wired into the render seam (crop_for_render + crop_mask + paste_back)",
          all(n in eng for n in ("plan_crop", "crop_for_render", "crop_mask", "paste_back")))



    solid = M._seam_alpha((64, 64), 0)
    check("feather 0 gives a SOLID alpha (no ring of the original survives a full-frame edit)",
          set(solid.tobytes()) == {255}, sorted(set(solid.tobytes()))[:5])
    feathered = M._seam_alpha((64, 64), M.SEAM_FEATHER_PX)
    check("the crop path still feathers its seam",
          min(feathered.tobytes()) == 0 and max(feathered.tobytes()) == 255)







if __name__ == "__main__":
    raise SystemExit(main())

