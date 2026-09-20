"""Run the toolbox editor driver in real headless Chrome and report per-assertion results.

    python3 tests/tbtest/run_tbtest.py [--mutate NAME] [--width 420] [--height 620]
    (--mutate: sticky ctlorder softpunch brushfeather kindstr autoslider widetol
     rawwash allmerge nosig selguard twoslider noderive blendblank seldbg
     barwash bar95 polltoken movelost shrinkrad padsniped erasesoft nofold latentpunch bakepad bakefeather erasepaints legacy)

--mutate rebuilds the page with a deliberately broken copy of the shipped JS, so the suite
proves it can actually see the two bugs it claims to have fixed. A green test that cannot
go red on the old code has verified nothing.
"""
import html, json, pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gen
import dbg



def driver():
    d = (HERE / "driver.js").read_text()
    steps = (HERE / "steps_a.js").read_text() + (HERE / "steps_b.js").read_text()
    assert "/*__PART2__*/" in d
    return d.replace("/*__PART2__*/", steps)


# ---- negative controls: reintroduce exactly what each fix removed -----------------------
# A test that cannot go red on the old code has not tested the fix, so each mutant must be
# faithful to the ORIGINAL defect, not merely different from the current code. (An earlier
# "legacy zoom" mutant sized the box to stage.clientWidth and passed 30/30 -- clientWidth
# already excludes the gutter, so it could not overflow: it reproduced nothing.)

# sticky gesture gate: once a pinch has ever happened, every later down() is ignored.
LEGACY_STICKY = lambda s: s.replace(
    "if (pinching || Date.now() - pinchEnd < 400) return;",
    "if (pinching || pinchEnd) return;")

# pre-fix zoom: fitW is sampled from offsetWidth (which INCLUDES the scrollbar's reserved
# space) and the box is then sized in fixed pixels at every zoom, so the photo is wider than
# the stage's content box the moment the vertical bar exists -> horizontal bar.
LEGACY_A = "    if (cw > 0) fitW = cw;"
LEGACY_B = ("      el.box.style.width = (zoom > 1 && fitW)\n"
            "        ? (Math.max(1, Math.round(fitW * zoom)) + 'px') : '100%';")


def LEGACY_JS(s):
    # chained replaces are a trap: if the second pattern stops matching, the mutant quietly
    # becomes a partial (and therefore misleading) reproduction. Require both.
    assert LEGACY_A in s and LEGACY_B in s, "legacy zoom patterns missing"
    return s.replace(LEGACY_A, "    if (cw > 0) fitW = el.stage.offsetWidth;").replace(
        LEGACY_B,
        "      el.box.style.width = Math.max(1, Math.round(fitW * (zoom > 1 ? zoom : 1))) + 'px';")


# the gutter reservation is what keeps the two bars from chasing each other
LEGACY_CSS = lambda d: d.replace("overflow: auto; scrollbar-gutter: stable; min-height: 120px;",
                                 "overflow: auto; min-height: 120px;")

# pre-fix slider: .value assigned before min/max/step, so fractional defaults snap to whole
# numbers (wash 0.45 -> 0: the invisible-mask bug the user reported as "shouldn't have to hit
# Preview to see the real mask").
LEGACY_CTL = lambda s: s.replace(
    "    for (var k in (attrs || {})) { if (Object.prototype.hasOwnProperty.call(attrs, k)) i.setAttribute(k, attrs[k]); }\n"
    "    i.value = value;",
    "    i.value = value;\n"
    "    for (var k in (attrs || {})) { if (Object.prototype.hasOwnProperty.call(attrs, k)) i.setAttribute(k, attrs[k]); }")

# the removed soft-edge punch, restored verbatim: erosion applied to the LIVE mask once
# per (every-other) point of the same stroke, so overlapping discs compound and the brush
# carves holes in its own trail. If the self-overlap assertions stay green with this in,
# they are not testing the thing they claim to test.
OLD_PUNCH_AFTER = ("      m.drawImage(sc, bx, by, bw, bh, bx, by, bw, bh);")
OLD_PUNCH = OLD_PUNCH_AFTER + """
      m.globalCompositeOperation = 'destination-out';
      for (i = 0; i < n; i += 2) {
        var _g = m.createRadialGradient(pts[i].x, pts[i].y, r * hard, pts[i].x, pts[i].y, r);
        _g.addColorStop(0, 'rgba(0,0,0,0)');
        _g.addColorStop(1, 'rgba(0,0,0,' + (1 - hard) + ')');
        m.fillStyle = _g; fillDisc(m, pts[i].x, pts[i].y, r);
      }"""


def LEGACY_PUNCH(s):
    assert OLD_PUNCH_AFTER in s, "freehand composite anchor moved"
    return s.replace(OLD_PUNCH_AFTER, OLD_PUNCH, 1)


# ---- per-object mask params -----------------------------------------------------------
# Pre-refactor semantics: ONE flattened mask, so the global feather/grow/shrink sliders hit
# every object indiscriminately -- including brush strokes, whose edge hardness already
# chose. Blurring a binarized mask does not soften it, it erodes it (ComfyUI round()s the
# mask at VAEEncodeForInpaint, the stack's own "Bug 1"), which is why the brush region
# shrank 17881 -> 15429 px as feather rose. Faithful: this is the old single-path behaviour,
# not an arbitrary perturbation of the new one.
def LEGACY_BRUSHFEATHER(s):
    # Post-region-model a brand-new blob's geometry comes from the zero-ancestor branch
    # of rebuildRegions; stamp()'s fields no longer reach the wire. Restoring the pre-fix
    # behaviour = that branch hands EVERY new blob (brush included) the global sliders,
    # which KIND_RULES exists to refuse for handwork (the brush-shrink bug that started
    # the rule). If the brush-edge assertions stay green with this in, they are decor.
    a = "        edge = kind === 'brush' ? 0 : val('tb_edge', 0);\n        feather = kind === 'brush' ? 0 : val('tb_feather', 8);"
    assert a in s, 'brush-edge branch anchor moved'
    return s.replace(a,
        "        edge = val('tb_edge', 0);\n        feather = val('tb_feather', 8);", 1)

