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
          "data:image/png;base64," in doc)
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
    test_spike_wiring()
    test_graphs()
    test_jobs()
    test_working_size()
    test_crop_pipeline()
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
    check("job create caps what the PHOTO was, in the bytes it enqueues",
          enq_size is not None and max(enq_size) <= CEIL, str(enq_size))
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





if __name__ == "__main__":
    raise SystemExit(main())

