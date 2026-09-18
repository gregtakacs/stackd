"""Run the toolbox editor driver in real headless Chrome and report per-assertion results.

    python3 tests/tbtest/run_tbtest.py [--mutate NAME] [--width 420] [--height 620]
    (--mutate: sticky ctlorder softpunch brushfeather kindstr autoslider widetol
     rawwash allmerge nosig selguard twoslider noderive blendblank seldbg
     barwash bar95 polltoken legacy)

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
    g = "if ((id === 'edge' || id === 'feather') && o && o.kind) {"
    assert g in s, 'toolbar region-routing guard anchor moved'
    return s.replace(g, "if (false) {", 1)


MUTANTS = {"sticky": LEGACY_STICKY, "ctlorder": LEGACY_CTL, "softpunch": LEGACY_PUNCH,
           "brushfeather": LEGACY_BRUSHFEATHER, "kindstr": LEGACY_KINDSTR,
           "autoslider": LEGACY_AUTOSLIDER, "widetol": LEGACY_WIDETOL,
           "rawwash": LEGACY_RAWWASH, "allmerge": LEGACY_ALLMERGE}


# The stale-overlay bug, reproduced exactly as it shipped: previewSig() derived from
# strokes.length + the global sliders only, so dragging a placed object (which changes
# neither) let the debounce answer "same request" and keep painting the old overlay.
def LEGACY_NOSIG(s):
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
    g = "      if ((id === 'edge' || id === 'feather') && o && o.kind) {"
    assert g in s, 'toolbar routing anchor moved'
    s = s.replace(g,
        "      if ((id === 'edge' || id === 'feather') && o && o.kind && o.kind !== 'brush') {" + chr(10) +
        "        if (o.kind === 'brush') return;", 1)
    h = "      setObjParam('edge', v);"
    assert s.count(h) == 1, 'edgePair live handler anchor moved'
    s = s.replace(h,
        "      if (o.kind === 'brush') { inp.disabled = true; return; }" + chr(10) + h, 1)
    f = "      setObjParam(key, parseFloat(inp.value) || 0);"
    assert s.count(f) == 1, 'ipair live handler anchor moved'
    s = s.replace(f,
        "      if (obj.kind === 'brush') { inp.disabled = true; return; }" + chr(10) + f, 1)
    return s


MUTANTS["inspredraw"] = LEGACY_INSPREDRAW
MUTANTS["brushdead"] = LEGACY_BRUSHDEAD

MUTANTS["barwash"] = LEGACY_BARWASH
MUTANTS["bar95"] = LEGACY_BAR95
MUTANTS["polltoken"] = LEGACY_POLLTOKEN


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
    EXPECT = 104
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