# The exact bug the layered export actually shipped with: flush() read .kind off curKey,
# which is the opaque comparison STRING, so every layer crossed the wire with kind
# undefined and the server fell back to its strictest rule (threshold + feather allowed)
# for brush strokes -- silently re-introducing the erosion bug through the back door.
LEGACY_KINDSTR = lambda s: s.replace(
    "                 kind: curParams.kind, edge: curParams.edge,",
    "                 kind: curKey.kind, edge: curParams.edge,", 1)


# The smart-select guard-order bug: objBBox tested `!o.pts` BEFORE the mode==='load' case, so
# a load stroke (no pts) returned null and was invisible to hitTest -- the auto object could
# never be selected, so its grow/shrink/feather inspector never opened. Reproduce by putting
# the pts guard first (faithful to the original defect).
# The grab-shadowing class: a wide near-miss tolerance makes a tap in the GAP between
# two objects grab a stranger (the pre-region hitTest measured BBOXES; the shipped
# pre-fix formula also divided px by scale instead of multiplying, widening the window
# to ~23 natural px at the suite's 0.36 px/css zoom). If the channel/hole assertions
# stay green with this in, they are not testing pixel-exact hit-testing.
def LEGACY_WIDETOL(s):
    a = "      var tol = Math.max(2, Math.round(8 * scale()));"
    assert a in s, 'hitTest tolerance anchor moved'
    return s.replace(a, "      var tol = Math.max(8, Math.round(24 * scale()));", 1)


# The bug the USER reported (#1): only part of the objects keep their morphed display,
# the rest snap back to raw geometry. Reproduced by composing every region from its RAW
# ship copy -- the parameters stay correct on the object, so a state-probe alone could
# NEVER see it: exactly why the live test is pixel-gated on the composited view.
def LEGACY_RAWWASH(s):
    a = "      var disp = regionDispCanvas(r), dg = r._dg;"
    assert a in s, 'compose display-canvas anchor moved'
    return s.replace(a,
        "      var _d = regionDispCanvas(r); var dg = _d && r._dg; var disp = regionRawCanvas(r, false);", 1)


# Contiguity decided by anything other than connected pixels is the pre-refactor model:
# one entry = one object whatever the geometry, so merge/split never fire.
def LEGACY_ALLMERGE(s):
    a = "    order.sort(function (p, q) { return p - q; });"
    assert a in s, 'order.sort anchor moved'
    return s.replace(a, a + " order = order.slice(0, 1);", 1)


# The grow/shrink/feather-does-nothing bug: the toolbar edge/feather sliders fed only the
# global mask_expand/feather, which the LAYERED server path ignores. Reproduce by making
# the region-routing guard always false, so the sliders behave exactly as pre-fix.

def LEGACY_AUTOSLIDER(s):
    """The inspector rows go dead: dragging edge/feather changes no object (the numbers
    reach the wire as nothing). The toolbar-routing code this mutant used to neuter no
    longer exists - the inspector is now the one and only editor, so THAT is the kill."""
    g = "      setObjParam(key, parseFloat(inp.value) || 0);"
    assert s.count(g) == 1, 'ipair handler anchor moved'
    s = s.replace(g, "      // MUTANT: the row edits nothing", 1)
    h = "      setObjParam('edge', v);"
    assert s.count(h) == 1, 'edgePair handler anchor moved'
    return s.replace(h, "      void v;   // MUTANT: the row edits nothing", 1)

MUTANTS = {"sticky": LEGACY_STICKY, "ctlorder": LEGACY_CTL, "softpunch": LEGACY_PUNCH,
           "brushfeather": LEGACY_BRUSHFEATHER, "kindstr": LEGACY_KINDSTR,
           "autoslider": LEGACY_AUTOSLIDER, "widetol": LEGACY_WIDETOL,
           "rawwash": LEGACY_RAWWASH, "allmerge": LEGACY_ALLMERGE}


# The stale-overlay bug, reproduced exactly as it shipped: previewSig() derived from
# strokes.length + the global sliders only, so dragging a placed object (which changes
# neither) let the debounce answer "same request" and keep painting the old overlay.
def LEGACY_NOSIG(s):
    # DIES to the DR __PVHOLD race: an answer minted pre-drag lands mid-drag, and with
    # the object term dropped from previewSig, compose()'s sig check considers the
    # frozen overlay still current and paints it over the finger (magenta gate goes
    # red). The offline test_contract previewSig pin backstops the same term at source.
    a = "            val('tb_brush', 60), val('tb_hardness', 0.55), paramsSig(), objSig()].join('|');"
    assert a in s, "previewSig anchor moved"
    return s.replace(a, "            val('tb_brush', 60), val('tb_hardness', 0.55), paramsSig()].join('|');", 1)


# The unmovable-selection bug, reproduced by RESTORING THE ORDER rather than by disabling
# code: the select-drag branch is lifted out from before the paint guard and dropped back
# in after it, exactly where it was when this shipped. Deleting the branch would be a
# different program, not the old one.
SELECT_BLOCK = """      if (tool === 'select' && selDrag) {
        var sp = toNatural(ev);
        if (selDrag.kind === 'move') {
          translateSel(sp.x - selDrag.last.x, sp.y - selDrag.last.y);
          selDrag.last = sp;
        }
        return;
      }
"""
GUARD = """      // hover (button up): still move the ring so brush/eraser show their footprint
      if (!dragEnabled || !active) {
        if (!jobId && baseC && (tool === 'brush' || tool === 'eraser')) { ringPt = toNatural(ev); drawRing(); }
        return;
      }
      var p = toNatural(ev);
"""


def LEGACY_SELGUARD(s):
    assert SELECT_BLOCK in s and GUARD in s, "select-guard anchors moved"
    s = s.replace(SELECT_BLOCK, "", 1)
    return s.replace(GUARD, GUARD + SELECT_BLOCK, 1)


