/* Comfy Toolbox — mount-agnostic mask editor.
 *
 * The SAME file runs (a) as the standalone lab page and (b) inside Open WebUI's rich-UI
 * embed, which renders the HTML we hand it into a sandboxed srcdoc iframe (verified in
 * the 0.11.3 bundle: sandbox = allow-scripts/allow-downloads/allow-forms, NOT
 * allow-same-origin; the only postMessage the renderer honours is iframe:height). So:
 *   - no cookies, no localStorage, no sessionStorage, no same-origin assumptions;
 *   - config arrives via window.__TB__ (injected by stackd/toolbox/web.py);
 *   - the only network calls are fetch()es to CFG.api with the launch token in a
 *     header — never a cookie, never credentials: 'include'.
 *
 * Division of labour with the server (stackd/toolbox/masks.py owns mask semantics):
 * this canvas paints at the SOURCE image's natural size and exports white-RGB +
 * alpha-coverage PNG. Nothing here resamples, thresholds or feathers — the server
 * resamples to the job's working dims and applies expand/feather, which is what makes
 * an imprecise brushstroke acceptable: overshoot is absorbed server-side.
 */
(function () {
  'use strict';

  var CFG = window.__TB__ || {};
  var API = String(CFG.api || '').replace(/\/+$/, '');
  var TOKEN = String(CFG.token || '');

  var $ = function (id) { return document.getElementById(id); };
  var el = {};
  var W = 0, H = 0;             // natural (source) pixel size of all layers
  var baseC = null, maskC = null, viewC = null, ringC = null;
  var strokes = [], active = null, tool = 'brush';
  /* ---- selection OBJECTS are connected components of the composited paint ----
   * What the user selects, tunes and deletes is a REGION (an 8-connected blob of the
   * final mask pixels), not the stroke that painted it: a brush touch that lands on a
   * smart-select becomes PART of that object (one object, one edge/feather adjuster),
   * an eraser slice that cuts a blob in two splits the object into two (each fragment
   * retains the parent's edge and feather — the feather across the fresh cut is the
   * seam-hider), and two blobs bridged by one stroke become one (edge resets to its
   * actual size, feather the ancestor-weighted average of the two). Strokes remain
   * only as the paint history rasterize() replays — that keeps undo push/pop honest
   * without the user ever touching a vector parameter after commit.
   * Contiguity is measured on the RAW composited pixels, never on the displayed
   * morphology: grow/feather are adjustments ON TOP, and if they decided identity a
   * slider drag that made two objects touch would silently merge them (and average
   * their feathers) under the user's own finger.
   */
  var MAX_REGIONS = 64;             // blobs past this are painted but not selectable
  var REGION_ALPHA_MIN = 50;        // mirrors masks.COVERAGE_BRIGHTNESS_THRESHOLD: the
                                    // server's own "counts as selected" number, so the
                                    // client's object identity cannot disagree with what
                                    // the server will actually paint
  var regions = [];                 // [{id, kind, edge, feather, area, bbox, disp, dispKey, mvx, mvy, _dg}]
  var regionSeq = 1;
  var regionRev = 0;                // bumped on every pixel rebuild (cache generations)
  var compLabel = null;             // Int32Array W*H: 0 = background, else region index + 1
  var maskA = null;                 // Uint8Array W*H: composited alpha, one read per rebuild
  var commitHintKind = 'shape';     // kind a zero-ancestor (brand-new) blob inherits
  var _dragReg = null;              // region currently being dragged by the Select tool
  // Smart-select (SAM3) session state. Points are kept in NORMALISED 0..1 coords so a zoom
  // or resize never shifts a committed click. Each SUCCESSFUL selection is its own OBJECT in
  // smartObjs — {pos, neg, layer} — so "select a car, then select another car" yields TWO
  // independent masks, each separately growable/featherable/deletable. shift/alt refine the
  // CURRENT object (the most recent one), which is what a follow-up click means to a user;
  // select another object with the Select tool and refine targets are unchanged (refine only
  // ever applies to the object you are actively building). The server re-infers the mask from
  // the WHOLE point list each call, so an excluded region genuinely vanishes rather than
  // ghosting back. smartSeq guards against a late reply applying out of order.
  var smartObjs = [], smartCur = -1, smartSeq = 0;
  // The lasso refine mode: Auto = trace a loose loop, seed SAM3 inside it (snaps to the
  // true boundary); Manual = commit the exact pixels traced. A one-button toggle.
  var smartAuto = true, smartDrag = null;
  // The object being manipulated by the Select tool, as an index into `strokes`.
  // An index and not a reference because undo/redo splice the array; every use
  // re-checks the bounds so a stale selection after Undo cannot throw.
  var sel = -1, selDrag = null;
  var redoStack = [];           // entries popped by undo, pushed back by redo
  var preview = null;           // last server-faithful overlay {img, sig}; null = re-preview

  // Zoom/pan: the canvas backing store always stays at natural W×H; zoom only changes the
  // CSS display size inside a scrollable stage. toNatural maps off the RENDERED rect, so it
  // needs no zoom term — the browser's own scaling of the backing store to the element's
  // box does that. fitW is the width the canvas gets at zoom 1 (the stage's inner width),
  // so the default (zoom 1) view is exactly what a no-zoom build showed.
  var zoom = 1, fitW = 0;
  var ZOOM_MIN = 1, ZOOM_MAX = 6;

  // Two-pointer pinch bookkeeping (mobile). Map pointerId -> {x,y} client px while active.
  var ptrs = {}, pinchStart = null;
  var dragEnabled = false;      // true only while a single pointer is actively painting
  var pinching = false;         // suppresses painting while two pointers drive zoom
  var pinchEnd = 0;             // end of the last pinch; a down() soon after is a leftover finger

  function status(msg, kind) {
    el.status.textContent = msg;
    el.status.className = 'tb-status' + (kind ? ' tb-' + kind : '');
  }
  function val(id, dflt) {
    var n = parseFloat(($(id) || {}).value);
    return isFinite(n) ? n : dflt;
  }

  /* ---------------- geometry ---------------- */
  // Pointer -> natural-image coords. The <canvas> element stays at natural pixel size
  // and CSS scales it (width fits the stage, grows with zoom); scaling the backing store
  // instead is what makes a mask "look right and paint the wrong pixels" on a phone, where
  // devicePixelRatio and CSS width both drift from the image's own size. Reading the LIVE
  // rect each time is what makes this correct under zoom, after a reflow, and mid-pinch —
  // the one input that would silently break painting is a cached scale factor.
  function toNatural(ev) {
    var r = viewC.getBoundingClientRect();
    var t = (ev.touches && ev.touches[0]) || (ptrs && ev.pointerId && ptrs[ev.pointerId]) || ev;
    return {
      x: (t.clientX - r.left) * (r.width ? W / r.width : 1),
      y: (t.clientY - r.top) * (r.height ? H / r.height : 1)
    };
  }
  // natural px per CSS px. Used to size the cursor ring so it is the SAME physical size
  // whatever the image happens to be scaled to (a 60px brush is 60 natural px wide on the
  // mask at any zoom, so it must LOOK 60/scale CSS px to match what it will select).
  function scale() {
    var r = viewC.getBoundingClientRect();
    return r.width ? W / r.width : 1;
  }
  function brushR() { return Math.max(1, val('tb_brush', 60) * scale() / 2); }
  function hardness() { return Math.min(1, Math.max(0, val('tb_hardness', 0.55))); }
  // Per-object geometry included in the preview cache key.
  //
  // HONEST STATUS: completeness insurance, NOT a demonstrated bug fix, and it must not be
  // described as one. An earlier draft of this comment claimed that dragging a placed
  // object would otherwise leave a stale overlay on screen; the browser suite disproved
  // that -- the mutation removing objSig() passes every assertion. It cannot manifest
  // today because schedulePreview() sets `preview = null` on every repaint and
  // runPreview() has no other caller, so the signature is never consulted while an
  // overlay is actually on screen.
  //
  // The change that WOULD expose it is the obvious flicker optimisation: stop blanking the
  // overlay while the request is in flight. compose() would then decide via
  // `preview.sig === previewSig()` whether to keep painting it, and without per-object
  // geometry in that key, moving or re-tuning an object would leave both the key and the
  // overlay untouched. Keeping the key complete costs nothing and defuses that trap.
  function objSig() {
    // Preview-cache key over the REGION table (post-refactor truth): params live on the
    // blob, not the stroke, so a feather change on an object nobody is holding must still
    // invalidate the cached server overlay. Geometry folds in as quantised bbox corners
    // and an area bucket — any real paint edit moves them; sub-pixel jitter cannot.
    var parts = [];
    for (var i = 0; i < regions.length; i++) {
      var r = regions[i], b = r.bbox || { x0: 0, y0: 0, x1: 0, y1: 0 };
      parts.push([r.kind, r.edge, r.feather, r.area >> 4,
                  Math.round(b.x0 / 4), Math.round(b.y0 / 4),
                  Math.round(b.x1 / 4), Math.round(b.y1 / 4)].join(':'));
    }
    return parts.join('|') + '#' + strokes.length;
  }

  function previewSig() {
    return [strokes.length, active ? active.pts.length : -1, tool,
            val('tb_brush', 60), val('tb_hardness', 0.55), paramsSig(), objSig()].join('|');
  }
  function paramsSig() {
    var p = params();
    return [p.width, p.height, p.mask_expand, p.mask_shrink, p.mask_feather, p.invert,
            p.overlay_alpha].join(',');
  }

  /* ---------------- painting model: strokes replay, but OBJECTS are pixels ----------
   * Strokes stay point lists because rasterize() replays them, which is what makes undo
   * push/pop honest and the eraser re-orderable BEFORE commit. But the user-facing object
   * is no longer a stroke: it is a connected component of the composited paint
   * (rebuildRegions below), so after commit there is nothing to "re-parameterise" — the
   * pure pixel values ARE the selection, exactly as a mask should be.
   */
  function rasterize() {
    if (!maskC) return;
    var m = maskC.getContext('2d', { willReadFrequently: true });
    m.clearRect(0, 0, W, H);
    var i;
    for (i = 0; i < strokes.length; i++) paintStroke(m, strokes[i]);
    // Identity from COMMITTED pixels only — and NOT on every pointermove: the O(pixels)
    // rebuild is gated off mid-stroke AND mid-select-drag (during a drag the blob moves
    // as a display offset; rebuildRegions() would re-derive it from pre-move pixels
    // every frame and starve a phone). The table is rebuilt once, at commit.
    if (!((dragEnabled && active) || (tool === 'select' && selDrag))) rebuildRegions();
    if (active) paintStroke(m, active);   // the in-progress shape is not an object yet
    compose();
    schedulePreview();     // any paint change invalidates the server preview; refresh on idle
  }

  // What a committed object IS, for the server: 'brush' means "the edge was chosen by
  // hand via hardness, leave it alone"; 'shape' means "this boundary is arbitrary, so
  // grow/shrink/feather are meaningful corrections". 'auto' is the prompt/CLIPSeg mask,
  // which is hard-edged and typically under-selects, so it wants the shape treatment too.
  function kindOf(s) {
    if (s.mode === 'load') return 'auto';
    if (s.mode === 'free') return 'brush';
    return 'shape';                                  // rect / ellipse / poly / draw
  }

  // Stamp the current slider values onto a just-committed object. Read AT COMMIT time:
  // the sliders are the defaults for the NEXT object, not a live global filter.
  function stamp(s) {
    s.kind = kindOf(s);
    if (s.kind === 'brush') {
      s.edge = 0; s.grow = 0; s.shrink = 0; s.feather = 0;   // hardness already defined the edge
    } else {
      s.edge = val('tb_edge', 0);
      // grow/shrink stay on the wire as the derived pair, so a server that only understands
      // the old two-field protocol still agrees with the new signed one instead of silently
      // ignoring the knob. They are NEVER independent: one number, two names.
      s.grow = s.edge > 0 ? s.edge : 0;
      s.shrink = s.edge < 0 ? -s.edge : 0;
      s.feather = val('tb_feather', 8);
    }
    return s;
  }

  /* ---------------- selecting and adjusting a placed object ----------------
   * Objects stay vectors for their whole life, so "adjust after placing" is a transform of
   * the point list, not a re-paint: moving is adding a delta, scaling is multiplying about
   * the centre. That is also why the per-object params are honest — the geometry the user
   * sees and the geometry the server rasterizes are derived from the same numbers.
   */
  function objBBox(o) {
    if (o && o.bbox) return o.bbox;            // a REGION: measured, not guessed
    // A load/auto (smart-select) stroke carries no vertex list — its extent is the opaque
    // region of the PNG it draws. The tight coverage box (o._bb) is measured once at load
    // time by measureLoadBBox; until that lands (or if it fails) fall back to the full
    // frame so the object is still selectable. THIS MUST PRECEDE the !pts guard: a load
    // stroke has no pts, so testing pts first returned null and made every smart selection
    // invisible to hitTest — the inspector that offers grow/shrink/feather for an 'auto'
    // object could never open, so "edit the selection the tool made" was impossible.
    if (o.mode === 'load') return o._bb || { x0: 0, y0: 0, x1: W, y1: H };
    if (!o.pts || !o.pts.length) return null;
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (var i = 0; i < o.pts.length; i++) {
      var p = o.pts[i];
      if (p.x < x0) x0 = p.x; if (p.y < y0) y0 = p.y;
      if (p.x > x1) x1 = p.x; if (p.y > y1) y1 = p.y;
    }
    var pad = (o.size || 0) / 2;                    // a brush's footprint is part of its box
    return { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad };
  }

  // Measure the tight coverage box of a loaded (smart-select / auto) PNG ONCE, in NATURAL
  // canvas coords, and stash it on the stroke as o._bb so objBBox/hitTest/the edge "% of
  // this object" readout all agree on where the selection actually is. Scanned on a
  // downsampled copy so a 1024² mask costs a fixed 96² pass, not a per-frame pixel walk.
  function measureLoadBBox(o) {
    if (!o || o.mode !== 'load' || !o.img) return null;
    try {
      var g = 96;
      var c = document.createElement('canvas'); c.width = g; c.height = g;
      var x = c.getContext('2d');
      x.clearRect(0, 0, g, g);
      x.drawImage(o.img, 0, 0, g, g);
      var d = x.getImageData(0, 0, g, g).data;
      var x0 = g, y0 = g, x1 = -1, y1 = -1, any = false;
      for (var yy = 0; yy < g; yy++) {
        for (var xx = 0; xx < g; xx++) {
          if (d[(yy * g + xx) * 4 + 3] > 16) {                 // alpha = coverage (load contract)
            any = true;
            if (xx < x0) x0 = xx; if (xx > x1) x1 = xx;
            if (yy < y0) y0 = yy; if (yy > y1) y1 = yy;
          }
        }
      }
      if (!any) { o._bb = null; return null; }
      o._bb = { x0: x0 / g * W, y0: y0 / g * H,
                x1: (x1 + 1) / g * W, y1: (y1 + 1) / g * H };
      return o._bb;
    } catch (e) { o._bb = null; return null; }                 // a tainted/failed read is not fatal
  }

  // A representative scatter of points strictly INSIDE a closed polygon, used to seed the
  // proven SAM3 point prompt from a hand-traced lasso loop. Centroid + the interior samples
  // of a coarse grid over the bbox; ray-cast point-in-polygon keeps only points that are
  // genuinely inside the traced shape, so SAM3 is prompted on the object, never on background
  // the loose loop swept over. Capped so a big loop stays one cheap GPU call.
  function interiorSeeds(pts, cap) {
    if (!pts || pts.length < 3) return [];
    var ax = 1e9, ay = 1e9, bx = -1e9, by = -1e9, sx = 0, sy = 0, i;
    for (i = 0; i < pts.length; i++) {
      if (pts[i].x < ax) ax = pts[i].x; if (pts[i].x > bx) bx = pts[i].x;
      if (pts[i].y < ay) ay = pts[i].y; if (pts[i].y > by) by = pts[i].y;
      sx += pts[i].x; sy += pts[i].y;
    }
    function inside(px, py) {
      var c = false, j = pts.length - 1;
      for (i = 0; i < pts.length; j = i++) {
        var xi = pts[i].x, yi = pts[i].y, xj = pts[j].x, yj = pts[j].y;
        if (((yi > py) !== (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi) + xi)) c = !c;
      }
      return c;
    }
    var out = [];
    if (inside(sx / pts.length, sy / pts.length)) out.push([sx / pts.length, sy / pts.length]);
    var STEP = 5;                                              // ~5x5 grid over the bbox
    for (var gy = 0; gy <= STEP && out.length < (cap || 16); gy++) {
      for (var gx = 0; gx <= STEP && out.length < (cap || 16); gx++) {
        var px = ax + (bx - ax) * gx / STEP, py = ay + (by - ay) * gy / STEP;
        if (inside(px, py)) out.push([px, py]);
      }
    }
    return out;
  }

  function hitTest(p) {
    // Objects are REGIONS (connected paint blobs), so the finger grabs the pixels it
    // actually touches: exact label lookup FIRST (a tap in the hole of a ring, or in a
    // channel an eraser cut, selects NOTHING — bbox-only hit-testing is the historic
    // smart-select grab-shadowing bug). The fallback for a near-miss then measures
    // DISTANCE TO REAL PIXELS of each region (windowed label scan), not to bboxes: a
    // bbox fallback would reintroduce the very shadowing it exists to prevent — two
    // boxes overlapping a gap always pick whichever sorts first.
    //
    // The window is 8 CSS px (what a finger aims), converted natural = px x scale().
    // The shipped formula divided; at the suite's ~0.36 px/css that widened tolerance
    // from 3 natural px to 23 and let the empty channel grab a stranger. Minimum 2
    // natural px so a 1:1 canvas (or a zoomed phone) keeps a usable finger target.
    if (!compLabel || !regions.length) return -1;
    var ix = Math.round(p.x), iy = Math.round(p.y);
    if (ix >= 0 && iy >= 0 && ix < W && iy < H) {
      var id = compLabel[iy * W + ix];
      if (id) return id - 1;
      var tol = Math.max(2, Math.round(8 * scale()));
      var best = -1, bestN = 0;
      var y0 = Math.max(0, iy - tol), y1 = Math.min(H - 1, iy + tol);
      var x0 = Math.max(0, ix - tol), x1 = Math.min(W - 1, ix + tol);
      var counts = {}, y2, x2;
      for (y2 = y0; y2 <= y1; y2++) {
        for (x2 = x0; x2 <= x1; x2++) {
          var q = compLabel[y2 * W + x2];
          if (!q) continue;
          var c = (counts[q] || 0) + 1;
          counts[q] = c;
          if (c > bestN) { bestN = c; best = q; }
        }
      }
      if (best > 0) return best - 1;
    }
    return -1;
  }

  function selObj() { return (sel >= 0 && sel < regions.length) ? regions[sel] : null; }

  function selectObj(i) {
    sel = (i >= 0 && i < regions.length) ? i : -1;
    // Mirror the object's edge/feather into the toolbar sliders so the obvious slider
    // reads (and then edits) THIS object. Brush objects are excluded: the server forbids
    // any morphology on a hand-painted edge (KIND_RULES['brush']), and a live slider that
    // silently did nothing is the exact lie this project exists to prevent.
    var o = selObj();
    if (o && o.kind !== 'brush') {
      if (el.edge) { el.edge.value = (typeof o.edge === 'number') ? o.edge : 0;
                     if (el.edge_out) el.edge_out.textContent = String(el.edge.value); }
      if (el.feather) { el.feather.value = (typeof o.feather === 'number') ? o.feather : 0;
                        if (el.feather_out) el.feather_out.textContent = String(el.feather.value); }
    }
    syncInspector();
    compose();
    if (sel < 0) status('Nothing selected.');
    else status(o.kind === 'auto'
      ? 'Selected an auto selection — use edge / feather to grow, shrink or soften it.'
      : 'Selected a ' + o.kind + ' selection — drag to move it.');
  }

  function translateSel(dx, dy) {
    // A region is pixels: dragging offsets its pixels for display (cheap, live); the move
    // is BAKED into one compound stroke on release (bakeMove). It is also what finally
    // makes a smart-select movable — the old vector translate was a silent no-op for it.
    var o = selObj(); if (!o) return;
    o.mvx = (o.mvx || 0) + dx;
    o.mvy = (o.mvy || 0) + dy;
    compose();
  }

  function bakeMove() {
    var r = _dragReg; _dragReg = null;
    if (!r) return;
    var dx = Math.round(r.mvx || 0), dy = Math.round(r.mvy || 0);
    r.mvx = 0; r.mvy = 0;
    if (!dx && !dy) return;
    var cv = regionRawCanvas(r, true);
    // ONE compound stroke (lift the blob's pixels here, lay them down at the offset).
    // Two strokes would let Undo pop half a move and leave the object drawn twice —
    // the compound keeps vector-undo honest for something vectors could never express.
    strokes.push({ mode: 'move', cv: cv, ox: r.bbox.x0, oy: r.bbox.y0, dx: dx, dy: dy,
                   kind: 'shape', edge: 0, grow: 0, shrink: 0, feather: 0,
                   size: 0, hardness: 1, erase: false });
    rasterize();
    status('Moved.');
  }

  // The inspector is what makes per-object parameters *legible*: the global sliders are
  // defaults for the NEXT object, and this panel edits the one you already placed. A brush
  // object shows its feather field greyed out with the reason, because that is the whole
  // semantic point — hardness chose that edge, and a server blur on top of it would just
  // shrink the region (ComfyUI binarizes the mask before it ever blends).
  var el_inspect = null;
  function buildInspector(root) {
    if (el_inspect || !root) return;
    el_inspect = mk('div', 'tb-inspect');
    el_inspect.style.display = 'none';
    root.appendChild(el_inspect);
  }

  function ipair(label, key, obj, min, max, step, val_, disabled) {
    var lab = mk('label', 'tb-lab', label);
    var wrap = mk('span', 'tb-num');
    var inp = mk('input');
    inp.type = 'range'; inp.min = min; inp.max = max; inp.step = step; inp.value = val_;
    var out = mk('span', 'tb-v', String(val_));
    inp.disabled = !!disabled;
    inp.addEventListener('input', function () {
      out.textContent = inp.value;
      var o = selObj(); if (!o) return;
      if (inp.disabled) return;
      o[key] = parseFloat(inp.value);
      touchObject();               // a parameter change: pixels untouched, view + cache refresh
    });
    wrap.appendChild(inp); wrap.appendChild(out);
    lab.appendChild(wrap);
    if (disabled) lab.appendChild(mk('span', 'tb-hint', 'hardness sets this edge'));
    return lab;
  }

  // The bipolar edge row, plus what the same value means against THIS object.
  //
  // Pixels drive the control; the percentage is derived and displayed, never stored. That
  // division is deliberate. A percentage of the object cannot be the stored number: it is
  // undefined for a non-square selection (5% of a 300x40 shape is 2 px or 15 px depending on
  // which side you pick), it silently changes meaning when the object is scaled with the
  // Select handle, and on a ring it destroys the selection -- 10% of a 566 px bbox is a 43 px
  // grow, enough to fill the hole the user deliberately erased, because a bounding box says
  // nothing about how thick the material is. Pixels are also the unit the graph's GrowMask
  // nodes use, which is what keeps the preview and the render the same number.
  //
  // So the number the user edits is honest, and the number that gives them scale-intuition
  // is computed from it. obj_min_side is the object's own extent in canvas px, which is the
  // same space the server rasterizes this layer into.
  function edgePair(o) {
    var lab = mk('label', 'tb-lab', 'edge');
    var wrap = mk('span', 'tb-num');
    var inp = mk('input');
    inp.type = 'range'; inp.min = -60; inp.max = 60; inp.step = 2;
    inp.value = (typeof o.edge === 'number') ? o.edge : 0;
    if (o.kind === 'brush') {                        // handwork edge: KIND_RULES forbids morphing it
      inp.disabled = true;
      lab.appendChild(mk('span', 'tb-hint', 'hardness sets this edge'));
    }
    var out = mk('span', 'tb-v', String(inp.value));
    var rel = mk('span', 'tb-hint', '');
    function paint(v) {
      out.textContent = v > 0 ? '+' + v : String(v);
      var b = objBBox(o);
      var side = b ? Math.max(1, Math.min(b.x1 - b.x0, b.y1 - b.y0)) : 0;
      // shrink is the direction that can delete a small selection outright, so only that
      // side gets the warning colour; the server clamps it regardless (masks._erode_with_cap)
      var risky = v < 0 && side && (-v) > side / 2;
      rel.textContent = side
        ? Math.round(100 * Math.abs(v) / side) + '% of this object' +
          (risky ? ' — at risk of erasing it; the server will keep what survives' : '')
        : '';
      rel.classList.toggle('tb-warn', !!risky);
    }
    inp.addEventListener('input', function () {
      if (inp.disabled) return;
      var v = parseInt(inp.value, 10) || 0;
      var t = selObj(); if (!t) return;
      t.edge = v;                     // the derived grow/shrink pair is built at export
      paint(v);
      touchObject();
    });
    paint(parseInt(inp.value, 10) || 0);
    wrap.appendChild(inp); wrap.appendChild(out);
    lab.appendChild(wrap); lab.appendChild(rel);
    return lab;
  }

  function syncInspector() {
    if (!el_inspect) return;
    var o = selObj();
    if (!o) { el_inspect.innerHTML = ''; el_inspect.style.display = 'none'; return; }
    el_inspect.style.display = '';
    el_inspect.innerHTML = '';
    var kind = o.kind;
    var brush = kind === 'brush';
    // The inspector edits the OBJECT (the connected blob), not the strokes that made it:
    // after commit there is no size/hardness vertex left to re-tune — the pixels are the
    // selection. A brush blob shows its edge/feather rows disabled with the reason,
    // because the server refuses morphology on handwork (masks.KIND_RULES['brush']).
    el_inspect.appendChild(mk('div', 'tb-inspect-h',
      (brush ? 'Brush selection' : (kind === 'auto' ? 'Auto selection' : 'Shape selection')) +
      ' — ' + (o.area || 0) + ' px'));
    el_inspect.appendChild(edgePair(o));
    el_inspect.appendChild(ipair('feather', 'feather', o, 0, 64, 1, o.feather || 0, brush));
    var rowb = mk('div', 'tb-row');
    rowb.appendChild(btn('Delete object', deleteSel, 'Remove this object from the mask'));
    rowb.appendChild(btn('Deselect', function () { selectObj(-1); }));
    el_inspect.appendChild(rowb);
  }

  function deleteSel() {
    var r = selObj(); if (!r) return;
    var cv = regionRawCanvas(r, true);
    // Deleting an OBJECT (a blob that may be made of many strokes) lifts exactly its
    // pixels as one synthetic erase stroke — splicing a stroke could not express it once
    // contiguity, not authorship, decides what an object is. Undo pops the carve; pixels back.
    strokes.push({ mode: 'load', img: cv, ox: r.bbox.x0, oy: r.bbox.y0,
                   w: cv.width, h: cv.height, erase: true,
                   kind: 'shape', edge: 0, grow: 0, shrink: 0, feather: 0,
                   size: 0, hardness: 1 });
    sel = -1; selDrag = null; _dragReg = null;
    redoStack = [];                                  // a deletion is a real edit; redo is stale
    syncInspector(); rasterize(); status('Object removed.');
  }

  function fillDisc(m, x, y, r) { m.beginPath(); m.arc(x, y, r, 0, 6.2832); m.fill(); }

  // One shared scratch buffer for a stroke's coverage shape, sized to the mask. Reused
  // rather than allocated per stroke because rasterize() replays every stroke on every
  // pointermove, and allocating W x H each time is what would make a long stroke stutter.
  var _sc = null;
  function scratch() {
    if (!_sc) _sc = document.createElement('canvas');
    if (_sc.width !== W || _sc.height !== H) { _sc.width = W; _sc.height = H; }
    return _sc;
  }

  function paintStroke(m, s) {
    // s.size is stored in NATURAL px (see down: size = brushR()*2). Do NOT multiply by
    // scale() here — that was the drift bug: a stroke painted at one display width would
    // re-render THICKER or THINNER after a resize/zoom because scale() is read live, so the
    // committed mask changed underneath the user. Natural px keeps the mask stable.
    var r = s.size / 2, hard = s.hardness;
    m.save();
    m.globalCompositeOperation = s.erase ? 'destination-out' : 'source-over';
    m.fillStyle = 'rgba(255,255,255,1)';
    m.strokeStyle = 'rgba(255,255,255,1)';
    var i, a, b;
    if (s.mode === 'load' && s.img) {
      // An auto-mask arrives as a ready PNG (white RGB + alpha coverage, the same
      // canonical shape masks.normalize() emits) and is committed as ONE stack entry, so
      // the guess stays erasable/undoable instead of becoming the ground truth.
      //
      // Paint the RAW coverage, do NOT bake grow/shrink/feather here. This canvas feeds
      // BOTH exportMask() and the region ship-copies, and the server re-applies each
      // object's edge + feather exactly once (masks.normalize_layers, KIND_RULES) —
      // morphing on top of the raw blit here would grow the object TWICE. Live feedback
      // is the display-only region copy (regionDispCanvas below), never exported; the
      // server overlay then paints identical pixels over it, so what the user approves
      // is what the graph will render.
      m.drawImage(s.img, s.ox || 0, s.oy || 0, s.w || W, s.h || H);
      m.restore();
      return;
    }
    if (s.mode === 'move' && s.cv) {
      // A COMPOUND region move (bakeMove): lift the blob's pixels where they stand and
      // lay them down at the offset — ONE replay entry, so Undo pops a whole move and
      // never half of one. destination-out with the blob's own footprint removes exactly
      // its pixels, even where other strokes' coverage lies underneath them.
      m.globalCompositeOperation = 'destination-out';
      m.drawImage(s.cv, s.ox || 0, s.oy || 0);
      m.globalCompositeOperation = 'source-over';
      m.drawImage(s.cv, (s.ox || 0) + s.dx, (s.oy || 0) + s.dy);
      m.restore();
      return;
    }
    if ((s.mode === 'poly' || s.mode === 'draw') && s.pts.length > 2) {
      // 'poly' = click-built polygon; 'draw' = freehand lasso trace. Both close+fill the
      // vertex list. (While a 'draw' lasso is still being traced, drawActiveOutline also
      // shows the live yellow outline, so the region fills in as you go.)
      m.beginPath(); m.moveTo(s.pts[0].x, s.pts[0].y);
      for (i = 1; i < s.pts.length; i++) m.lineTo(s.pts[i].x, s.pts[i].y);
      m.closePath(); m.fill();
    } else if (s.mode === 'rect' && s.pts.length > 1) {
      a = s.pts[0]; b = s.pts[s.pts.length - 1];
      m.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    } else if (s.mode === 'ellipse' && s.pts.length > 1) {
      a = s.pts[0]; b = s.pts[s.pts.length - 1];
      m.beginPath();
      m.ellipse((a.x + b.x) / 2, (a.y + b.y) / 2, Math.abs(b.x - a.x) / 2, Math.abs(b.y - a.y) / 2, 0, 0, 6.2832);
      m.fill();
    } else {
      // Freehand. The coverage shape is BUILT in a scratch buffer and composited ONCE, and
      // that ordering is the fix for the brush erasing its own stroke mid-drag. The feather
      // used to be punched onto the live mask with destination-out at every other point, but
      // destination-out MULTIPLIES the destination alpha down: consecutive points sit ~1px
      // apart while their discs are r wide, so each neighbour's falloff re-eroded the pixels
      // the last one had just painted, and the compounding carved holes along the centre of
      // the very line being drawn. (A fresh stroke also ate the older one, for the same
      // reason.) Building the shape first and compositing once makes a brush stroke
      // monotonic -- inside a stroke, painting can only ever ADD coverage.
      var pts = s.pts, n = pts.length, x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (i = 0; i < n; i++) {
        if (pts[i].x < x0) x0 = pts[i].x; if (pts[i].x > x1) x1 = pts[i].x;
        if (pts[i].y < y0) y0 = pts[i].y; if (pts[i].y > y1) y1 = pts[i].y;
      }
      var bx = Math.max(0, Math.floor(x0 - r)), by = Math.max(0, Math.floor(y0 - r));
      var bw = Math.min(W - bx, Math.ceil(x1 + r) - bx), bh = Math.min(H - by, Math.ceil(y1 + r) - by);
      if (bw <= 0 || bh <= 0) { m.restore(); return; }
      var sc = scratch(), c = sc.getContext('2d');
      // clear only this stroke's box: the buffer is shared, and a full W x H clear per
      // stroke per pointermove would dominate the frame budget on a long mask
      c.clearRect(bx, by, bw, bh);
      var core = Math.max(0.75, r * hard);
      c.save();
      c.fillStyle = 'rgba(255,255,255,1)';
      c.strokeStyle = 'rgba(255,255,255,1)';
      // Solid core: guarantees the centre line is fully painted whatever the pointer
      // sample rate, so a fast drag reads as a line and not a dotted one.
      for (i = 0; i < n; i++) fillDisc(c, pts[i].x, pts[i].y, core);
      c.lineWidth = core * 2; c.lineCap = 'round'; c.lineJoin = 'round';
      c.beginPath();
      for (i = 0; i < n; i++) { if (i === 0) c.moveTo(pts[i].x, pts[i].y); else c.lineTo(pts[i].x, pts[i].y); }
      if (n > 1) c.stroke();
      // Feather ring: source-over gradient discs, opaque at the core and transparent at r.
      // Overlapping neighbours ACCUMULATE coverage instead of subtracting it, which is the
      // whole difference between a soft brush and a hole punch.
      if (r - core > 0.5) {
        var step = n > 160 ? Math.ceil(n / 160) : 1;   // ~r/2 spacing is plenty
        for (i = 0; i < n; i += step) {
          var g = c.createRadialGradient(pts[i].x, pts[i].y, core, pts[i].x, pts[i].y, r);
          g.addColorStop(0, 'rgba(255,255,255,1)');
          g.addColorStop(1, 'rgba(255,255,255,0)');
          c.fillStyle = g; fillDisc(c, pts[i].x, pts[i].y, r);
        }
      }
      c.restore();
      // ONE composite, under the mode the caller chose: source-over to paint, destination-out
      // to erase. The eraser still lifts a previous stroke -- that is its job -- but it now
      // subtracts the stroke's coverage exactly once instead of once per overlapping disc.
      m.drawImage(sc, bx, by, bw, bh, bx, by, bw, bh);
    }
    m.restore();
  }


  /* ---------------- display-only morphology (region copies) -------------------------
   * #5 (generalised by the region model): the toolbar edge/feather sliders must give LIVE
   * feedback for EVERY object, not just the selected one, before the (debounced) server
   * overlay lands. regionDispCanvas morphs each region's committed coverage for DISPLAY
   * only; it is never written to maskC nor shipped by exportMask/exportLayers, so the
   * server still applies the real geometry exactly once — no double-grow (the bug a naive
   * client bake reintroduces). It mirrors masks._morph_disk / _morph / _feather_out so
   * what the sliders preview and what the server paints agree; when the server overlay
   * arrives it replaces these with identical pixels, so there is no flicker.
   */
  var _DISK_CAP = 256;   // display-only: the browser caps lower than the server (masks.DISK_CAP
                         // 2048) because this runs per slider-drag; it is never exported, so the
                         // render still uses the full-resolution server kernel.
  function _dt1d(f) {
    // Mirrors masks._dt1d EXACTLY, including the seed-awareness: only FINITE f entries are
    // seeds. Without the f[q] >= Infinity skip, an all-off line (every entry Infinity, which
    // happens for any smart-select object that does not span the full canvas width) makes
    // s = (Inf - Inf) / ... = NaN; NaN > z[k] is never true, so k walks below zero and this
    // loop never terminates -- that hung headless Chrome for the whole 180 s budget while the
    // server (which HAS this guard) sailed through the same mask. The two must stay in lockstep.
    var n = f.length; if (!n) return [];
    var INF = Infinity;
    var v = new Int32Array(n), z = new Float64Array(n + 1), d = new Float64Array(n);
    var k = -1, q, vk, s, kk;
    for (q = 0; q < n; q++) {
      if (f[q] >= INF) continue;                       // not a seed: contributes no parabola
      if (k < 0) { k = 0; v[0] = q; z[0] = -INF; z[1] = INF; continue; }
      var fq = f[q];
      for (;;) {
        vk = v[k];
        // q != vk (seeds only, q increasing) and every f[vk]/fq is finite, so s is finite.
        s = ((q * q + fq) - (vk * vk + f[vk])) / (2 * (q - vk));
        if (s > z[k]) break;                           // at k=0, z[0] = -Inf, so this always breaks
        k -= 1;
      }
      k += 1; v[k] = q; z[k] = s; z[k + 1] = INF;
    }
    if (k < 0) { for (q = 0; q < n; q++) d[q] = INF; return d; }   // whole line off: unbounded
    kk = 0;
    for (q = 0; q < n; q++) {
      while (z[kk + 1] < q) kk += 1;
      var dq = q - v[kk]; d[q] = dq * dq + f[v[kk]];
    }
    return d;
  }
  // on: Uint8Array (w*h, 1 = selected). Returns Uint8Array of the disc-dilated/eroded mask.
  function _edtOn(on, w, h) {
    var INF = Infinity, x, y, q;
    var col = new Float64Array(h), tmp = new Float64Array(w * h);
    for (x = 0; x < w; x++) {
      for (y = 0; y < h; y++) col[y] = on[y * w + x] ? 0 : INF;
      var dc = _dt1d(col);
      for (y = 0; y < h; y++) tmp[y * w + x] = dc[y];
    }
    var row = new Float64Array(w), out = new Float64Array(w * h);
    for (y = 0; y < h; y++) {
      for (x = 0; x < w; x++) row[x] = tmp[y * w + x];
      var dr = _dt1d(row);
      for (x = 0; x < w; x++) out[y * w + x] = dr[x];
    }
    return out;
  }
  function _discOn(on, w, h, r, grow) {
    if (r <= 0) return on;
    var D = _edtOn(on, w, h), out = new Uint8Array(w * h), i;
    if (grow) {
      var r2 = r * r + 0.25;
      for (i = 0; i < w * h; i++) out[i] = (on[i] || D[i] <= r2) ? 1 : 0;
    } else {
      // erode: stay on iff nearest OFF pixel is farther than r
      var off = new Uint8Array(w * h);
      for (i = 0; i < w * h; i++) off[i] = on[i] ? 0 : 1;
      var Do = _edtOn(off, w, h);
      for (i = 0; i < w * h; i++) out[i] = (on[i] && Do[i] > r * r) ? 1 : 0;
    }
    return out;
  }

  /* ---------------- region rebuild: pixels -> objects ----------------
   * Two-pass connected-components (union-find, 8-connected - the same connectivity
   * masks.keep_significant_components uses server-side, so client identity and the
   * server's de-speckle never disagree about what one blob is). Identity is read off
   * the COMMITTED composited pixels, never off the displayed morphology: grow/feather
   * are adjustments on top, and letting them decide identity would merge objects the
   * moment a slider drag made their skirts touch - renumbering the user's objects
   * mid-gesture.
   *
   * Parameter carry-over by ancestor overlap (a new blob inherits from the old blobs
   * whose pixels it contains, when the overlap clears a floor of ~20% of either side -
   * a 3-px brush touch must not silently renumber a stranger object, and a bridge
   * stroke joining two big blobs must count BOTH):
   *   1 ancestor  -> inherit kind/edge/feather. A brush touch onto a smart-select
   *                 makes the brushed pixels PART of that object (one adjuster for the
   *                 whole blob), and an erase that trims or SPLITS a blob leaves
   *                 fragments that retain the parent's edge and feather - the feather
   *                 across the fresh cut is what hides the seam of the split.
   *   2+ ancestors-> MERGE: edge resets to 0 (the new object starts at its actual
   *                 size and grows from there - averaging two offsets double-counts),
   *                 feather = the ancestors' overlap-weighted average, kind prefers
   *                 'auto' then 'shape' then 'brush'.
   *   0 ancestors -> brand-new blob: kind from the committing stroke's hint, edge 0,
   *                 feather default read from the sliders AT COMMIT (the sliders are
   *                 defaults for the NEXT object - the contract stamp() has honoured).
   */
  function rebuildRegions() {
    regionRev++;
    var n = W * H;
    if (!maskA || maskA.length !== n) maskA = new Uint8Array(n);
    var m = maskC.getContext('2d', { willReadFrequently: true });
    var data;
    try { data = m.getImageData(0, 0, W, H).data; }
    catch (e) { compLabel = null; regions = []; sel = -1; return; }   // tainted: overlay still rules
    var i, x, y;
    for (i = 0; i < n; i++) maskA[i] = data[i * 4 + 3];
    var prevLabel = compLabel, prevRegs = regions;
    compLabel = new Int32Array(n);
    var parent = new Int32Array(n + 1);
    for (i = 0; i <= n; i++) parent[i] = i;
    function find(a) { while (parent[a] !== a) { parent[a] = parent[parent[a]]; a = parent[a]; } return a; }
    function join(a, b) { a = find(a); b = find(b); if (a !== b) { if (a < b) parent[b] = a; else parent[a] = b; } }
    var lab = compLabel;
    for (y = 0; y < H; y++) {
      for (x = 0; x < W; x++) {
        i = y * W + x;
        if (maskA[i] <= REGION_ALPHA_MIN) continue;
        var l = x > 0 ? lab[i - 1] : 0;
        var u = y > 0 ? lab[i - W] : 0;
        var ul = (x > 0 && y > 0) ? lab[i - W - 1] : 0;
        var ur = (x < W - 1 && y > 0) ? lab[i - W + 1] : 0;
        if (!l && !u && !ul && !ur) { lab[i] = i + 1; continue; }
        var mn = l || u || ul || ur;   // (a diagonal-only neighbour made mn 0 and merged the whole blob into the background)
        if (ul && ul < mn) mn = ul;
        if (ur && ur < mn) mn = ur;
        lab[i] = mn;
        if (l) join(mn, l);
        if (u) join(mn, u);
        if (ul) join(mn, ul);
        if (ur) join(mn, ur);
      }
    }
    var stats = {}, order = [];
    for (i = 0; i < n; i++) {
      var a = lab[i];
      if (!a) continue;
      var root = find(a);
      lab[i] = root;
      var st = stats[root];
      if (!st) { st = stats[root] = { area: 0, x0: W, y0: H, x1: -1, y1: -1, anc: null }; order.push(root); }
      var px = i % W, py = (i - px) / W;
      st.area++;
      if (px < st.x0) st.x0 = px;
      if (px > st.x1) st.x1 = px;
      if (py < st.y0) st.y0 = py;
      if (py > st.y1) st.y1 = py;
      if (prevLabel) {
        var pa = prevLabel[i];
        if (pa) { if (!st.anc) st.anc = {}; st.anc[pa] = (st.anc[pa] || 0) + 1; }
      }
    }
    order.sort(function (p, q) { return p - q; });       // scan order == paint-order proxy
    var selReg = (sel >= 0 && sel < prevRegs.length) ? prevRegs[sel] : null;
    regions = [];
    var newSel = -1;
    var rootSlot = {};
    for (var oi = 0; oi < order.length && oi < MAX_REGIONS; oi++) {
      var st2 = stats[order[oi]];
      var q = null;
      if (st2.anc && prevRegs.length) {
        q = [];
        for (var key in st2.anc) {
          var old = prevRegs[(key | 0) - 1];
          if (!old) continue;
          var cnt = st2.anc[key];
          if (cnt >= 6 && (cnt >= 0.2 * st2.area || cnt >= 0.2 * (old.area || 1))) q.push({ old: old, cnt: cnt });
        }
      }
      var kind, edge, feather;
      if (q && q.length === 1) {
        kind = q[0].old.kind; edge = q[0].old.edge; feather = q[0].old.feather;
      } else if (q && q.length > 1) {
        var ws = 0, wf = 0, sawAuto = false, sawShape = false;
        for (var qi = 0; qi < q.length; qi++) {
          ws += q[qi].cnt; wf += q[qi].cnt * (q[qi].old.feather || 0);
          if (q[qi].old.kind === 'auto') sawAuto = true;
          else if (q[qi].old.kind === 'shape') sawShape = true;
        }
        feather = ws ? Math.round(wf / ws) : 0;
        edge = 0;
        kind = sawAuto ? 'auto' : (sawShape ? 'shape' : 'brush');
      } else {
        kind = commitHintKind || 'shape';
        edge = kind === 'brush' ? 0 : val('tb_edge', 0);
        feather = kind === 'brush' ? 0 : val('tb_feather', 8);
      }
      var reg = { id: regionSeq++, kind: kind, edge: edge, feather: feather,
                  area: st2.area, mvx: 0, mvy: 0,
                  bbox: { x0: st2.x0, y0: st2.y0, x1: st2.x1, y1: st2.y1 } };
      if (q) for (var qj = 0; qj < q.length; qj++) {
        if (q[qj].old === selReg) newSel = regions.length;
        if (_dragReg && q[qj].old === _dragReg) { reg.mvx = _dragReg.mvx; reg.mvy = _dragReg.mvy; }
      }
      regions.push(reg);
      rootSlot[order[oi]] = regions.length - 1;   // the slot becomes the exported label
    }
    // Remap the union-find ROOTS (seed pixel ids — huge, meaningless numbers) down to
    // region slot+1. Every consumer (hitTest, the ship-copies, the display morphs) reads
    // compLabel as slot+1; shipping raw roots left every per-region canvas silently
    // empty, because no pixel's root ever equalled a small slot number.
    if (regions.length) {
      for (i = 0; i < n; i++) {
        var rw = lab[i];
        if (!rw) { continue; }
        var sl = rootSlot[rw];
        lab[i] = (sl === undefined) ? 0 : sl + 1;
      }
    }
    sel = newSel;
  }

  // Square-kernel binary morph — mirrors masks._morph (server-side, shapes use the
  // square kernel; only 'auto' gets the disc, and the client display mirrors exactly
  // which kind gets which). Sliding-window OR over the box, two separable passes.
  function _boxOn(on, w, h, r, grow) {
    if (r <= 0) return on;
    var tmp = new Uint8Array(w * h), out = new Uint8Array(w * h);
    var src = on, x, y, k;
    if (!grow) {
      src = new Uint8Array(w * h);
      for (k = 0; k < w * h; k++) src[k] = on[k] ? 0 : 1;      // erode = no OFF in window
    }
    for (y = 0; y < h; y++) {
      var row = y * w, run = 0;
      for (k = 0; k <= Math.min(w - 1, r); k++) run += src[row + k];
      for (x = 0; x < w; x++) {
        tmp[row + x] = run > 0 ? 1 : 0;
        if (x + 1 + r < w) run += src[row + x + 1 + r];
        if (x - r >= 0) run -= src[row + x - r];
      }
    }
    for (x = 0; x < w; x++) {
      var run2 = 0;
      for (k = 0; k <= Math.min(h - 1, r); k++) run2 += tmp[k * w + x];
      for (y = 0; y < h; y++) {
        out[y * w + x] = run2 > 0 ? 1 : 0;
        if (y + 1 + r < h) run2 += tmp[(y + 1 + r) * w + x];
        if (y - r >= 0) run2 -= tmp[(y - r) * w + x];
      }
    }
    if (grow) return out;
    var res = new Uint8Array(w * h);
    for (k = 0; k < w * h; k++) res[k] = (on[k] && !out[k]) ? 1 : 0;
    return res;
  }

  function regionGeom(r) {
    var pad = Math.max(0, (r.edge || 0)) + (r.feather || 0) + 2;
    var px0 = Math.max(0, r.bbox.x0 - pad), py0 = Math.max(0, r.bbox.y0 - pad);
    var px1 = Math.min(W - 1, r.bbox.x1 + pad), py1 = Math.min(H - 1, r.bbox.y1 + pad);
    return { px0: px0, py0: py0, w: px1 - px0 + 1, h: py1 - py0 + 1 };
  }

  // The blob's OWN pixels. binary=true lifts the whole object (destination-out cuts
  // must be total); binary=false keeps the composited alpha so a pure-brush object
  // ships its hardness gradient (masks.KIND_RULES['brush'] forbids the server from
  // touching it, so nothing here may pre-morph the pixels either — the double-grow
  // bug this whole file has been bitten by once).
  function regionRawCanvas(r, binary) {
    var ck = binary ? '_cBin' : '_cShip';
    var key = regionRev + '|' + binary + '|' + r.bbox.x0 + ',' + r.bbox.y0 + ',' + r.bbox.x1 + ',' + r.bbox.y1;
    if (r[ck + 'Key'] === key) return r[ck];
    var id = regions.indexOf(r) + 1;
    var w = r.bbox.x1 - r.bbox.x0 + 1, h = r.bbox.y1 - r.bbox.y0 + 1;
    var c = document.createElement('canvas'); c.width = w; c.height = h;
    var cx = c.getContext('2d');
    var im = cx.createImageData(w, h), d = im.data;
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        var g = (y + r.bbox.y0) * W + (x + r.bbox.x0);
        if (compLabel && compLabel[g] === id) {
          var o = (y * w + x) * 4;
          d[o] = 255; d[o + 1] = 255; d[o + 2] = 255;
          d[o + 3] = binary ? 255 : maskA[g];
        }
      }
    }
    cx.putImageData(im, 0, 0);
    r[ck] = c; r[ck + 'Key'] = key;
    return c;
  }

  // The blob's DISPLAY copy: its own signed edge and outward feather, applied with the
  // server's own operators (disc for auto, square for shape, nothing for brush). This
  // is why every object stays live on screen whatever the Select tool points at — the
  // bug it replaces only ever showed the SELECTED object's morphology, so a just-tuned
  // object snapped back to raw geometry the moment you reached for the next one.
  // Cached per (pixel generation, kind, edge, feather, bbox): a slider drag recomputes
  // only the object being dragged. Grid capped at _DISK_CAP — display only, never
  // exported; the server overlay replaces these pixels with identical ones.
  function regionDispCanvas(r) {
    var key = regionRev + '|' + r.kind + '|' + r.edge + '|' + r.feather + '|'
            + r.bbox.x0 + ',' + r.bbox.y0 + ',' + r.bbox.x1 + ',' + r.bbox.y1;
    if (r._cDispKey === key) return r._cDisp;
    var dg = regionGeom(r);
    r._dg = dg;
    var w = dg.w, h = dg.h, id = regions.indexOf(r) + 1, x, y, k;
    var c = document.createElement('canvas'); c.width = w; c.height = h;
    var cx = c.getContext('2d');
    if (r.kind === 'brush') {                       // hardness IS the edge: raw pixels
      cx.drawImage(regionRawCanvas(r, false), r.bbox.x0 - dg.px0, r.bbox.y0 - dg.py0);
      r._on = null;
      r._cDisp = c; r._cDispKey = key;
      return c;
    }
    var on = new Uint8Array(w * h);
    for (y = 0; y < h; y++) {
      for (x = 0; x < w; x++) {
        var g = (y + dg.py0) * W + (x + dg.px0);
        on[y * w + x] = (compLabel && compLabel[g] === id && maskA[g] > REGION_ALPHA_MIN) ? 1 : 0;
      }
    }
    var sd = Math.min(1, _DISK_CAP / Math.max(1, Math.max(w, h)));
    var e = Math.round(r.edge || 0);
    if (e) on = (r.kind === 'auto' ? _discOn : _boxOn)(on, w, h, Math.max(1, Math.round(Math.abs(e) * sd)), e > 0);
    var im = cx.createImageData(w, h), d = im.data;
    for (k = 0; k < w * h; k++) {
      if (on[k]) { var o = k * 4; d[o] = 255; d[o + 1] = 255; d[o + 2] = 255; d[o + 3] = 255; }
    }
    cx.putImageData(im, 0, 0);
    var f = Math.round(r.feather || 0);
    if (f > 0) {
      // outward feather: solid core UNION blurred grown copy — the >=128 footprint can
      // only match or exceed the hard silhouette the user approved (masks._feather_out).
      var grown = (r.kind === 'auto' ? _discOn : _boxOn)(on, w, h, Math.max(1, Math.round(f * sd)), true);
      var gc = document.createElement('canvas'); gc.width = w; gc.height = h;
      var gx = gc.getContext('2d');
      var gi = gx.createImageData(w, h), gd = gi.data;
      for (k = 0; k < w * h; k++) if (grown[k]) { var oo = k * 4; gd[oo] = 255; gd[oo + 1] = 255; gd[oo + 2] = 255; gd[oo + 3] = 255; }
      gx.putImageData(gi, 0, 0);
      var bc = document.createElement('canvas'); bc.width = w; bc.height = h;
      var bx = bc.getContext('2d');
      bx.filter = 'blur(' + Math.max(0.5, f * sd) + 'px)';
      bx.drawImage(gc, 0, 0);
      cx.globalCompositeOperation = 'lighter';
      cx.drawImage(bc, 0, 0);
      cx.globalCompositeOperation = 'source-over';
    }
    r._on = on;
    r._cDisp = c; r._cDispKey = key;
    return c;
  }

  // A pure parameter change (edge/feather): pixels untouched, view + preview cache updated.
  function touchObject() { compose(); schedulePreview(); syncInspectorSoon(); }

  function commitStroke(s) {
    s = stamp(s);                       // slider defaults read AT COMMIT, as ever
    commitHintKind = s.kind === 'brush' ? 'brush' : (s.kind === 'auto' ? 'auto' : 'shape');
    strokes.push(s);
    redoStack = [];
    return s;
  }

  /* ---------------- compositing (what the user sees) ---------------- */
  // Default: the mask as a translucent red wash over the photo — the SAME thing the
  // server's dry-run overlay (masks.overlay) draws, so local + authoritative agree in
  // colour. When a server preview is fresh (its sig matches the current paint+params),
  // we show THAT image instead, because it carries the feather and grow/shrink the
  // browser cannot — so what you see is genuinely what will be painted, not raw strokes.
  function compose() {
    if (!viewC || !baseC) return;
    var v = viewC.getContext('2d');
    v.clearRect(0, 0, W, H);
    v.drawImage(baseC, 0, 0);
    if (selObj()) drawSelection(v);          // before the early return, or the box vanishes
    if (preview && preview.img && preview.sig === previewSig()) {
      v.drawImage(preview.img, 0, 0, W, H);
      drawActiveOutline(v);
      var s0 = selObj(); if (s0) drawActiveAutoOutline(v, s0);
      return;
    }
    // Local wash: EVERY region is drawn with its OWN edge/feather — the whole point of
    // the region model. The bug this replaces showed the morph of only the SELECTED
    // object, so a just-tuned selection snapped back to raw geometry the moment you
    // reached for the next one. The server overlay, when it lands, paints identical
    // pixels over these (same operators, same numbers), so this copy never ships.
    var wash = document.createElement('canvas');
    wash.width = W; wash.height = H;
    var wc = wash.getContext('2d');
    for (var i = 0; i < regions.length; i++) {
      var r = regions[i];
      var disp = regionDispCanvas(r), dg = r._dg;
      wc.drawImage(disp, dg.px0 + (r.mvx || 0), dg.py0 + (r.mvy || 0));
    }
    if (active) paintStroke(wc, active);   // the in-progress shape, at full liveness
    wc.globalCompositeOperation = 'source-in';
    wc.fillStyle = 'rgba(232,62,62,' + val('tb_wash', 0.45) + ')';
    wc.fillRect(0, 0, W, H);
    v.drawImage(wash, 0, 0);
    var so = selObj();
    if (so) drawActiveAutoOutline(v, so);
    drawActiveOutline(v);
  }

  // #4: the SELECTED region gets a bright cyan ring on its MORPHED edge (the same disc/
  // square grow the wash and the server use), so the ring tracks the edge slider and
  // reads unmistakably as "this is the one the sliders are driving".
  function drawActiveAutoOutline(v, o) {
    try {
      var on = o._on, dg = o._dg;
      if (!on) {
        // brush blobs keep their raw silhouette (no _on grid): binarise the ship copy
        // for the ring. A brush edge is handwork, so the ring traces what it ships.
        var rc0 = regionRawCanvas(o, true);
        var pw = rc0.width, ph = rc0.height;
        if (!pw || !ph) return;
        var pc = rc0.getContext('2d', { willReadFrequently: true });
        var pd = pc.getImageData(0, 0, pw, ph).data;
        on = new Uint8Array(pw * ph);
        for (var t = 0; t < pw * ph; t++) on[t] = pd[t * 4 + 3] > 50 ? 1 : 0;
        dg = { px0: o.bbox.x0, py0: o.bbox.y0, w: pw, h: ph };
      }
      var ring = _boundaryRing(on, dg.w, dg.h);
      var rc = document.createElement('canvas'); rc.width = dg.w; rc.height = dg.h;
      var rx = rc.getContext('2d');
      var ri = rx.createImageData(dg.w, dg.h), rd = ri.data;
      for (var j = 0; j < ring.length; j++) {
        if (ring[j]) { var q = j * 4; rd[q] = 60; rd[q + 1] = 220; rd[q + 2] = 255; rd[q + 3] = 255; }
      }
      rx.putImageData(ri, 0, 0);
      v.save();
      v.globalAlpha = 0.95;
      v.drawImage(rc, dg.px0 + (o.mvx || 0), dg.py0 + (o.mvy || 0));
      v.restore();
    } catch (e) { /* readback/taint: skip the outline, the wash still shows the selection */ }
  }
  function _boundaryRing(on, w, h) {
    var off = new Uint8Array(w * h), i;
    for (i = 0; i < w * h; i++) off[i] = on[i] ? 0 : 1;
    var Do = _edtOn(off, w, h), ring = new Uint8Array(w * h);
    for (i = 0; i < w * h; i++) ring[i] = (on[i] && Do[i] <= 1) ? 1 : 0;   // edge band
    return ring;
  }

  // The in-progress shape, drawn in natural coords so it scales with the view for free.
  // Covers the freehand lasso trace ('draw'), the click-built polygon ('poly'), rect and
  // ellipse — every tool now previews while it builds, which the lasso previously did not.
  function drawSelection(v) {
    var o = selObj(); if (!o) return;
    var b = objBBox(o); if (!b) return;
    // Pixel objects: the dashed box is an indicator, not a manipulator. The corner SCALE
    // handle retired with the vector model — a blob that may BE a merge of several
    // strokes has no "vector to resize", and an affordance that scales half of what the
    // user sees is exactly the dead-control lie this project's greyed-knobs rule exists
    // to prevent. Drag anywhere inside the object to move its pixels.
    v.save();
    v.strokeStyle = '#3ba7ff'; v.lineWidth = Math.max(1.5, 2 * scale());
    v.setLineDash([6 * scale(), 4 * scale()]);
    v.strokeRect(b.x0 + (o.mvx || 0), b.y0 + (o.mvy || 0),
                 b.x1 - b.x0 + 1, b.y1 - b.y0 + 1);
    v.setLineDash([]);
    v.restore();
  }

  function drawActiveOutline(v) {
    if (!active || !active.pts.length || active.mode === 'load') return;
    v.save();
    v.strokeStyle = '#ffd54f'; v.lineWidth = Math.max(1, 2 * scale());
    v.fillStyle = 'rgba(255,213,79,0.18)';
    var closed = active.mode === 'draw' || active.mode === 'poly';
    v.beginPath();
    v.moveTo(active.pts[0].x, active.pts[0].y);
    for (var i = 1; i < active.pts.length; i++) v.lineTo(active.pts[i].x, active.pts[i].y);
    if (active.mode === 'rect' && active.pts.length > 1) {
      var a = active.pts[0], b = active.pts[active.pts.length - 1];
      v.rect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
    } else if (active.mode === 'ellipse' && active.pts.length > 1) {
      var c = active.pts[0], d = active.pts[active.pts.length - 1];
      v.ellipse((c.x + d.x) / 2, (c.y + d.y) / 2,
                Math.abs(d.x - c.x) / 2, Math.abs(d.y - c.y) / 2, 0, 0, 6.2832);
    } else if (closed) { v.closePath(); }
    if (closed && active.pts.length > 2) v.fill();
    v.stroke();
    if (active.mode === 'poly') {          // place-markers for the click-built polygon
      v.fillStyle = '#fff';
      for (var j = 0; j < active.pts.length; j++) {
        v.beginPath(); v.arc(active.pts[j].x, active.pts[j].y, Math.max(2, 3 * scale()), 0, 6.2832); v.fill();
      }
    }
    v.restore();
  }

  /* ---------------- undo (vector, so it costs nothing) ---------------- */
  // Strokes are a stack, so undo/redo is push/pop — no pixel snapshots. Each drops any
  // in-progress shape first (`active`): the lasso/polygon leaves one until it closes, and
  // an undo that ignored it would pop a committed stroke while the half-built shape stayed
  // on screen, which is exactly why the buttons looked dead before. redoStack is the
  // redo pile; clear empties both and the active shape.
  function undo() {
    active = null;
    if (!strokes.length) { rasterize(); return; }
    redoStack.push(strokes.pop());
    rasterize(); status('Undo');
  }
  function syncInspectorSoon() { if (el_inspect) syncInspector(); }

  function redoStep() {
    active = null;
    if (!redoStack.length) { rasterize(); return; }
    strokes.push(redoStack.pop());
    rasterize(); status('Redo');
  }
  function clearMask() {
    strokes = []; redoStack = []; active = null; preview = null; sel = -1; selDrag = null;
    regions = []; compLabel = null; maskA = null; _dragReg = null;
    smartObjs = []; smartCur = -1; smartDrag = null;   // a cleared canvas has no object to refine
    rasterize(); status('Mask cleared');
  }
  // Invert is a mask *semantic*, so it travels as a parameter instead of being baked
  // into the painted alpha: the graph already wires an InvertMask node
  // (workflows.MASK_INVERT_NODE), and inverting pixels here would make the
  // feather/expand maths disagree with the preview the user approved.
  function inverted() { return !!(el.inv && el.inv.checked); }

  /* ---------------- network ---------------- */
  function req(path, body, tok) {
    if (!API) return Promise.reject(new Error('no api base configured'));
    // Reads (preview/auto/echo) and the create itself send the LAUNCH token; poll/cancel
    // send the JOB-scope token the create response minted, passed via `tok`. They are NOT
    // interchangeable: create redeems the launch token single-use, and polling is a read
    // that must never be able to redeem anything, so it has its own job-bound token.
    var tk = tok || TOKEN;
    var ctl = null;
    try { ctl = new AbortController(); } catch (e) { /* ancient engines */ }
    var timer = ctl && setTimeout(function () { ctl.abort(); }, 30000);
    return fetch(API + path, {
      method: 'POST',
      // text/plain, not application/json: a srcdoc iframe is an opaque origin, so this
      // is a cross-origin POST and application/json would force a preflight OPTIONS the
      // plain-stdlib front would have to answer. Same body either way.
      headers: Object.assign({ 'content-type': 'text/plain' },
                             tk ? { 'authorization': 'Bearer ' + tk } : {}),
      body: JSON.stringify(body || {}),
      signal: ctl ? ctl.signal : undefined
    }).then(function (r) {
      if (timer) clearTimeout(timer);
      if (!r.ok) return r.text().then(function (t) { throw new Error(r.status + ' ' + String(t).slice(0, 200)); });
      return r.json();
    });
  }

  function exportMask() {
    if (!maskC) return null;
    return (maskC.toDataURL('image/png').split(',')[1]) || null;   // base64, no data: prefix
  }

  // The mask as per-object LAYERS — one PNG per connected REGION, in scan order.
  // Contiguity already decided who the objects are: a brush touch that landed on a
  // smart-select ships INSIDE that object's layer (one object, one edge/feather for the
  // server to apply), and an erase that cut a blob in two ships two layers. Erase
  // strokes never cross the wire — their effect is already baked into which pixels are
  // left. Nothing here morphs: the server re-applies each layer's edge/feather exactly
  // once (masks.normalize_layers, KIND_RULES), and the double-grow that comment exists
  // to prevent is only avoided if the ship copy stays RAW pixels.
  function exportLayers() {
    if (!maskC || !regions.length) return null;
    var out = [];
    for (var i = 0; i < regions.length; i++) {
      var r = regions[i];
      if (!r.area) continue;
      var curParams = { kind: r.kind, edge: r.edge || 0,
                        grow: (r.edge || 0) > 0 ? (r.edge || 0) : 0,
                        shrink: (r.edge || 0) < 0 ? -(r.edge || 0) : 0,
                        feather: r.feather || 0, erase: false };
      var curKey = [curParams.kind, curParams.edge, curParams.grow, curParams.shrink,
                    curParams.feather, r.bbox.x0, r.bbox.y0, r.area].join('|');
      if (r._ck === curKey && r._cp) { out.push(r._cp); continue; }   // pixels+params unchanged
      // WIRE CONTRACT (learned the hard way from the reported "one smart select and
      // the entire image is the mask"): a layer PNG is a FULL-CANVAS raster of that
      // object — the server's masks._layer_coverage decodes the PNG and resamples it
      // whenever its size disagrees with the working canvas, which for a bbox crop
      // means LANCZOS-STRETCHING the tight crop across the WHOLE photo: a 10% blob
      // inflates into a full-frame mask, in the preview overlay AND the render. The
      // bbox-cropped ship copy is an export STAGE only; it must be pasted back at its
      // offset onto a full-size canvas before the encode.
      var fc = document.createElement('canvas'); fc.width = W; fc.height = H;
      fc.getContext('2d').drawImage(regionRawCanvas(r, false), r.bbox.x0, r.bbox.y0);
      var png = fc.toDataURL('image/png').split(',')[1];
      if (!png) continue;
      r._ck = curKey;
      // curParams, NOT curKey: curKey is the opaque comparison STRING built FROM these
      // values. Reading .kind off it yields undefined, which the server then maps onto
      // the strictest rule — a bug that looks like "the brush got hard-edged again".
      r._cp = { png: png,
                 kind: curParams.kind, edge: curParams.edge,
                 grow: curParams.grow, shrink: curParams.shrink,
                 feather: curParams.feather, erase: !!curParams.erase };
      out.push(r._cp);
    }
    return out.length ? out : null;
  }

  function int0(id, dflt) {
    var n = parseInt(($(id) || {}).value, 10);
    return isFinite(n) && n > 0 ? n : dflt;
  }
  // The working dims the server should resample to. Sent as a request, NEVER trusted as
  // truth: masks.normalize() resamples to whatever dims the job actually submits, so a
  // disagreement here costs quality, never correctness.
  function params() {
    return {
      kind: el.kind ? el.kind.value : 'edit',
      prompt: el.prompt ? el.prompt.value : '',
      strength: val('tb_strength', 0.25),
      // Derived, never independent: one bipolar knob is the only way to express this, so the
      // flat-mask path (spike, older editors) cannot enter the both-set closing state either.
      mask_edge: val('tb_edge', 0),
      mask_expand: Math.max(0, val('tb_edge', 0)),
      mask_shrink: Math.max(0, -val('tb_edge', 0)),
      mask_feather: val('tb_feather', 8),
      invert: inverted(),
      // Cosmetic read-strength of the mask overlay (the 'overlay' slider). Sent to the
      // preview so the SERVER bakes the tint at the chosen alpha; the client cannot fade
      // the server's already-composited image without also fading the photo underneath.
      // Never reaches the render's mask maths.
      overlay_alpha: val('tb_wash', 0.45),
      opacity: val('tb_opacity', 1.0),
      blend_mode: el.blend ? el.blend.value : 'normal',
      color_match: val('tb_colormatch', 0.9),
      preserve_detail: val('tb_detail', 0.35),
      variants: int0('tb_variants', 1),
      seed: int0('tb_seed', -1),
      width: int0('tb_w', W), height: int0('tb_h', H)
    };
  }

  /* ---------------- UI ----------------
   * Built here rather than in the HTML shell so the control ids can never drift from
   * params(): one file owns both ends of every control. */
  function mk(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }
  function row(cls) { return mk('div', 'tb-row' + (cls ? ' ' + cls : '')); }

  function ctl(id, type, value, label, attrs) {
    var wrap = mk('label', 'tb-ctl');
    wrap.appendChild(mk('span', 'tb-lbl', label));
    var i = document.createElement('input');
    i.type = type; i.id = 'tb_' + id;
    // Attributes FIRST, value LAST, and this ordering is load-bearing. A range input
    // defaults to min=0, max=100, step=1, and its value is snapped to the step at the
    // moment it is assigned — so setting .value before the min/max/step attributes snaps
    // 0.45 to 0 and 0.55 to 1, and applying the attributes afterwards does NOT re-snap an
    // already-set value. That silently zeroed the mask overlay (wash 0.45 -> 0, so the
    // painted mask was invisible until the server preview arrived), pinned hardness at 1
    // and drove strength to 0. It was only visible in a real browser, not offline.
    for (var k in (attrs || {})) { if (Object.prototype.hasOwnProperty.call(attrs, k)) i.setAttribute(k, attrs[k]); }
    i.value = value;
    var out = mk('span', 'tb-num', String(value));
    // When a shape or auto REGION is selected, the edge/feather sliders must edit THAT
    // object: per-object geometry is what the layered server path normalises (see
    // masks.normalize_layers + api h_mask_preview), and the global mask_expand/feather
    // these sliders otherwise feed are IGNORED once layers exist. Brush blobs are
    // excluded — their edge is hardness, chosen by hand, and masks.KIND_RULES forbids
    // any remote number from moving it; a slider that silently did nothing is the dead
    // knob this project refuses to ship. An unselected canvas keeps the old meaning of
    // these controls: defaults for the NEXT object.
    i.addEventListener('input', function () {
      out.textContent = i.value;
      var o = selObj();
      if ((id === 'edge' || id === 'feather') && o && o.kind && o.kind !== 'brush') {
        var v = parseFloat(i.value) || 0;
        if (id === 'edge') { o.edge = v; } else { o.feather = v; }
        touchObject();                 // parameters only: pixels untouched, view + cache refresh
        return;
      }
      refresh();
    });
    wrap.appendChild(i); wrap.appendChild(out);
    el[id] = i; el[id + '_out'] = out;   // *_out lets selectObj mirror the object's numbers in
    return wrap;
  }
  function toggle(id, label, on) {
    var w = mk('label', 'tb-ctl tb-inline');
    var i = document.createElement('input');
    i.type = 'checkbox'; i.id = 'tb_' + id; i.checked = !!on;
    i.addEventListener('change', refresh);
    w.appendChild(i); w.appendChild(mk('span', 'tb-lbl', label));
    el[id] = i;
    return w;
  }
  function btn(label, fn, title) {
    var b = mk('button', 'tb-btn', label);
    b.type = 'button';
    if (title) b.title = title;
    b.addEventListener('click', function (e) { e.preventDefault(); fn(); });
    return b;
  }
  function select(id, opts) {
    var s = document.createElement('select'); s.id = 'tb_' + id;
    for (var i = 0; i < opts.length; i++) {
      var op = document.createElement('option');
      // opts[i] is [value, label] OR just [value]. Reading [1] unconditionally made every
      // label undefined whenever a caller passed bare values -- which is how the blend
      // dropdown shipped as a row of blank options. Fall back to the value, never blank.
      op.value = opts[i][0];
      op.textContent = (opts[i].length > 1 && opts[i][1] != null) ? opts[i][1] : opts[i][0];
      s.appendChild(op);
    }
    el[id] = s;
    return s;
  }
  function labelled(label, node) {
    var r = row(); r.appendChild(mk('span', 'tb-lbl', label)); r.appendChild(node); return r;
  }

  function setTool(t, msg) {
    tool = t;
    var g = el.tools.getElementsByTagName('button');
    for (var i = 0; i < g.length; i++) g[i].classList.toggle('tb-on', g[i].getAttribute('data-tool') === t);
    // Each shape tool starts its own in-progress buffer; brush/eraser start nothing (their
    // active is created on pointerdown). A fresh 'draw' (freehand lasso) or 'poly'
    // (click-built) holds the points until it is closed, so switching tools discards the
    // half-built one rather than leaking an invisible ghost onto the next stroke.
    active = null;
    selDrag = null;
    if (t !== 'select' && sel >= 0) { sel = -1; syncInspector(); }
    if (t === 'lasso') active = { mode: 'draw', pts: [], size: 0, hardness: 1, erase: false };
    else if (t === 'polygon') active = { mode: 'poly', pts: [], size: 0, hardness: 1, erase: false };
    else if (t === 'smart') msg = msg || 'Smart select — click an object to select it whole; shift+click adds, alt/ctrl+click removes. Drag a loop: Auto snaps it to the object, Manual keeps the exact pixels.';
    if (el.smartmode) el.smartmode.disabled = (t !== 'smart');   // the loop mode only means something here
    if (t !== 'smart') smartDrag = null;                          // never leak a half-traced loop into another tool
    // brush/eraser hide the OS cursor so the drawn ring is the only cursor — otherwise a
    // crosshair and the size ring fight each other and the size is still ambiguous.
    if (viewC) viewC.style.cursor = (t === 'brush' || t === 'eraser') ? 'none'
      : (t === 'select' ? 'default' : 'crosshair');
    status(msg || ('Tool: ' + t));
  }

  function buildUI(root) {
    el.tools = row('tb-tools');
    function tbtn(t, label, title) {
      var b = btn(label, function () { setTool(t); }, title);
      b.setAttribute('data-tool', t);
      return b;
    }
    el.tools.appendChild(tbtn('select', 'Select', 'Tap an object to move, scale or re-tune it'));
    el.tools.appendChild(tbtn('brush', 'Brush', 'Paint the area to change (B)'));
    el.tools.appendChild(tbtn('eraser', 'Eraser', 'Erase from the mask (E)'));
    el.tools.appendChild(tbtn('lasso', 'Lasso', 'Freehand: drag a loop around the area (L)'));
    el.tools.appendChild(tbtn('polygon', 'Polygon', 'Click points around the area, then Close (P)'));
    el.tools.appendChild(tbtn('smart', 'Smart select', 'Click an object to select the whole thing (SAM3). Shift+click adds to it, Alt/Ctrl+click subtracts. Drag a loop to lasso-select.'));
    // The lasso mode only matters while the Smart tool is up, but keeping it visible (and
    // greyed otherwise) means one glance always says what a smart drag will DO — Auto snaps
    // the loop to the real SAM3 boundary, Manual commits exactly the pixels traced.
    el.smartmode = btn('Loop: Auto', function () {
      smartAuto = !smartAuto;
      el.smartmode.textContent = smartAuto ? 'Loop: Auto' : 'Loop: Manual';
      status(smartAuto ? 'Smart loop = Auto: SAM3 snaps your loop to the object edge.'
                       : 'Smart loop = Manual: the exact pixels you trace are added/removed.');
    }, 'Smart drag: Auto snaps your loop to the SAM3 boundary; Manual uses the exact pixels traced.');
    el.smartmode.setAttribute('data-smartmode', '1');
    el.tools.appendChild(el.smartmode);
    el.tools.appendChild(tbtn('rect', 'Rect', 'Drag a box'));
    el.tools.appendChild(tbtn('ellipse', 'Ellipse', 'Drag an ellipse'));
    el.tools.appendChild(btn('Close shape', function () {
      if (active && (active.mode === 'poly' || active.mode === 'draw') && active.pts.length > 2) {
        commitStroke(active);
      }
      active = null; rasterize(); status('Shape added.');
    }));
    el.tools.appendChild(btn('Undo', undo, 'Ctrl/Cmd-Z'));
    el.tools.appendChild(btn('Redo', redoStep, 'Ctrl/Cmd-Shift-Z'));
    el.tools.appendChild(btn('Clear', clearMask));
    root.appendChild(el.tools);
    buildInspector(root);          // appears directly under the toolbar when something is picked

    var g1 = row('tb-grid');
    g1.appendChild(ctl('brush', 'range', 60, 'size', { min: 4, max: 400, step: 2 }));
    g1.appendChild(ctl('hardness', 'range', 0.55, 'hardness', { min: 0, max: 1, step: 0.05 }));
    g1.appendChild(ctl('wash', 'range', 0.45, 'overlay', { min: 0, max: 1, step: 0.05 }));
    root.appendChild(g1);

    var g2 = row('tb-grid');
    // ONE bipolar control, not a grow slider and a shrink slider. Two independent fields
    // were not opposites: the server always dilates then erodes, so setting both to 12 ran a
    // morphological CLOSING (measured: a notched shape went 9704 -> 10251 px, its notch
    // filled). A user reading "grow 12, shrink 12" as "no net change" got a different mask
    // than they asked for. One signed axis has exactly one no-op, at the detent.
    //
    // Still PIXELS, deliberately: it is the same unit the graph's GrowMask nodes use, so the
    // preview and the render are the same number (see masks.py). A percentage of the object
    // would be a nicer-sounding lie -- "5% of the selection" is undefined for a non-square
    // shape (2 px or 15 px for the same number on a 300x40 box), silently changes meaning
    // when the object is scaled in the Select tool, and on a ring painted as blob-minus-
    // eraser it fills the hole the user carved (10% of a 566 px bbox is 43 px, against ~50 px
    // of material). Percentage is offered as a READOUT, so the number stays honest.
    g2.appendChild(ctl('edge', 'range', 0, 'edge grow/shrink', { min: -60, max: 60, step: 2 }));
    g2.appendChild(ctl('feather', 'range', 8, 'feather', { min: 0, max: 40, step: 1 }));
    root.appendChild(g2);
    root.appendChild(mk('div', 'tb-hint',
      'edge: drag left to shrink the selection, right to grow it. Applies to shapes and ' +
      'auto-selections — a brush stroke keeps the edge its hardness gave it. These set the ' +
      'values for the NEXT object; tap Select to re-tune one already placed.'));
    root.appendChild(labelled('', toggle('inv', 'invert — edit everything OUTSIDE the paint', false)));

    var g3 = row('tb-grid');
    g3.appendChild(ctl('strength', 'range', 0.25, 'edit strength', { min: 0.05, max: 1, step: 0.05 }));
    g3.appendChild(ctl('opacity', 'range', 1, 'blend opacity', { min: 0, max: 1, step: 0.05 }));
    g3.appendChild(ctl('colormatch', 'range', 0.9, 'colour match', { min: 0, max: 1, step: 0.05 }));
    g3.appendChild(ctl('detail', 'range', 0.35, 'keep original detail', { min: 0, max: 1, step: 0.05 }));
    root.appendChild(g3);

    // The mode is NOT cosmetic: engine._apply_mode reads spec["kind"] and rewires the
    // KSampler conditioning, so these two really produce different images. 'edit' keeps the
    // scene-referenced pass (KSampler conditions on a latent built from the whole original
    // image) so surface changes -- recolor, a different shirt, an accessory -- blend into the
    // surroundings. 'replace' drops that pass and conditions on the masked region alone, so
    // the prompt can do a genuine identity-level swap / removal. Both act on the PAINTED
    // mask only (never a text segmentation, so nothing outside your paint is touched). This
    // mirrors imagegen's _submit_edit preserve_scene_context branch -- the same replacement
    // the OWUI edit_image tool performs. The offline suite pins the wiring (engine reads
    // kind; 'replace' rewires the graph) and that these labels describe the two real paths.
    root.appendChild(labelled('mode', select('kind', [
      ['edit', 'Edit — recolor / light touch, blends into the scene'],
      ['replace', 'Replace — erase / swap what is inside the mask']
    ])));
    root.appendChild(mk('div', 'tb-hint tb-deadnote',
      'Edit blends the change into the surroundings and keeps what is in the mask; Replace ' +
      'reimagines the mask from the prompt. Either way only the area you paint is touched.'));
    el.prompt = document.createElement('textarea');
    el.prompt.id = 'tb_prompt'; el.prompt.rows = 2;
    el.prompt.placeholder = 'What to put there (for Replace). For Edit, name the change. Empty = clean up.';
    el.prompt.value = CFG.prompt || '';
    root.appendChild(labelled('prompt', el.prompt));

    var g4 = row('tb-grid');
    // NOT yet wired into the render graph (see DEAD_KNOBS below): this mode is recorded
    // with the job and echoed by the API, but _patch_graph never reads it. Values match
    // ComfyUI's ImageBlend enum spellings (soft_light/hard_light) so the eventual
    // rewire is a passthrough.
    g4.appendChild(labelled('blend', select('blend', [
      ['normal', 'Normal'], ['multiply', 'Multiply'], ['screen', 'Screen'],
      ['overlay', 'Overlay'], ['soft_light', 'Soft light'], ['hard_light', 'Hard light'],
      ['luminosity', 'Luminosity'], ['color', 'Color']])));
    g4.appendChild(ctl('variants', 'number', 1, 'variants', { min: 1, max: 4 }));
    g4.appendChild(ctl('seed', 'number', -1, 'seed (-1 random)', { min: -1, max: 4294967295, step: 1 }));
    root.appendChild(g4);

    // Knobs whose values reach the server, are persisted with the job, and are then
    // ignored: engine._patch_graph wires only prompt/width/height/seed (plus the mask
    // geometry, which masks.py applies). Everything else here is M1 scaffolding for a
    // compositing pass the graph does not have yet. A control that silently does nothing
    // is worse than an absent one -- the user adjusts it, sees no change, and concludes the
    // whole editor is unreliable. So: greyed, titled with the reason, kept in the layout so
    // the feature reads as planned rather than missing (same choice as the brush's feather
    // row in the inspector). DELETING this list is the discipline: when a knob is wired
    // into the graph, remove it here -- the smoke suite FAILS if a knob is enabled but
    // unclaimed by the engine.
    var DEAD_KNOBS = ['strength', 'variants'];
    var DEAD_EL = {};   // control-id overrides for knobs still on death row (none today)
    for (var di = 0; di < DEAD_KNOBS.length; di++) {
      var node = el[DEAD_EL[DEAD_KNOBS[di]] || DEAD_KNOBS[di]];
      if (node) {
        node.disabled = true;
        node.title = 'Recorded with the job, not wired into the render graph yet — changing this does nothing today';
      }
    }
    root.appendChild(mk('div', 'tb-hint tb-deadnote',
      'greyed knobs are recorded with the job but not wired into the render graph yet — ' +
      'prompt, seed, the mask geometry and the compositing knobs (opacity, blend, colour ' +
      'match, detail) affect the image today'));
    return root;
  }


  /* ---------------- actions ---------------- */
  function showImage(b64) {
    el.out.innerHTML = '';
    var i = document.createElement('img');
    i.src = 'data:image/png;base64,' + b64;
    i.alt = 'result';
    // Re-report on DECODE, not merely on append: an <img> contributes height only once it
    // has intrinsic dimensions, so reporting synchronously after appendChild measures the
    // pre-image document and the host sizes the frame short — an internal scrollbar again,
    // the exact bug this whole pass exists to remove.
    i.onload = reportHeightSoon;
    el.out.appendChild(i);
    reportHeightSoon();          // cover the empty/decorative case where onload never fires
  }


  function doPreview() {
    var m = exportMask();
    if (!m) { status('Nothing painted yet.', 'bad'); return; }
    status('Previewing mask (server-side, no GPU)…');
    req('/toolbox/mask/preview', {
      image: CFG.image || null, image_id: CFG.image_id || null,
      mask_png: m, layers: exportLayers(), params: params()
    }).then(function (r) {
      if (r.overlay_png) showImage(r.overlay_png);
      var c = 100 * (typeof r.coverage === 'number' ? r.coverage : 0);
      var msg = 'Server sees ' + c.toFixed(2) + '% of the frame selected';
      if (r.info && r.info.size) msg += ' · resampled to ' + r.info.size[0] + 'x' + r.info.size[1];
      // empty vs tiny is the difference between a broken mask and a blemish-sized edit;
      // collapsing them would make every spot-heal look like a failure.
      if (r.empty) msg += ' — NOTHING SELECTED: the render would paint nothing.';
      else if (r.tiny) msg += ' — small (fine for a spot, too small for an object).';
      status(r.error || r.note || msg, r.empty ? 'bad' : (r.tiny ? 'warn' : 'ok'));
    }).catch(function (e) { status('Preview failed: ' + e.message, 'bad'); });
  }

  function doAuto() {
    var text = (el.prompt.value || '').trim();
    if (!text) { status('Type what to SELECT in the prompt box first.', 'bad'); return; }
    status('Auto-masking "' + text + '"…');
    req('/toolbox/mask/auto', {
      image: CFG.image || null, image_id: CFG.image_id || null,
      text: text, threshold: 0.4
    }).then(function (r) {
      if (r.error) throw new Error(r.error);
      if (!r.mask_png) throw new Error('no mask came back');
      var im = new Image();
      im.onload = function () {
        // Loaded as ONE entry on the vector stack, so the guess stays editable: the
        // eraser and undo still work on an auto mask exactly as on a brush stroke.
        active = null;
        commitStroke({ mode: 'load', img: im, size: 0, hardness: 1, erase: false });
        rasterize();
        status('Auto-mask loaded — refine it with brush/eraser, then Preview.');
      };
      im.src = 'data:image/png;base64,' + r.mask_png;
    }).catch(function (e) { status('Auto-mask failed: ' + e.message, 'bad'); });
  }

  // ---- SAM3 smart-select ---------------------------------------------------------------
  // mode: 'new' (fresh object, plain click) | 'add' (grow, shift) | 'neg' (shrink, alt).
  // A plain click always STARTS A NEW independent object, so "select a car, then another
  // car" leaves two separately-editable masks; shift/alt refine the current (most recent)
  // object. runSmart owns the point-list edit so a failed/empty request rolls back to exactly
  // the mask still on canvas. Every request re-sends the WHOLE list and REPLACES that object's
  // layer (the node is stateless), so an excluded region genuinely disappears rather than
  // surviving as a union of prior guesses.
  function runSmart(mode, pt) {
    var obj;
    if (mode === 'new') {
      obj = { pos: [pt], neg: [], layer: null };
      smartObjs.push(obj); smartCur = smartObjs.length - 1;
    } else {
      obj = smartObjs[smartCur];                       // refine the object being built
      if (!obj) { obj = { pos: [], neg: [], layer: null }; smartObjs.push(obj); smartCur = smartObjs.length - 1; }
      if (mode === 'neg') obj.neg = obj.neg.concat([pt]); else obj.pos = obj.pos.concat([pt]);
    }
    if (!obj.pos.length) {
      status('Alt+click only subtracts — click the object normally first.', 'warn');
      if (mode !== 'new') { obj.neg = obj.neg.slice(0, -1); } else { smartObjs.pop(); smartCur = smartObjs.length - 1; }
      return;
    }
    requestSmart(obj, mode);
  }

  // Shared SAM3 request + commit. `obj` carries the full point list and its layer (replaced in
  // place on a refine, appended once on a first success). seq guards out-of-order replies.
  function requestSmart(obj, tag) {
    var seq = ++smartSeq;
    status('Selecting…');
    req('/toolbox/mask/click', {
      image: CFG.image || null, image_id: CFG.image_id || null,
      points: obj.pos, negative_points: obj.neg, threshold: 0.5
    }).then(function (r) {
      if (seq !== smartSeq) return;                        // a later click already superseded this
      if (r.error) throw new Error(r.error);
      if (!r.mask_png) throw new Error('no mask came back');
      if (r.empty) {                                        // selected nothing: undo this click's point
        if (tag === 'new') { smartObjs.pop(); smartCur = smartObjs.length - 1; }
        else if (tag === 'neg') obj.neg = obj.neg.slice(0, -1);
        else obj.pos = obj.pos.slice(0, -1);
        status(r.warning || 'That click selected nothing — tap a solid part of the object.', 'warn');
        return;
      }
      var im = new Image();
      im.onload = function () {
        if (seq !== smartSeq) return;                       // superseded while the image decoded
        active = null;
        if (obj.layer && strokes.indexOf(obj.layer) >= 0) {
          obj.layer.img = im; measureLoadBBox(obj.layer);   // refine in place: replace, never append
          commitHintKind = 'auto';                    // re-guessed pixels; the object's params
                                                      // ride over via ancestor overlap
        } else {
          obj.layer = commitStroke({ mode: 'load', img: im, size: 0, hardness: 1, erase: false });
          measureLoadBBox(obj.layer);
        }
        // Select the REGION the click landed in (the object the user pointed at),
        // not the stroke index — after a merge the layer may be one of many parents.
        var _lp = (obj.pos && obj.pos[0]) ? { x: obj.pos[0][0] * W, y: obj.pos[0][1] * H } : null;
        var _hi = _lp ? hitTest(_lp) : -1;
        selectObj(_hi >= 0 ? _hi : (regions.length ? regions.length - 1 : -1));
        rasterize();
        var c = 100 * (typeof r.coverage === 'number' ? r.coverage : 0);
        status('Selected ' + c.toFixed(1) + '% — Select-tool to grow/feather, shift+click to add, alt+click to remove, then Preview.');
      };
      im.src = 'data:image/png;base64,' + r.mask_png;
    }).catch(function (e) {
      if (seq !== smartSeq) return;
      if (tag === 'new' && !obj.layer) { smartObjs.pop(); smartCur = smartObjs.length - 1; }
      status('Smart select failed: ' + e.message, 'bad');
    });
  }

  // The lasso path. `seed` is a natural-px polygon; Auto converts it to interior points and
  // reuses the verified point seam (SAM3 snaps to the real boundary); Manual commits the exact
  // traced loop as a normal shape stroke (already pixel-accurate, no second guess to snap).
  // 'add'/'neg' refine the current object; 'new' starts a fresh one.
  function smartLasso(seedPts, mode) {
    if (!seedPts || seedPts.length < 3) return;
    if (mode !== 'new') {
      var obj = smartObjs[smartCur];
      if (!obj) { obj = { pos: [], neg: [], layer: null }; smartObjs.push(obj); smartCur = smartObjs.length - 1; }
      if (smartAuto) {
        var frac = interiorSeeds(seedPts).map(function (q) { return [q[0] / W, q[1] / H]; });
        if (!frac.length) { status('That loop had no clear interior to sample.', 'warn'); return; }
        if (mode === 'neg') obj.neg = obj.neg.concat(frac); else obj.pos = obj.pos.concat(frac);
        requestSmart(obj, mode);
      } else {
        active = null;
        commitStroke({ mode: 'draw', pts: seedPts, size: 0, hardness: 1, erase: mode === 'neg' });
        redoStack = []; rasterize();
        status((mode === 'neg' ? 'Removed' : 'Added') + ' that loop to the selection.');
      }
      return;
    }
    // 'new'
    if (!smartAuto) {
      active = null;
      var o = commitStroke({ mode: 'draw', pts: seedPts, size: 0, hardness: 1, erase: false }); rasterize();
      status('Added that loop as a shape — Select-tool to grow/feather it.');
      return;
    }
    var fr = interiorSeeds(seedPts).map(function (q) { return [q[0] / W, q[1] / H]; });
    if (!fr.length) { status('That loop had no clear interior to sample.', 'warn'); return; }
    var nobj = { pos: fr, neg: [], layer: null };
    smartObjs.push(nobj); smartCur = smartObjs.length - 1;
    requestSmart(nobj, 'new');
  }


  var jobTimer = null;
  var jobId = null;
  var jobToken = null;                 // the job-scope token create minted; poll/cancel use it
  function doRender() {
    var m = exportMask();
    if (!m) { status('Paint a mask first.', 'bad'); return; }
    if (jobTimer) { status('Still waiting on the previous render — see the result below.'); return; }
    status('Submitting render…');
    req('/toolbox/jobs', {
      image: CFG.image || null, image_id: CFG.image_id || null,
      source_ref: CFG.source_ref || null, mask_png: m,
      layers: exportLayers(), spec: params()
    }).then(function (r) {
      if (r.error) throw new Error(r.error);
      if (!r.job_id) throw new Error('job was not created');
      if (el.seed && r.seed !== undefined) el.seed.value = r.seed;
      status('Queued as job ' + String(r.job_id).slice(0, 8) + '…');
      // Poll with the JOB-scope token the create response minted, NOT the launch token:
      // create redeemed the launch token single-use, and the poll/cancel routes require
      // scope="job" (a launch token there is a 403). This token is bound to this job_id.
      jobToken = r.token || null;
      jobId = r.job_id;
      pollJob(r.job_id, 0, jobToken);
    }).catch(function (e) { status('Render failed: ' + e.message, 'bad'); });
  }

  function cropNote(crop) {
    // Provenance, not decoration. A crop-rendered artifact is a COMPOSITE of the model
    // output and the user's own photo -- not a pure model output -- and a photorealism
    // workflow needs to be able to tell the two apart after the fact. Absent for a
    // full-frame render, so nothing is claimed when nothing was done.
    if (!crop || typeof crop !== 'object') return '';
    if (!crop.cropped) return '';
    var sz = crop.size || [], ar = crop.artifact || [];
    var bits = ' · crop-rendered ' + (sz[0] || '?') + '×' + (sz[1] || '?');
    if (ar.length === 2) bits += ' → composited into ' + ar[0] + '×' + ar[1];
    if (crop.composited === false) {
      // Say so plainly: the pixels the user is looking at are the bare crop.
      bits += ' (compositing FAILED — showing the crop alone)';
    }
    var k = crop.knobs || {}, used = [];
    for (var key in k) { if (Object.prototype.hasOwnProperty.call(k, key)) used.push(key); }
    if (used.length) bits += ' · ' + used.sort().join(', ');
    return bits;
  }

  function pollJob(id, tries, tok) {
    // Poll rather than hold a connection open: an editor session should not depend on a
    // 10-minute in-flight request surviving a daemon restart or a proxy idle timeout.
    if (tries > 400) { clearBar(); status('Render timed out.', 'bad'); return; }
    req('/toolbox/jobs/poll', { job_id: id }, tok).then(function (r) {
      if (r.error) throw new Error(r.error);
      if (r.state === 'done' || r.state === 'error') {
        jobTimer = null; jobId = null; jobToken = null;   // terminal: drop the spent job token
        clearBar();   // a bar parked at 95% next to a finished image is worse than no bar
        if (r.state === 'error') { status('Render failed: ' + (r.error_message || 'engine error'), 'bad'); return; }
        var list = r.artifacts || [];
        el.out.innerHTML = '';
        for (var i = 0; i < list.length; i++) {
          var im = document.createElement('img');
          im.src = list[i].url || ('data:image/png;base64,' + list[i].png);
          im.onload = reportHeightSoon;   // same decode-time re-report as showImage: N
          el.out.appendChild(im);         // artifacts arrive after the ladder has stopped
        }
        reportHeightSoon();

        status('Rendered ' + list.length + ' image(s)' + (r.seed !== undefined ? ' · seed ' + r.seed : '')
               + (r.elapsed_s ? ' · ' + r.elapsed_s.toFixed(0) + 's' : '')
               + cropNote(r.crop), 'ok');
        return;
      }
      status(progressLine(r, tries));
      renderBar(r.progress, tries);
      jobTimer = setTimeout(function () { jobTimer = null; pollJob(id, tries + 1, tok); }, 2000);
    }).catch(function (e) { jobTimer = null; clearBar(); status('Render failed: ' + e.message, 'bad'); });
  }

  function progressLine(r, tries) {
    // Only what is KNOWN may be said. ComfyUI reports step count on /ws alone (no HTTP
    // route, and no websocket client in this image), so this line never shows a percent
    // complete: it shows the stage read from /queue, real elapsed seconds, and an ETA that
    // is calibrated from this server's own recent renders at the same size -- labelled est.
    // When there is no history, it says so instead of guessing.
    var p = r.progress || {};
    var secs = (p.elapsed_s === null || p.elapsed_s === undefined) ? (tries * 2) : p.elapsed_s;
    var stage = (p.stage === 'running') ? 'generating'
              : (p.stage === 'queued') ? ('queued' + (p.ahead ? ', ' + p.ahead + ' ahead' : ''))
              : (p.stage === 'vanished') ? 'finishing'
              : 'working';
    var s = 'Rendering… ' + stage + ' · ' + Math.round(secs) + 's';
    if (p.eta_s) {
      s += ' · ~' + Math.max(0, Math.round(p.eta_s - secs)) + 's left (est., '
           + (p.eta_samples || 0) + ' prior render' + ((p.eta_samples || 0) === 1 ? '' : 's') + ')';
    } else {
      s += ' · no timing history at this size yet';
    }
    return s;
  }

  function renderBar(p, tries) {
    // The bar mirrors the line's honesty: a determinate fill ONLY when a calibrated ETA
    // exists, capped short of 100% because we are estimating, and a dim indeterminate
    // wash otherwise. A bar that creeps to 100% and then still says 'queued' is the single
    // most effective way to teach users to ignore this UI.
    var host = el.status && el.status.parentNode;
    if (!host) return;
    var bar = host.querySelector('#tb-progbar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'tb-progbar';
      bar.style.cssText = 'height:4px;border-radius:2px;background:#23272e;'
                        + 'overflow:hidden;margin:2px 0 4px';
      var f = document.createElement('div');
      f.style.cssText = 'height:100%;width:0%;background:#4c8f6a;'
                      + 'transition:width .8s linear;opacity:.35';
      bar.appendChild(f);
      host.insertBefore(bar, el.status.nextSibling);
    }
    var fill = bar.firstChild;
    var secs = (p && p.elapsed_s != null) ? p.elapsed_s : (tries * 2);
    if (p && p.eta_s) {
      fill.style.opacity = '1';
      fill.style.width = Math.max(3, Math.min(95, (secs / p.eta_s) * 100)).toFixed(1) + '%';
    } else {
      fill.style.opacity = '.35';
      fill.style.width = '100%';
    }
  }

  function clearBar() {
    var host = el.status && el.status.parentNode;
    if (!host) return;
    var bar = host.querySelector('#tb-progbar');
    if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
  }

  function doCancel() {
    // Cancel with the JOB-scope token (same one poll uses), never the launch token. Best
    // effort: a job that already reached a terminal state cancels to that state (server
    // idempotent), so this is safe to press at any point.
    if (!jobId) { status('Nothing to cancel.', 'bad'); return; }
    if (jobTimer) { clearTimeout(jobTimer); jobTimer = null; }
    req('/toolbox/jobs/cancel', { job_id: jobId }, jobToken).then(function (r) {
      if (r.error) throw new Error(r.error);
      jobId = null; jobToken = null;
      status('Cancelled' + (r.state ? ' (' + r.state + ')' : '') + '.', 'ok');
    }).catch(function (e) { status('Cancel failed: ' + e.message, 'bad'); });
  }


  /* ---------------- live mask preview (server-faithful) ---------------- */
  // The browser can show raw strokes; it cannot show feather/grow/shrink (that is
  // masks.normalize, server-side and authoritative). So on idle we ask the server for the
  // real overlay and show THAT — the editor reflects what the render will actually inpaint,
  // not a rough local guess. Debounced so a long stroke does not fire a round-trip per
  // pixel, suppressed mid-drag, and guarded by a sequence id so a slow response cannot
  // overwrite a newer one. On any error we keep the local wash: an unavailable preview must
  // never block painting.
  function hasMask() { return strokes.length > 0 || !!(active && active.pts && active.pts.length > 2); }
  function refresh() { compose(); drawRing(); schedulePreview(); }
  var _pvTimer = null, _pvSeq = 0;
  function schedulePreview() {
    preview = null;                 // fall back to the local wash until the server answer
    if (!maskC) return;
    compose();                      // instant feedback while the request is pending
    if (!hasMask() || dragEnabled || (!CFG.image && !CFG.image_id) || !TOKEN) return;
    if (_pvTimer) clearTimeout(_pvTimer);
    _pvTimer = setTimeout(runPreview, 420);
  }
  function runPreview() {
    _pvTimer = null;
    if (!hasMask() || (!CFG.image && !CFG.image_id) || !TOKEN) return;
    var sig = previewSig();
    if (preview && preview.sig === sig) return;
    var seq = ++_pvSeq;
    req('/toolbox/mask/preview', {
      image: CFG.image || null, image_id: CFG.image_id || null,
      mask_png: exportMask(), layers: exportLayers(), params: params()
    }, TOKEN).then(function (r) {
      if (seq !== _pvSeq) return;   // a newer request already won
      var im = new Image();
      im.onload = function () {
        if (seq !== _pvSeq) return;
        preview = { img: im, sig: sig };
        if (el.cover) el.cover.textContent = 'mask ≈ ' + Math.round((r.coverage || 0) * 100) + '% of frame';
        compose();
      };
      im.src = 'data:image/png;base64,' + (r.overlay_png || r.overlay);
    }).catch(function () { /* preview optional; the local wash is already shown */ });
  }

  /* ---------------- zoom / pan ---------------- */
  // The canvas backing store never changes; only the CSS box grows, and toNatural reads the
  // live rect, so painting stays correct at any zoom. The stage scrolls when zoomed.
  function setZoom(z) {
    zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z || 1));
    applyZoom();
  }
  function measureFit() {
    // clientWidth already excludes the permanently reserved vertical gutter, i.e. the
    // width content can occupy WITHOUT provoking a horizontal bar. Sample it only while
    // unzoomed: measured while zoomed it would shrink a little on every +/- press.
    var cw = el.stage ? el.stage.clientWidth : 0;
    if (cw > 0) fitW = cw;
  }
  function applyZoom() {
    if (!viewC || !W) return;
    if (zoom <= 1) measureFit();
    if (el.box) {
      el.box.style.aspectRatio = W + ' / ' + H;
      // unzoomed: fluid, so no measured-pixel drift can overflow the stage and no
      // scrollbar can ever appear at 'Fit'. zoomed: px, and the stage legitimately
      // scrolls to reach the rest of the photo.
      el.box.style.width = (zoom > 1 && fitW)
        ? (Math.max(1, Math.round(fitW * zoom)) + 'px') : '100%';
    }
    if (el.stage) el.stage.style.justifyContent = zoom > 1 ? 'flex-start' : 'center';
    if (el.zoomVal) el.zoomVal.textContent = Math.round(zoom * 100) + '%';
    drawRing();
  }

  /* ---------------- brush / eraser cursor ring ---------------- */
  // Its own overlay canvas (never composited into maskC/viewC) so it can follow the pointer
  // without repainting the mask. Drawn in natural coords at the brush's true footprint, so
  // it always shows exactly what one click will select — the feedback that made brush/eraser
  // size pure guesswork before.
  var ringPt = null;
  function drawRing() {
    if (!ringC) return;
    var rc = ringC.getContext('2d');
    rc.clearRect(0, 0, W, H);
    if (!ringPt || (tool !== 'brush' && tool !== 'eraser')) return;
    var r = brushR();
    rc.save();
    rc.lineWidth = Math.max(1, 1.5 * scale());
    rc.strokeStyle = tool === 'eraser' ? 'rgba(255,255,255,0.95)' : 'rgba(232,62,62,0.95)';
    rc.beginPath(); rc.arc(ringPt.x, ringPt.y, r, 0, 6.2832); rc.stroke();
    rc.beginPath();
    rc.moveTo(ringPt.x - r * 0.3, ringPt.y); rc.lineTo(ringPt.x + r * 0.3, ringPt.y);
    rc.moveTo(ringPt.x, ringPt.y - r * 0.3); rc.lineTo(ringPt.x, ringPt.y + r * 0.3); rc.stroke();
    rc.restore();
  }
  function hideRing() { ringPt = null; drawRing(); }

  /* ---------------- pointer ---------------- */
  function wirePointer() {
    viewC.style.touchAction = 'none';   // we own gestures; the stage scrolls/zooms via us
    var lastPt = null;

    function restIfEmpty() {
      // Called from every exit of up(): once no pointer is down there is no gesture left
      // to remember, so nothing may survive to veto the next stroke. Without it, one lost
      // pointerup (a browser cancelling a pinch finger and never reporting it) would
      // poison the editor permanently.
      if (Object.keys(ptrs).length) return;
      ptrs = {}; dragEnabled = false; pinching = false; pinchStart = null;
    }
    function dist(a, b) { return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY); }
    function twoPts() {
      var k = Object.keys(ptrs);
      return k.length >= 2 ? [ptrs[k[0]], ptrs[k[1]]] : null;
    }
    // Zoom about the fingers and follow the pinch midpoint so a two-finger drag pans the
    // stage once zoomed in — otherwise the only way to reach an edge is the scrollbar.
    function applyPinch() {
      var ab = twoPts();
      if (!ab || !pinchStart) return;
      var d = dist(ab[0], ab[1]);
      if (d < 8) return;                       // untrustworthy: fingers collapsed together
      setZoom(pinchStart.zoom * d / pinchStart.dist);
      if (zoom > 1 && el.stage) {
        var mx = (ab[0].clientX + ab[1].clientX) / 2, my = (ab[0].clientY + ab[1].clientY) / 2;
        el.stage.scrollLeft = pinchStart.sx + (pinchStart.mx - mx);
        el.stage.scrollTop = pinchStart.sy + (pinchStart.my - my);
        pinchStart.mx = mx; pinchStart.my = my;
        pinchStart.sx = el.stage.scrollLeft; pinchStart.sy = el.stage.scrollTop;
      }
    }

    function down(ev) {
      if (jobId || !baseC) return;             // frozen while a job runs; no image yet
      if (ev.button != null && ev.button !== 0) return;
      ev.preventDefault();
      // isPrimary marks the first pointer of a NEW gesture. Trusting the pointer map
      // alone meant a stale entry (an up() the browser never fired) kept reporting
      // '>=2 pointers', so every later tap read as mid-pinch and was ignored.
      if (ev.isPrimary !== false && !pinching) ptrs = {};
      ptrs[ev.pointerId] = { clientX: ev.clientX, clientY: ev.clientY };
      if (Object.keys(ptrs).length >= 2) {      // second finger: this is a pinch, not a stroke
        active = null; dragEnabled = false; pinching = true;
        var ab = twoPts();
        pinchStart = {
          dist: dist(ab[0], ab[1]), zoom: zoom,
          mx: (ab[0].clientX + ab[1].clientX) / 2, my: (ab[0].clientY + ab[1].clientY) / 2,
          sx: el.stage ? el.stage.scrollLeft : 0, sy: el.stage ? el.stage.scrollTop : 0
        };
        return;
      }
      // A down inside the settle window of a pinch is the finger that outlived it, not a
      // new stroke; past that window a tap is always a tap, however the last one ended.
      if (pinching || Date.now() - pinchEnd < 400) return;
      var p = toNatural(ev); lastPt = p; ringPt = p;
      var hard = hardness(), t = tool;
      if (t === 'select') {
        // Objects are PIXELS under the finger now: tap the blob to grab it, drag to move
        // the pixels, and an empty tap must CLEAR the selection (a hidden selected object
        // silently receiving edits was the bug this branch exists to stop). The corner
        // SCALE handle is gone for good — a blob that may BE a merge of several strokes
        // has no vector left to resize, and an affordance that does something other than
        // what it looks like it does is the death this project's dead-knob rule prevents.
        var hit = hitTest(p);
        if (hit >= 0) {
          selectObj(hit);
          _dragReg = selObj();
          selDrag = { kind: 'move', last: p };
        } else { selectObj(-1); _dragReg = null; }
        dragEnabled = !!selDrag;
        try { viewC.setPointerCapture(ev.pointerId); } catch (e) { /* not fatal */ }
        return;
      }
      if (sel >= 0 && selObj()) selectObj(-1);   // any other tool drops the selection
      if (t === 'polygon') {                    // click a vertex; never a drag
        if (!active || active.mode !== 'poly') active = { mode: 'poly', pts: [], size: 0, hardness: 1, erase: false };
        active.pts.push(p); dragEnabled = false; compose(); return;
      }
      if (t === 'smart') {                      // tap = point prompt; drag = a lasso loop
        // Decide click-vs-lasso on release: a finger that barely moved is a point prompt
        // (shift/alt add/remove a point); one that traced a path is a lasso. We start a
        // 'draw' buffer so the loop previews live, exactly like the Lasso tool, but do NOT
        // commit here — up() routes it to smartLasso, not the generic shape commit.
        var sm = (ev.altKey || ev.ctrlKey || ev.metaKey) ? 'neg' : (ev.shiftKey ? 'add' : 'new');
        smartDrag = { mode: sm, moved: false, x0: p.x, y0: p.y };
        active = { mode: 'draw', size: 0, hardness: 1, erase: false, pts: [p] };
        dragEnabled = true;
        try { viewC.setPointerCapture(ev.pointerId); } catch (e) { /* not fatal */ }
        return;
      }
      if (t === 'brush' || t === 'eraser') {
        active = { mode: 'free', size: brushR() * 2, hardness: hard, pts: [p], erase: t === 'eraser' };
      } else if (t === 'lasso') {               // freehand: one point now, more on move
        active = { mode: 'draw', size: 0, hardness: 1, erase: false, pts: [p] };
      } else {                                  // rect / ellipse: [start, current]
        active = { mode: t, size: 0, hardness: 1, erase: false, pts: [p, p] };
      }
      dragEnabled = true;
      try { viewC.setPointerCapture(ev.pointerId); } catch (e) { /* not fatal */ }
      rasterize();
    }

    function move(ev) {
      if (ptrs[ev.pointerId]) ptrs[ev.pointerId] = { clientX: ev.clientX, clientY: ev.clientY };
      if (pinching) { applyPinch(); return; }
      // Select-mode dragging must be handled BEFORE the painting guard below. That guard
      // requires an `active` stroke buffer, which the Select tool deliberately never
      // creates: it manipulates already-committed objects rather than painting new ones.
      // With the select branch after the guard, a grabbed object could never be moved --
      // the drag fell silently through to the hover branch and nothing happened (found by
      // the browser suite: the exported mask was unchanged, old band 210390 / new band 0).
      if (tool === 'select' && selDrag) {
        var sp = toNatural(ev);
        if (selDrag.kind === 'move') {
          translateSel(sp.x - selDrag.last.x, sp.y - selDrag.last.y);
          selDrag.last = sp;
        }
        return;
      }
      // hover (button up): still move the ring so brush/eraser show their footprint
      if (!dragEnabled || !active) {
        if (!jobId && baseC && (tool === 'brush' || tool === 'eraser')) { ringPt = toNatural(ev); drawRing(); }
        return;
      }
      var p = toNatural(ev);
      if (tool === 'smart' && smartDrag && !smartDrag.moved) {
        // Past a small displacement this is a lasso trace, not a tap; the generic 'draw'
        // branch below still appends the points so the loop previews while tracing.
        if (Math.hypot(p.x - smartDrag.x0, p.y - smartDrag.y0) > 6) smartDrag.moved = true;
      }
      if (active.mode === 'free' || active.mode === 'draw') {
        // jitter guard: a real drag must move at least ~1 natural px so a fast swipe does
        // not stack hundreds of near-identical discs (slow, and it thickens the stroke)
        var dx = p.x - lastPt.x, dy = p.y - lastPt.y;
        if (dx * dx + dy * dy >= 1) { active.pts.push(p); lastPt = p; }
      } else {
        active.pts[1] = p;                      // rect / ellipse resize
      }
      ringPt = p; drawRing();
      rasterize();
    }

    function up(ev) {
      delete ptrs[ev.pointerId];
      if (pinching) {
        if (Object.keys(ptrs).length < 2) {
          pinching = false; pinchStart = null;
          pinchEnd = Date.now();
          active = null; dragEnabled = false;    // discard the stroke the pinch interrupted
        }
        restIfEmpty();
        return;
      }
      if (Date.now() - pinchEnd < 400) { restIfEmpty(); return; }
      dragEnabled = false;
      if (tool === 'select') {
        // selDrag ALWAYS clears on release: a leftover handle keeps the Select tool
        // latched in drag state (the region rebuild gate stays closed, and a button-up
        // pointermove would still translate the object).
        var wasMove = selDrag && selDrag.kind === 'move';
        selDrag = null;
        if (wasMove) bakeMove(); else _dragReg = null;
        return;
      }
      if (tool === 'smart') {
        // A barely-moved press is a point prompt; a traced path is a lasso. Neither commits
        // through the generic shape path below — runSmart/smartLasso own it (Auto reuses the
        // point seam; Manual commits the exact loop). active is discarded either way.
        var sd = smartDrag; smartDrag = null;
        var loop = active && active.pts ? active.pts.slice() : null; active = null;
        if (!sd) { if (ev && ev.pointerType === 'touch') hideRing(); restIfEmpty(); rasterize(); return; }
        if (sd.moved && loop && loop.length >= 3) {
          smartLasso(loop, sd.mode);
        } else {
          runSmart(sd.mode, [sd.x0 / W, sd.y0 / H]);
        }
        if (ev && ev.pointerType === 'touch') hideRing();
        restIfEmpty(); rasterize();
        return;
      }
      if (!active) return;
      var m = active.mode;
      if (m === 'free') { if (active.pts.length) commitStroke(active); }
      else if (m === 'draw') { if (active.pts.length > 2) commitStroke(active); }   // auto-close
      else if (m === 'rect' || m === 'ellipse') {
        var a = active.pts[0], b = active.pts[1];
        if (Math.abs(b.x - a.x) > 2 && Math.abs(b.y - a.y) > 2) commitStroke(active);
      }
      if (m !== 'poly') active = null;           // polygon stays open until "Close shape"
      if (ev && ev.pointerType === 'touch') hideRing();
      restIfEmpty();
      rasterize();
    }

    viewC.addEventListener('pointerdown', down);
    viewC.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', up);
    viewC.addEventListener('pointerleave', function () { if (!dragEnabled) hideRing(); });
    // wheel zoom (Ctrl/Cmd held, and trackpad pinch, which browsers report as ctrlKey) so
    // the stage never becomes an accidental scroll-while-painting trap
    viewC.addEventListener('wheel', function (ev) {
      if (!baseC || (!ev.ctrlKey && !ev.metaKey)) return;
      ev.preventDefault();
      setZoom(zoom * (ev.deltaY < 0 ? 1.15 : 1 / 1.15));
    }, { passive: false });
  }

  /* ---------------- mount ---------------- */
  function boot() {
    var host = $(CFG.root || 'tb');
    if (!host) return;
    host.className = 'tb';
    viewC = document.createElement('canvas'); viewC.id = 'tb_view';
    ringC = document.createElement('canvas'); ringC.id = 'tb_ring';
    var tbBox = mk('div', 'tb-canvasbox');
    tbBox.appendChild(viewC); tbBox.appendChild(ringC);
    el.box = tbBox;
    el.stage = row('tb-stage'); el.stage.appendChild(tbBox);
    el.status = mk('div', 'tb-status', 'Loading photo…');
    el.out = row('tb-out');

    var actions = row('tb-actions');
    actions.appendChild(btn('Preview mask', doPreview, 'tb-key'));
    actions.appendChild(btn('Auto-mask from prompt', doAuto));
    actions.appendChild(btn('Render', doRender, 'tb-go'));
    actions.appendChild(btn('Cancel', doCancel, 'stop the running render (interrupts ComfyUI)'));
    actions.appendChild(btn('Start over', function () { clearMask(); el.out.innerHTML = ''; status('Ready.'); }));

    var zr = row('tb-zoom');
    zr.appendChild(btn('Zoom -', function () { setZoom(zoom / 1.4); }, 'Zoom out'));
    el.zoomVal = mk('span', 'tb-num', '100%');
    zr.appendChild(el.zoomVal);
    zr.appendChild(btn('Zoom +', function () { setZoom(zoom * 1.4); }, 'Zoom in'));
    zr.appendChild(btn('Fit', function () { setZoom(1); }, 'Fit to width'));
    el.cover = mk('span', 'tb-hint', '');
    zr.appendChild(el.cover);
    host.appendChild(el.stage);
    host.appendChild(zr);
    host.appendChild(el.status);
    host.appendChild(actions);
    buildUI(host);
    host.appendChild(el.out);

    var im = new Image();
    im.onload = function () {
      // The server hands us the photo already inside its render ceiling and tells us what
      // that ceiling is; the fallback is the shipped default, not the old 2048, so a server
      // that predates the field cannot put us back on the 10-minute path.
      var cap = CFG.max_side || 1024;
      var long = Math.max(im.width, im.height) || 1;
      var s = long > cap ? cap / long : 1;
      // Multiples of 16, matching the pipeline's own constraint (workflows._round16), so
      // the mask grid aligns with the latent grid it eventually feeds.
      W = Math.max(64, Math.round(im.width * s / 16) * 16);
      H = Math.max(64, Math.round(im.height * s / 16) * 16);
      baseC = document.createElement('canvas'); baseC.width = W; baseC.height = H;
      baseC.getContext('2d').drawImage(im, 0, 0, W, H);
      maskC = document.createElement('canvas'); maskC.width = W; maskC.height = H;
      viewC.width = W; viewC.height = H;
      ringC.width = W; ringC.height = H;
      fitW = 0;
      // Say what size we will actually RENDER at, and that it is a ceiling rather than a
      // mystery: the server caps the long edge at CFG.max_side (masks.RENDER_MAX_SIDE) and
      // now hands us the photo already inside it, so a user who loads a 4000px original is
      // told the truth once, here, instead of guessing when the result comes back smaller.
      status('Ready — ' + W + 'x' + H
             + (s < 1 ? ' (shrunk from ' + im.width + 'x' + im.height + ')' : '')
             + '; renders run at this size, long edge capped at ' + cap + 'px.'
             + ' Paint the area; the mask preview updates live.');
      compose();
      wirePointer();
      applyZoom();
      setTool('brush');           // ring cursor live from the start, no first-click-to-arm
      reportHeightSoon();      // the photo just changed our height; one report never caught it
    };
    im.onerror = function () { status('The source image failed to load.', 'bad'); };
    if (CFG.image) im.src = CFG.image;
    else status('No source image was handed to the editor.', 'bad');
  }

  /* ---------------- embed plumbing ---------------- */
  // iframe:height is the ONLY postMessage Open WebUI's embed renderer honours (grepped
  // out of the 0.11.3 bundle). Reporting it ourselves is what stops a chat message
  // putting the editor inside a 300px box with its own scrollbar.
  //
  // One report is not enough and that was a real bug: at load/boot time the control
  // panel is built but the photo has not decoded, the toolbars have not wrapped, and a
  // Preview result has not been appended — so a single measurement under-reports and the
  // host leaves the frame with an internal scrollbar. Report now, again after layout
  // settles, and again whenever our own box changes size.
  function contentHeight() {
    var de = document.documentElement, b = document.body;
    return Math.max(de ? de.scrollHeight : 0, de ? de.offsetHeight : 0,
                    b ? b.scrollHeight : 0, b ? b.offsetHeight : 0);
  }
  var _lastH = 0;
  function reportHeight() {
    if (window.parent === window) return;
    var hgt = contentHeight() + 8;
    // Always send, even when unchanged: the host may have (re)set the frame height since
    // the last message, and a repeat is cheaper than a stuck scrollbar.
    _lastH = hgt;
    try { window.parent.postMessage({ type: 'iframe:height', height: hgt }, '*'); }
    catch (e) { /* not embedded, or blocked: harmless */ }
  }
  // The ladder: [0, ~2 frames, after decode, after late wrapping]. Timed retries rather
  // than trusting one ResizeObserver callback, because image decode and font loading
  // change height without changing body's border-box in every engine.
  function reportHeightSoon() {
    [0, 60, 250, 700, 1600].forEach(function (t) {
      if (t === 0) { try { reportHeight(); } catch (e) {} }
      else if (window.requestAnimationFrame) {
        window.requestAnimationFrame(function () {
          window.setTimeout(reportHeight, t);
        });
      } else window.setTimeout(reportHeight, t);
    });
  }
  window.addEventListener('resize', function () {
    if (zoom <= 1) { fitW = 0; applyZoom(); }
  });
  window.addEventListener('load', reportHeightSoon);
  if (window.ResizeObserver) {
    // Observe the host element, not just body: a growing control panel or an appended
    // preview result changes the host box, and body can stay put while it does.
    try {
      var ro = new ResizeObserver(reportHeight);
      ro.observe(document.body);
      var _host = document.getElementById(CFG.root || 'tb');
      if (_host) ro.observe(_host);
    } catch (e) { /* older engine: the ladder still covers us */ }
  }
  window.ToolboxEditor = { boot: boot, status: status, params: params,
                           reportHeight: reportHeight, contentHeight: contentHeight };
  // Read-only introspection for the browser suite (and for anyone debugging an embed):
  // it exposes WHAT the editor considers the selection objects without reaching into
  // private state or perturbing it. The suite's region-model assertions run on this; no
  // shipped code path reads it back, so it can never change behaviour.
  window.ToolboxEditor.state = function () {
    return { regions: regions.length, strokes: strokes.length, rev: regionRev,
             tool: tool, sel: sel,
             ink: (function () { var c = 0; for (var i = 0; i < maskA.length; i++) if (maskA[i] > REGION_ALPHA_MIN) c++; return c; })(),
             all: regions.map(function (q) {
               return { kind: q.kind, edge: q.edge, feather: q.feather, area: q.area,
                        bbox: { x0: q.bbox.x0, y0: q.bbox.y0, x1: q.bbox.x1, y1: q.bbox.y1 } };
             }) };
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();