MUTANTS["nosig"] = LEGACY_NOSIG
MUTANTS["selguard"] = LEGACY_SELGUARD

# The merged control, UN-merged: two independent values again. Faithful because it is the
# literal previous state of the code, where grow and shrink were separate sliders and a user
# setting both to 12 got a morphological CLOSING (notch filled, 9704 -> 10251 px) while
# reading that as "no net change". If the consistency and single-slider assertions stay green
# with this restored, they are not testing the merge.
EXPORT_DERIVE = ("                        grow: (r.edge || 0) > 0 ? (r.edge || 0) : 0,\n"
               "                        shrink: (r.edge || 0) < 0 ? -(r.edge || 0) : 0,")
def LEGACY_TWOSLIDER(s):
    # The un-merged control, faithful to the literal previous state: grow and shrink
    # INDEPENDENT, so an edge>0 layer can also carry a shrink and the server's
    # dilate-then-erode turns "no net change" into a morphological CLOSING.
    assert EXPORT_DERIVE in s, 'export derive anchor moved'
    return s.replace(EXPORT_DERIVE,
        "                        grow: (r.edge || 0) > 0 ? (r.edge || 0) : 0,\n"
        "                        shrink: 12,", 1)


def LEGACY_NO_DERIVE(s):
    # Broken the other way: the signed edge ships while the legacy pair stays zero, so a
    # consumer still reading grow/shrink ignores the user's edge knob entirely.
    assert EXPORT_DERIVE in s, 'export derive anchor moved'
    return s.replace(EXPORT_DERIVE,
        "                        grow: 0,\n                        shrink: 0,", 1)


MUTANTS["twoslider"] = LEGACY_TWOSLIDER
MUTANTS["noderive"] = LEGACY_NO_DERIVE


def SELDBG(s):
    """Event-level trace of the Select tool, injected into the generated page COPY only
    (the shipped file is never touched). Logs every select-mode down()/move() with the
    state that decides selection: current tool, selection index, stroke count, the natural
    point, and what hitTest/objBBox actually returned."""
    a = "      if (t === 'select') {"
    b = "      if (tool === 'select' && selDrag) {"
    assert a in s and b in s, "select injection anchors moved"
    s = s.replace(a, a + """
        try { window.__SEL = (window.__SEL || []); window.__SEL.push('DOWN tool=' + tool +
          ' sel=' + sel + ' n=' + strokes.length + ' p=' + Math.round(p.x) + ',' + Math.round(p.y) +
          ' hit=' + hitTest(p) + ' bbox0=' + JSON.stringify(objBBox(strokes[0] || { pts: [{x:0,y:0}] })) +
          ' pts0=' + ((strokes[0] || {}).pts || []).length +
          ' size0=' + (strokes[0] || {}).size + ' mode0=' + (strokes[0] || {}).mode); }
        catch (e) { window.__SEL = (window.__SEL || []); window.__SEL.push('DOWNERR ' + e); }""", 1)
    s = s.replace(b, b + """
        try { window.__SEL = (window.__SEL || []); window.__SEL.push('MOVE kind=' + selDrag.kind +
          ' sel=' + sel + ' p=' + Math.round(p.x) + ',' + Math.round(p.y)); }
        catch (e) { window.__SEL = (window.__SEL || []); window.__SEL.push('MOVEERR ' + e); }""", 1)
    return s


MUTANTS["seldbg"] = SELDBG

# The unselectable-auto bug, reproduced EXACTLY as shipped: objBBox tested !pts before the
# load case, so a smart-select (load) stroke had no bbox -> hitTest skipped it -> the
# inspector that offers grow/shrink/feather for an auto object could never open. If the
# "selecting an auto object opens an inspector" assertion stays green with this restored,
# it is not testing the fix.


# blank blend options: the defect the user actually reported. A faithful mutant needs
# BOTH halves at once -- the old select() (which read opts[i][1] unconditionally) and
# the old caller that passed bare values. Change either half alone and the options are
# still labelled, so the mutant would reproduce nothing and pass for the wrong reason.
def LEGACY_BLENDBLANK(s):
    s2 = re.sub(r"labelled\('blend', select\('blend', \[.*?\]\]\)\)",
                "labelled('blend', select('blend', [['normal'], ['multiply'], ['screen'], "
                "['overlay'], ['soft-light'], ['hard-light'], ['luminosity'], ['color']]))",
                s, count=1, flags=re.S)
    assert s2 != s, 'blend caller not found -- mutant is not faithful'
    s3 = s2.replace("(opts[i].length > 1 && opts[i][1] != null) ? opts[i][1] : opts[i][0]",
                    "opts[i][1]")
    assert s3 != s2, 'select() fallback not found -- mutant is not faithful'
    return s3


MUTANTS["blendblank"] = LEGACY_BLENDBLANK



# ---- progress-bar mutants: the ladder's three promises, each un-done faithfully ----

# The determinate branch disabled: the bar stays a permanent wash even when a
# calibrated ETA exists (the "we never believe the bar" regression class).
def LEGACY_BARWASH(s):
    a = "    if (p && p.eta_s) {\n      fill.style.opacity = '1';"
    assert a in s, 'renderBar eta anchor moved'
    return s.replace(a, "    if (false) {\n      fill.style.opacity = '1';", 1)


# The terminal clearBar removed: a bar parked at 95% next to a finished image —
# reproduced exactly as the honesty comment in pollJob() describes the defect.
def LEGACY_BAR95(s):
    a = "        clearBar();   // a bar parked at 95%"
    assert a in s, 'pollJob clearBar anchor moved'
    return s.replace(a, "        /* mutant: leave it parked */   // a bar parked at 95%", 1)


# Poll on the LAUNCH token instead of the job token create minted (the historic
# 403-on-poll class: the real server answers this with a token error).
def LEGACY_POLLTOKEN(s):
    a = "    req('/toolbox/jobs/poll', { job_id: id }, tok)"
    assert a in s, 'pollJob req anchor moved'
    return s.replace(a, "    req('/toolbox/jobs/poll', { job_id: id })", 1)


# The wire-contract break the region refactor shipped with (the user-reported "one
# smart select and the entire image is the mask"): layers crossed as bbox CROPS, and
# masks._layer_coverage LANCZOS-stretches any png whose size disagrees — a 10% blob
# inflated to a full-frame overlay and mask. Restores the exact pre-fix line.
def LEGACY_CROPSHIP(s):
    a = "      fc.getContext('2d').drawImage(regionRawCanvas(r, false), r.bbox.x0, r.bbox.y0);\n"
    a += "      var png = fc.toDataURL('image/png').split(',')[1];"
    assert a in s, 'exportLayers full-canvas paste anchor moved'
    return s.replace(a,
        "      var png = regionRawCanvas(r, false).toDataURL('image/png').split(',')[1];", 1)


MUTANTS["cropship"] = LEGACY_CROPSHIP
# The slider-drag thief: touchObject() rebuilt the inspector DOM on every input event,
# and the browser delivers a range input's event stream to the node it grabbed at
# mousedown - the node vanishes and the drag dies (click-to-jump survives; the user
# reported exactly "only clickable to a new value"). Restores the rebuild per edit.
def LEGACY_INSPREDRAW(s):
    a = "    if (el_inspect && el_inspect._sync && el_inspect.offsetParent !== null) el_inspect._sync();"
    assert a in s, 'touchObject in-place sync anchor moved'
    return s.replace(a, "    if (el_inspect) syncInspector();", 1)


# The dead brush knobs, restored FAITHFULLY: both halves at once - the toolbar guard
# excluding brush AND the inspector rows disabled for brush. Either half alone still
# leaves one working route to edit the object, so a one-sided mutant would reproduce
# nothing and pass for the wrong reason.

def LEGACY_BRUSHDEAD(s):
    """The dead brush knobs, restored where they now live: BOTH inspector rows go
    disabled for a brush object (the toolbar half was retired with the panel pair, so
    a one-sided mutant would reproduce nothing)."""
    h = "      setObjParam('edge', v);"
    assert s.count(h) == 1, 'edgePair live handler anchor moved'
    s = s.replace(h,
        "      if (o.kind === 'brush') { inp.disabled = true; return; }" + chr(10) + h, 1)
    f = "      setObjParam(key, parseFloat(inp.value) || 0);"
    assert s.count(f) == 1, 'ipair live handler anchor moved'
    return s.replace(f,
        "      if (obj.kind === 'brush') { inp.disabled = true; return; }" + chr(10) + f, 1)

MUTANTS["inspredraw"] = LEGACY_INSPREDRAW
MUTANTS["brushdead"] = LEGACY_BRUSHDEAD

MUTANTS["barwash"] = LEGACY_BARWASH
MUTANTS["bar95"] = LEGACY_BAR95
MUTANTS["polltoken"] = LEGACY_POLLTOKEN


# MOVE IDENTITY, pre-fix: the baked move/undo/redo strokes carry their params, but the
# rebuild never receives them (noteMoveIdentity neutralised), so a blob whose pixels
# land with no ancestor overlap re-rolls kind/edge/feather from commitHintKind + the
# toolbar sliders. 'Sometimes loses its feathering' = always, once any brush stroke was
# painted earlier in the session (hint=brush) or the blob lands onto a plain neighbour
# (the merge takes the neighbour's feather wholesale).
def LEGACY_MOVELOST(s):
    an = "  function noteMoveIdentity(st, applied) {\n    pendingMove = null;"
    assert an in s, 'noteMoveIdentity anchor moved'
    return s.replace(an,
        "  function noteMoveIdentity(st, applied) {\n    if (1) return;   // MUTANT: identity never reaches the rebuild\n    pendingMove = null;", 1)


# PREVIEW FEATHER TIGHTNESS, pre-fix: every display kernel/blur radius multiplied by
# CAP/grid-side. The shipped defect hid at cap 256 (only phone-sized canvases shrink);
# the harness view is 160x320, so the mutant stands the cap in at 48 — the SAME relative
# shrinkage the user sees at 1024 px (feather 30 drawn as ~4, visibly hard-edged).
def LEGACY_SHRINKRAD(s):
    an = "    if (e) on = (r.kind !== 'shape' ? _discOn : _boxOn)(on, w, h, Math.min(96, Math.max(1, Math.abs(e))), e > 0);"
    assert an in s, 'true-edge anchor moved'
    s = s.replace(an,
        "    var sd = Math.min(1, 48 / Math.max(1, Math.max(w, h)));\n" + an.replace(
            'Math.min(96, Math.max(1, Math.abs(e)))', 'Math.max(1, Math.round(Math.abs(e) * sd))'), 1)
    g2 = "var grown = _discOn(on, w, h, Math.min(96, Math.max(1, f)), true);"
    assert g2 in s, 'true-grow anchor moved'
    s = s.replace(g2, "var grown = _discOn(on, w, h, Math.max(1, Math.round(f * sd)), true);", 1)
    b3 = "bx.filter = 'blur(' + Math.max(0.5, Math.min(96, f)) + 'px)';"
    assert b3 in s, 'true-blur anchor moved'
    s = s.replace(b3, "bx.filter = 'blur(' + Math.max(0.5, f * sd) + 'px)';", 1)
    return s


MUTANTS["movelost"] = LEGACY_MOVELOST
MUTANTS["shrinkrad"] = LEGACY_SHRINKRAD


# CLIENT FEATHER SKIRT — the visible square-corner defect, restored: the display grid
# padded only e+f+2 past the bbox, cutting the blurred skirt on a hard axis-aligned
# line. Measured: disc-vs-Chebyshev grow is INVISIBLE under a sigma-f blur (>=128 reach
# (32,15) vs (34,17) — within jitter), so the CLIP — not the kernel — squared off the
# corners; the _boxOn/_discOn kind split this replaces was faithful-to-server only, and
# no honest gate could tell the difference. The clip could, and does.
def LEGACY_PADSNIP(s):
    an = "    var pad = Math.max(0, effEdge(r)) + 2 * (r.feather || 0) + 4;"
    assert an in s, 'regionGeom pad anchor moved'
    return s.replace(an,
        "    var pad = Math.max(0, effEdge(r)) + (r.feather || 0) + 2;", 1)


MUTANTS["padsniped"] = LEGACY_PADSNIP


# THE HARD CUT SOFTENED: the eraser honours the hardness slider, leaving partial-alpha
# residue whose blob membership then hinges on crossing REGION_ALPHA_MIN - a stair-stepped,
# zoom-dependent cut that the region model can fail to split at all.
def LEGACY_ERASESOFT(s):
    an = "hardness: (t === 'eraser' ? 1 : hard),"
    assert an in s, 'eraser hardness pin moved'
    return s.replace(an, "hardness: hard,", 1)


MUTANTS["erasesoft"] = LEGACY_ERASESOFT


# THE FOLD NEVER HAPPENS, restored: an erase (or a merge) leaves the parent live, so each
# fragment dilates from its own fresh cut edge and the channel heals shut — the reported
# 'two halves bigger than the original and overlapping'. Mutating this one function takes
# both call sites, which is exactly how the shipped bug behaved.
NL = chr(10)
def LEGACY_NOFOLD(s):
    an = '  function foldLiveGeometry(st) {' + NL + '    var bk = bakeSnapshot();'
    assert an in s, 'foldLiveGeometry anchor moved'
    return s.replace(an, '  function foldLiveGeometry(st) {' + NL +
        '    if (1) return false;   // MUTANT: geometry stays live' + NL +
        '    var bk = bakeSnapshot();', 1)


# THE PERSISTENT PUNCH, restored (the first attempt at this fix, reverted after the
# user found the latent gap): the eraser replays against the finished wash AND ships as
# an erase layer, so the channel can never be re-filled - objects move, the punch does
# not. Kills the re-merge gate (a hole through the joined object) and the wire gate.
def LEGACY_LATENTPUNCH(s):
    NL = chr(10)
    an1 = '    if (active) paintStroke(wc, active);   // the in-progress shape, at full liveness'
    an2 = NL.join(['    return out.length ? out : null;', '  }', '',
                   '  function int0(id, dflt) {'])
    assert an1 in s and an2 in s, 'latentpunch anchors moved'
    wash = NL.join([
        '    for (var ei = 0; ei < strokes.length; ei++) {            // MUTANT: permanent punch',
        '      if (strokes[ei] !== active && isUserErase(strokes[ei])) paintStroke(wc, strokes[ei]);',
        '    }', '']) + an1
    ship = NL.join([
        "    for (var si = 0; si < strokes.length; si++) {            // MUTANT: punch on the wire",
        '      var es = strokes[si];',
        '      if (es === active || !isUserErase(es)) continue;',
        "      var ec = document.createElement('canvas'); ec.width = W; ec.height = H;",
        "      paintStroke(ec.getContext('2d'), { mode: es.mode, pts: es.pts, size: es.size,",
        '                                          hardness: es.hardness, erase: false });',
        "      var epng = ec.toDataURL('image/png').split(',')[1];",
        '      if (!epng) continue;',
        "      out.push({ png: epng, kind: 'brush', edge: 0, grow: 0, shrink: 0,",
        '                 feather: 0, erase: true });',
        '    }', '']) + an2
    s = s.replace(an1, wash, 1)
    return s.replace(an2, ship, 1)


# THE BAKE DRIFTS FROM THE DISPLAY: the folded silhouette is built with more slack than
# the kernels give, so the object fattens the moment a cut lands on it. Gate: the outer
# boundary, same row, measured before and after the cut.
def LEGACY_BAKEPAD(s):
    an = '                    on, w, h, Math.min(96, Math.max(1, Math.abs(e))), e > 0);'
    assert an in s, 'bake kernel anchor moved'
    s = s.replace(an,
        '                    on, w, h, Math.min(96, Math.max(1, Math.abs(e) + 4)), e > 0);'
        '   // MUTANT', 1)
    # The crop window caps how far a bake can reach, so a faithful "bake drifts" mutation
    # has to widen it too - otherwise the tight crop clips the drift into invisibility and
    # the mutant survives for a reason that is not a gate.
    an2 = 'var pp = Math.max(0, effEdge(r)) + 1;'
    assert an2 in s, 'bake pad anchor moved'
    return s.replace(an2, 'var pp = Math.max(0, effEdge(r)) + 5;   // MUTANT', 1)


# THE FEATHER GETS BAKED IN: softness stops being a property of the current boundary and
# becomes permanent geometry, which re-feathers on every later edit (a ratcheting skirt).
# The pad grows with it on purpose: anyone who really made this change would size their
# own crop window, so a mutation that left the crop tight would be clipped into harmlessness
# and "survive" for a reason that has nothing to do with the gates.
def LEGACY_BAKEFEATHER(s):
    NL = chr(10)
    an = NL.join([
        "      if (e) on = (r.kind === 'auto' ? _discOn : _boxOn)(",
        '                    on, w, h, Math.min(96, Math.max(1, Math.abs(e))), e > 0);'])
    assert an in s, 'bake morph anchor moved'
    add = NL.join([
        '',
        '      var _f = Math.round(r.feather || 0);',
        '      if (_f > 0) on = _discOn(on, w, h, Math.min(96, Math.max(1, _f)), true);   // MUTANT'])
    s = s.replace(an, an + add, 1)
    an2 = 'var pp = Math.max(0, effEdge(r)) + 1;'
    assert an2 in s, 'bake pad anchor moved'
    return s.replace(an2, 'var pp = Math.max(0, effEdge(r)) + Math.round(r.feather || 0) + 1;', 1)


# THE ERASER PAINTS INSTEAD OF REMOVING: destination-out lost, so a sweep ADDS coverage
# and the object never splits. Gate: the split itself, and the channel.
def LEGACY_ERASEPAINTS(s):
    NL = chr(10)
    an = "    m.globalCompositeOperation = s.erase ? 'destination-out' : 'source-over';" + NL
    assert s.count(an) == 2, 'expected two gCO sites (entry + post-bake), got ' + str(s.count(an))
    # BOTH: the bake branch restores the mode, so mutating only the entry leaves the
    # eraser honest - an ineffective mutant is not a killed gate, it is a fake one.
    i = s.rindex(an)
    return s[:i] + "    m.globalCompositeOperation = 'source-over';   // MUTANT" + NL + s[i + len(an):]


# THE DOUBLE SOFT EDGE, restored: the hardness ramp lives in the raster again AND the
# display early-returns raw ink for brush, so an object feather lands outside the ramp -
# the exact "feather on the brush, then feather on the pixel cloud" the user rejected.
def LEGACY_BRUSHSOFT(s):
    NL = chr(10)
    an = "          d[o + 3] = 255;"
    assert an in s, 'binary alpha write moved'
    s = s.replace(an, "          d[o + 3] = maskA[g];   // MUTANT: ramp lives in the raster", 1)
    an2 = "    var cx = c.getContext('2d');" + NL + "    // No kind is exempt, brush included."
    assert an2 in s, 'disp brush comment anchor moved'
    early = NL.join([
        "    var cx = c.getContext('2d');   // MUTANT: raw ramp for brush again",
        "    if (r.kind === 'brush') {",
        "      cx.drawImage(regionRawCanvas(r), r.bbox.x0 - dg.px0, r.bbox.y0 - dg.py0);",
        "      r._on = null; r._cDisp = c; r._cDispKey = key; return c;",
        "    }",
        "    // No kind is exempt, brush included."])
    return s.replace(an2, early, 1)


# THE SERVER IGNORES THE BRUSH NUMBERS: wireKind stopped routing a numbered brush to the
# 'auto' rules, so the feather the user sees on screen is KIND_RULES-forbidden in the
# render - preview softens, render does not.
def LEGACY_WIREBRUSH(s):
    an = "  function wireKind(r) {" + chr(10) + "    if (r.kind !== 'brush') return r.kind;"
    assert an in s, 'wireKind anchor moved'
    return s.replace(an, "  function wireKind(r) {" + chr(10)
        + "    return r.kind;   // MUTANT: label = contract" + chr(10)
        + "    if (r.kind !== 'brush') return r.kind;", 1)


# THE DRAG DRAWN UNDER A STALE ANSWER: preview survives translateSel, so the server's
# frozen overlay covers the canvas while only the bbox/ring follow the finger (report 4).
def LEGACY_STALEOVERLAY(s):
    NL = chr(10)
    an = NL.join(["    preview = null;", "    compose();", "  }", "", "  function bakeMove()"])
    assert an in s, 'translateSel preview-drop anchor moved'
    s = s.replace(an, NL.join(["    compose();", "  }", "", "  function bakeMove()"]), 1)
    # Removing ONLY the explicit drop cannot reproduce the bug because objSig carries the
    # live mvx/mvy (the second guard). A mutant has to actually restore the defect:
    # neither guard present, exactly as shipped when the user hit it.
    an2 = 'r.feather, r.area >> 4, r.mvx || 0, r.mvy || 0,'
    assert an2 in s, 'objSig offset term moved'
    return s.replace(an2, 'r.feather, r.area >> 4,', 1)


# THE VESTIGIAL BUTTON, restored: a manual 'Preview mask' press for the server answer
# that already arrives on its own with every paint - theatre the user retired.
def LEGACY_PREVIEWBTN(s):
    an = "    var actions = row('tb-actions');"
    assert s.count(an) == 1, 'actions row anchor moved'
    return s.replace(an, an + NL +
        "    actions.appendChild(btn('Preview mask', function () { status('Previewing mask (server-side, no GPU)…'); }, 'tb-key'));", 1)


# THE SILENT ZERO: the automatic round-trip swallows the server's empty/tiny verdict,
# so a mask that selects NOTHING costs a full render before the user finds out.
def LEGACY_NOWARN(s):
    an = "      if (r.empty || r.tiny) {"
    assert s.count(an) == 1, 'runPreview verdict anchor moved'
    return s.replace(an, "      if (false && (r.empty || r.tiny)) {", 1)


# THE SILENT REWRITE, restored: the server reduced the user's instruction to a caption
# but the submit line still only says 'Queued' — the words that went to the model
# differ from the words the user approved, and nobody says so.
def LEGACY_NOECHO(s):
    an = "      if (typeof sp.prompt_raw === 'string' && typeof sp.prompt === 'string'"
    assert s.count(an) == 1, 'submit-echo anchor moved'
    return s.replace(an, "      if (false && typeof sp.prompt_raw === 'string' && typeof sp.prompt === 'string'", 1)


MUTANTS["nofold"] = LEGACY_NOFOLD
# THE FEATHER-VANISHING-COMMIT, restored: the region rebuild hard-zeroed brush
# edge/feather while stamp() had already given the stroke the slider numbers - the soft
# edge under the finger disappeared the moment the stroke lifted (user report).
def LEGACY_COMMITZERO(s):
    an = NL.join(["        edge = val('tb_edge', 0);",
                  "        feather = val('tb_feather', 8);"])
    assert s.count(an) == 1, 'commit-branch anchor moved'
    return s.replace(an, NL.join([
        "        edge = kind === 'brush' ? 0 : val('tb_edge', 0);",
        "        feather = kind === 'brush' ? 0 : val('tb_feather', 8);"]), 1)


# THE DEAD SLIDERS BACK: visibility collapses to the old eraser-only rule, so polygon/
# smart/rect/select all show size AND hardness again.
def LEGACY_TOOLVIS(s):
    an = NL.join([
        "    if (el.brush) el.brush.parentNode.style.display =",
        "      (t === 'brush' || t === 'eraser') ? '' : 'none';",
        "    if (el.hardness) el.hardness.parentNode.style.display = (t === 'brush') ? '' : 'none';"])
    assert an in s, 'setTool visibility anchor moved'
    return s.replace(an, "    if (el.hardness) el.hardness.parentNode.style.display = (t === 'eraser') ? 'none' : '';", 1)


# THE SECOND LIVE FEATHER COPY, restored: the panel pair edits the selected object too,
# so two UIs claim the same number with different labels and histories again.

def LEGACY_PANEDITS(s):
    """The second LIVE edge/feather UI, resurrected: the hidden pair becomes visible,
    enabled, and routes onto the selected object again - the duplicate the user
    pointed at ('showing the same stupid value I can adjust in the select window')."""
    an = "      n.disabled = true;"
    assert s.count(an) == 1, 'syncKnobLock disable anchor moved'
    s = s.replace(an, "      n.disabled = false;   // MUTANT: edits whatever is touched", 1)
    an2 = "      if (n.parentNode) n.parentNode.style.display = 'none';"
    assert s.count(an2) == 1, 'syncKnobLock hide anchor moved'
    s = s.replace(an2, "      // MUTANT: and the panel pair shows itself again", 1)
    an3 = NL.join([
        "    i.addEventListener('input', function () {",
        "      out.textContent = i.value;",
        "      refresh();",
        "    });"])
    assert s.count(an3) == 1, 'ctl input handler anchor moved'
    return s.replace(an3, NL.join([
        "    i.addEventListener('input', function () {",
        "      out.textContent = i.value;",
        "      var o = selObj();",
        "      if ((id === 'edge' || id === 'feather') && o && o.kind) {",
        "        setObjParam(id === 'edge' ? 'edge' : 'feather', parseFloat(i.value) || 0);",
        "        return;",
        "      }",
        "      refresh();",
        "    });"]), 1)

# TUNING ARMS THE NEXT OBJECT: setObjParam writes the number into the hidden default
# store too, so 'strictly post-generation' becomes 'set the feather once and every
# later object inherits it' - the hidden twin of the visible mirror the user rejected.
def LEGACY_ARMLEAK(s):
    an = "    if (key === 'edge') o.foldIdx = null;"
    assert an in s, 'setObjParam fold-reset anchor moved'
    return s.replace(an, an + NL +
        "    if (el[key]) { el[key].value = String(v); if (el[key + '_out']) el[key + '_out'].textContent = String(v); }   // MUTANT: arms the NEXT object", 1)


# THE DASHED BOX, resurrected: compose() frames the selected object with a dashed blue
# rectangle the object does not even fill - the indicator retired for the cyan ring.
def LEGACY_DASHBOX(s):
    # Injected into BOTH compositing branches. The SB gate measures inside the 420 ms
    # preview debounce (the LOCAL WASH path) and after the stub answer lands (the overlay
    # path) — and the overlay paints opaquely over the canvas, so a dashed box placed
    # under it is hidden and the mutant survives: an early single-point version proved
    # exactly that at 158/158. The historical defect drew on top of whatever composited.
    DASH = [
        "      var _so = selObj();                     // MUTANT: dashed bbox is back",
        "      if (_so) { var _b = objBBox(_so);",
        "        if (_b) { v.save(); v.strokeStyle = '#3ba7ff'; v.globalAlpha = 1;",
        "          v.lineWidth = Math.max(3.5, 2 * (scale() || 1));",
        "          v.setLineDash([6, 4]);",
        "          v.strokeRect(_b.x0 + (_so.mvx || 0) + .5, _b.y0 + (_so.mvy || 0) + .5,",
        "                       _b.x1 - _b.x0 + 1, _b.y1 - _b.y0 + 1);",
        "          v.restore(); } }"]
    an = NL.join(["      v.drawImage(preview.img, 0, 0, W, H);", "      drawActiveOutline(v);"])
    assert s.count(an) == 1, 'compose overlay anchor moved'
    s = s.replace(an, NL.join(["      v.drawImage(preview.img, 0, 0, W, H);"] + DASH +
                              ["      drawActiveOutline(v);"]), 1)
    an2 = NL.join(["    v.drawImage(wash, 0, 0);", "    var so = selObj();"])
    assert s.count(an2) == 1, 'compose wash anchor moved'
    s = s.replace(an2, NL.join(["    v.drawImage(wash, 0, 0);"] + DASH +
                               ["    var so = selObj();"]), 1)
    # (Injected-block indentation is cosmetic in JS — one block, both branches.)
    return s


# THE ZOOM LIE, restored: the label printed a hardcoded 100% in Fit mode regardless of
# the real image:device pixel ratio.
def LEGACY_ZOOMLIE(s):
    an = "    if (el.zoomVal) el.zoomVal.textContent = Math.round(curZoom() * 100) + '%';"
    assert s.count(an) == 1, 'zoom label anchor moved'
    return s.replace(an,
        "    if (el.zoomVal) el.zoomVal.textContent = (zoomFit ? '100' : String(Math.round(zoom * 100))) + '%';", 1)


# THE LIQUID BOX: below fit the canvas silently stretches to the stage while the label
# keeps a smaller number - percent and pixels must come from ONE equation.
def LEGACY_FLUIDBOX(s):
    an = NL.join([
        "      el.box.style.width = zoomFit ? '100%'",
        "        : Math.max(1, Math.round(W * zoom / dpr())) + 'px';"])
    assert s.count(an) == 1, 'zoom box width anchor moved'
    return s.replace(an, "      el.box.style.width = '100%';   // MUTANT: free mode silently re-fluids", 1)


# THE UNEQUAL PAIR, restored: before the fixed name/value columns, the always-present
# edge hint (a width:100% flex item IN the slider row) plus the shorter 'edge' word
# made the edge track a few dozen px narrower than feather — the user's "not the same
# size which is weird". Faithful reproduction: put the hint back inline.
def LEGACY_SIZERIFT(s):
    an = "    lab.appendChild(wrap); lab.appendChild(rel);"
    assert s.count(an) == 1, 'edgePair row assembly anchor moved'
    return s.replace(an, an + NL +
        "    rel.style.flex = '0 0 auto'; rel.style.width = 'auto';   // MUTANT: hint back inline, eats the track", 1)


MUTANTS["commitzero"] = LEGACY_COMMITZERO
MUTANTS["sizerift"] = LEGACY_SIZERIFT
MUTANTS["toolvis"] = LEGACY_TOOLVIS
MUTANTS["panedits"] = LEGACY_PANEDITS
MUTANTS["armleak"] = LEGACY_ARMLEAK
MUTANTS["dashbox"] = LEGACY_DASHBOX
MUTANTS["previewbtn"] = LEGACY_PREVIEWBTN
MUTANTS["nowarn"] = LEGACY_NOWARN
MUTANTS["noecho"] = LEGACY_NOECHO
MUTANTS["zoomlie"] = LEGACY_ZOOMLIE
MUTANTS["fluidbox"] = LEGACY_FLUIDBOX


MUTANTS["latentpunch"] = LEGACY_LATENTPUNCH
MUTANTS["bakepad"] = LEGACY_BAKEPAD
MUTANTS["bakefeather"] = LEGACY_BAKEFEATHER
MUTANTS["erasepaints"] = LEGACY_ERASEPAINTS
MUTANTS["brushsoft"] = LEGACY_BRUSHSOFT
MUTANTS["wirebrush"] = LEGACY_WIREBRUSH
MUTANTS["staleoverlay"] = LEGACY_STALEOVERLAY


# Not every MUTANTS entry is a bug-restoration the suite must reject. This one is
# EXPECTED to leave the suite green BY DESIGN, and a sweep must not report it alive:
#   seldbg — a diagnostic event-trace injector (feeds window.__SEL for the select
#            asserts). Its membership in the mutant registry exists to CRASH when the
#            select anchors move, not to go red; running it clean is running it right.
# nosig is NOT in this set and must die: compose() itself refuses an overlay whose
# preview.sig mismatches the live previewSig, so the dropped objSig() term IS
# observable — once the DR __PVHOLD hook can strand an answer across the move.
EXPECTED_ALIVE = {"seldbg"}


def main():
    a = sys.argv[1:]
    mutate = doc_mutate = None
    which = a[a.index("--mutate") + 1] if "--mutate" in a else None
    if which == "legacy":
        mutate, doc_mutate = LEGACY_JS, LEGACY_CSS
    elif which:
        mutate = MUTANTS[which]
        assert mutate, "unknown mutant: %s" % which
    if "--dbg" in a:
        mutate = dbg.inject
    # A mutant that silently fails to match is a false green: it reports the old bug as
    # fixed because the old bug was never put back. Fail loudly instead.
    if mutate:
        assert mutate(gen.SHIPPED_JS) != gen.SHIPPED_JS, "mutant '%s' changed nothing" % which
    if doc_mutate:
        real_css = (gen.WEB / "toolbox.css").read_text()
        assert doc_mutate(real_css) != real_css, "css mutant changed nothing"



    w = int(a[a.index("--width") + 1]) if "--width" in a else 420
    h = int(a[a.index("--height") + 1]) if "--height" in a else 620
    out_html = HERE / ("t%s.html" % ("mut" if mutate else "ok"))
    gen.build(str(out_html), driver(), mutate=mutate, doc_mutate=doc_mutate)
    # a viewport short enough that the portrait photo must overflow vertically: that is the
    # condition that used to mint the second (horizontal) scrollbar
    cmd = ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
           "--disable-dev-shm-usage",
           # Headless Chrome defaults to OVERLAY scrollbars, which steal no layout width --
           # so the classic "vertical bar mints a horizontal bar" loop physically cannot
           # happen, and the legacy negative control passed 30/30 while reproducing nothing.
           # Forcing classic scrollbars makes this environment able to exhibit the bug the
           # user actually reported (their desktop has non-overlay bars).
           "--disable-features=OverlayScrollbar",
           "--window-size=%d,%d" % (w, h), "--virtual-time-budget=60000",
           "--dump-dom", "file://" + str(out_html)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    m = re.search(r'<pre id="__TBRESULT__">(.*?)</pre>', p.stdout, re.S)
    if not m:
        print("NO RESULTS from chrome. rc=%d" % p.returncode)
        print((p.stderr or "")[-1500:])
        print((p.stdout or "")[:1200])
        return 2
    res = json.loads(html.unescape(m.group(1)))
    if res.get('dbgerr'): print('DBGERR', res['dbgerr'])
    if res.get('dbg'):
        print('COMPOSE TRACE:')
        for i, d in enumerate(res['dbg'][:10]):
            print('  ', i, d)
    EXPECT = 162   # 161 + 1 edge/feather equal-track gate: visibility x6 (pair NEVER shown), KF promise x2,
                   # fresh-hard x1, no-arm-leak x1, no-dashed-box x1, ZM honesty x3, A3 belt x1
                 # wire-no-punch (2), feather-live group (4), re-merge no-remnant (3)
    if len(res["tests"]) != EXPECT:
        print("TRUNCATED RUN: got %d assertions, expected %d -- an early error stopped the "
              "steps (notes: %s). This is NOT a pass." %
              (len(res["tests"]), EXPECT, res.get("note")))
        return 3
    bad = [t for t in res["tests"] if not t["pass"]]
    for t in res["tests"]:
        print("%s  %-62s %s" % ("PASS" if t["pass"] else "FAIL", t["name"], t["detail"]))
    for n in res["note"]:
        print("NOTE", n[:300])
    print("\n%d/%d assertions passed  (viewport %dx%d, %s)"
          % (len(res["tests"]) - len(bad), len(res["tests"]), w, h,
             "MUTATED" if mutate else "shipped code"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
