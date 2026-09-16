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
  //
  // Coordinates fold into two quantised sums rather than being listed: same O(pts) cost,
  // a short key, and sub-pixel jitter cannot thrash the server while any real drag moves.
  function objSig() {
    var parts = [];
    for (var i = 0; i < strokes.length; i++) {
      var o = strokes[i], pts = o.pts || [], sx = 0, sy = 0;
      for (var j = 0; j < pts.length; j++) { sx += pts[j].x; sy += pts[j].y; }
      parts.push([o.mode, o.kind, o.edge, o.grow, o.shrink, o.feather, o.erase ? 1 : 0,
                  o.size, o.hardness, pts.length,
                  Math.round(sx * 4) / 4, Math.round(sy * 4) / 4].join(':'));
    }
    return parts.join('|');
  }

  function previewSig() {
    return [strokes.length, active ? active.pts.length : -1, tool,
            val('tb_brush', 60), val('tb_hardness', 0.55), paramsSig(), objSig()].join('|');
  }
  function paramsSig() {
    var p = params();
    return [p.width, p.height, p.mask_expand, p.mask_shrink, p.mask_feather, p.invert].join(',');
  }

  /* ---------------- painting model: vector strokes, rasterized on demand ----------------
   * Painting straight into a bitmap is the tempting design and the wrong one: undo then
   * needs pixel snapshots, a brush-size change cannot retro-apply, and the eraser cannot
   * be re-ordered. Keeping strokes as point lists and repainting the mask from scratch
   * costs O(pixels x strokes) per frame, trivially real-time at these canvas sizes, and
   * it is what makes the "sloppy by design" promise actually true — undo, size, hardness
   * and eraser order all stay editable after the fact.
   */
  function rasterize() {
    if (!maskC) return;
    var m = maskC.getContext('2d');
    m.clearRect(0, 0, W, H);
    var all = strokes.concat(active ? [active] : []);
    for (var i = 0; i < all.length; i++) paintStroke(m, all[i]);
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
    if (!o.pts || !o.pts.length) return null;
    if (o.mode === 'load') return { x0: 0, y0: 0, x1: W, y1: H };   // a whole-frame auto mask
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (var i = 0; i < o.pts.length; i++) {
      var p = o.pts[i];
      if (p.x < x0) x0 = p.x; if (p.y < y0) y0 = p.y;
      if (p.x > x1) x1 = p.x; if (p.y > y1) y1 = p.y;
    }
    var pad = (o.size || 0) / 2;                    // a brush's footprint is part of its box
    return { x0: x0 - pad, y0: y0 - pad, x1: x1 + pad, y1: y1 + pad };
  }

  function hitTest(p) {
    // Topmost wins: reverse order, so the thing the user can see on top is the thing they
    // grab. Tolerance makes thin strokes reachable with a finger.
    for (var i = strokes.length - 1; i >= 0; i--) {
      var b = objBBox(strokes[i]);
      if (!b) continue;
      var tol = 8 / scale();
      if (p.x >= b.x0 - tol && p.x <= b.x1 + tol && p.y >= b.y0 - tol && p.y <= b.y1 + tol) return i;
    }
    return -1;
  }

  function selObj() { return (sel >= 0 && sel < strokes.length) ? strokes[sel] : null; }

  function selectObj(i) {
    sel = i;
    syncInspector();
    rasterize();
    if (i < 0) status('Nothing selected.');
    else {
      var o = strokes[i];
      status('Selected a ' + (o.kind || kindOf(o)) + ' — drag to move, corner to scale.');
    }
  }

  function translateSel(dx, dy) {
    var o = selObj(); if (!o) return;
    for (var i = 0; i < o.pts.length; i++) { o.pts[i].x += dx; o.pts[i].y += dy; }
    rasterize();
  }

  function scaleSel(f) {
    var o = selObj(); if (!o) return;
    var b = objBBox(o), cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
    for (var i = 0; i < o.pts.length; i++) {
      o.pts[i].x = cx + (o.pts[i].x - cx) * f;
      o.pts[i].y = cy + (o.pts[i].y - cy) * f;
    }
    if (o.size) o.size = Math.max(2, o.size * f);   // a scaled brush keeps a scaled footprint
    rasterize();
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
      o[key] = parseFloat(inp.value);
      if (o.mode === 'free' && key === 'hardness') { /* live repaint only */ }
      rasterize();
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
      var v = parseInt(inp.value, 10) || 0;
      var t = selObj(); if (!t) return;
      t.edge = v;
      t.grow = v > 0 ? v : 0;
      t.shrink = v < 0 ? -v : 0;
      paint(v);
      rasterize();
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
    var kind = o.kind || kindOf(o);
    var brush = kind === 'brush';
    el_inspect.appendChild(mk('div', 'tb-inspect-h',
      (brush ? 'Brush stroke' : (kind === 'auto' ? 'Auto selection' : 'Shape')) +
      ' — ' + (o.erase ? 'eraser' : 'paint')));
    if (brush) {
      el_inspect.appendChild(ipair('hardness', 'hardness', o, 0, 1, 0.05, o.hardness, false));
      el_inspect.appendChild(ipair('size', 'size', o, 4, 400, 2, o.size || 60, false));
      el_inspect.appendChild(ipair('feather', 'feather', o, 0, 64, 1, 0, true));
    } else {
      el_inspect.appendChild(edgePair(o));
      el_inspect.appendChild(ipair('feather', 'feather', o, 0, 64, 1, o.feather || 0, false));
    }
    var rowb = mk('div', 'tb-row');
    rowb.appendChild(btn('Delete object', deleteSel, 'Remove this object from the mask'));
    rowb.appendChild(btn('Deselect', function () { selectObj(-1); }));
    el_inspect.appendChild(rowb);
  }

  function deleteSel() {
    if (!selObj()) return;
    strokes.splice(sel, 1);
    sel = -1; selDrag = null;
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
      m.drawImage(s.img, 0, 0, W, H);
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
      return;
    }
    var wash = document.createElement('canvas');
    wash.width = W; wash.height = H;
    var wc = wash.getContext('2d');
    wc.drawImage(maskC, 0, 0);
    wc.globalCompositeOperation = 'source-in';
    wc.fillStyle = 'rgba(232,62,62,' + val('tb_wash', 0.45) + ')';
    wc.fillRect(0, 0, W, H);
    v.drawImage(wash, 0, 0);
    drawActiveOutline(v);
  }

  // The in-progress shape, drawn in natural coords so it scales with the view for free.
  // Covers the freehand lasso trace ('draw'), the click-built polygon ('poly'), rect and
  // ellipse — every tool now previews while it builds, which the lasso previously did not.
  function drawSelection(v) {
    var o = selObj(); if (!o) return;
    var b = objBBox(o); if (!b) return;
    v.save();
    v.strokeStyle = '#3ba7ff'; v.lineWidth = Math.max(1.5, 2 * scale());
    v.setLineDash([6 * scale(), 4 * scale()]);
    v.strokeRect(b.x0, b.y0, b.x1 - b.x0, b.y1 - b.y0);
    v.setLineDash([]);
    v.fillStyle = '#3ba7ff';                          // the one handle: uniform scale
    var hs = 5 * scale();
    v.fillRect(b.x1 - hs / 2, b.y1 - hs / 2, hs, hs);
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

  // The mask as per-object LAYERS.
  //
  // Run-length grouped, NOT grouped by params key: objects are walked in paint order and a
  // new layer starts whenever the params key changes. Grouping by key instead would move a
  // mid-list eraser to the end of the stack and let it eat paint the user added afterwards
  // — a silent, catastrophic difference from what they can see on screen. Contiguous runs
  // still coalesce, so a typical session ships 1-3 PNGs rather than one per object.
  function exportLayers() {
    if (!maskC) return null;
    var out = [], cur = null, curKey = null, curParams = null;
    var tmp = document.createElement('canvas');
    tmp.width = W; tmp.height = H;
    var t = tmp.getContext('2d');
    function flush() {
      if (!cur) return;
      // curParams, NOT curKey: curKey is the opaque comparison string built FROM these
      // values. Reading .kind off it yields undefined, which the server then maps onto the
      // strictest rule -- a bug that looks like "the brush got hard-edged again".
      out.push({ png: (tmp.toDataURL('image/png').split(',')[1]) || '',
                 kind: curParams.kind, edge: curParams.edge,
                 grow: curParams.grow, shrink: curParams.shrink,
                 feather: curParams.feather, erase: !!curParams.erase });
      cur = null; curKey = null; curParams = null;
    }
    for (var i = 0; i < strokes.length; i++) {
      var o = strokes[i];
      if (!o.pts || !o.pts.length) continue;
      var k = { kind: o.kind || kindOf(o), edge: o.edge || 0,
                grow: o.grow || 0, shrink: o.shrink || 0,
                feather: o.feather || 0, erase: !!o.erase };
      // edge is in the key as well as grow/shrink: two runs could share the derived pair and
      // differ only in how they got there if a stamp were ever half-updated, and coalescing
      // them would silently apply one run's geometry to the other's pixels.
      var key = [k.kind, k.edge, k.grow, k.shrink, k.feather, k.erase ? 1 : 0].join('|');
      if (curKey !== key) { flush(); curKey = key; curParams = k; cur = o; t.clearRect(0, 0, W, H); }
      paintStroke(t, o);
    }
    flush();
    for (var j = 0; j < out.length; j++) if (!out[j].png) out.splice(j--, 1);
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
      kind: el.kind ? el.kind.value : 'retouch',
      prompt: el.prompt ? el.prompt.value : '',
      strength: val('tb_strength', 0.25),
      // Derived, never independent: one bipolar knob is the only way to express this, so the
      // flat-mask path (spike, older editors) cannot enter the both-set closing state either.
      mask_edge: val('tb_edge', 0),
      mask_expand: Math.max(0, val('tb_edge', 0)),
      mask_shrink: Math.max(0, -val('tb_edge', 0)),
      mask_feather: val('tb_feather', 8),
      invert: inverted(),
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
    i.addEventListener('input', function () { out.textContent = i.value; refresh(); });
    wrap.appendChild(i); wrap.appendChild(out);
    el[id] = i;
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
    el.tools.appendChild(tbtn('rect', 'Rect', 'Drag a box'));
    el.tools.appendChild(tbtn('ellipse', 'Ellipse', 'Drag an ellipse'));
    el.tools.appendChild(btn('Close shape', function () {
      if (active && (active.mode === 'poly' || active.mode === 'draw') && active.pts.length > 2) {
        strokes.push(stamp(active));
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

    // MODE IS COSMETIC TODAY: nothing in engine.py reads spec["kind"] -- all five modes
    // run the IDENTICAL repaint graph, so the only things that change the image are the
    // prompt, the seed and the mask geometry. The values are still recorded on the job row
    // (and Darkroom is where deterministic ops will live once they exist), but labels must
    // not promise a differentiation no user can ever observe. The darkroom label this
    // replaced claimed deterministic tone ops on the CPU, while the only path that has
    // ever existed is the GPU Klein inpaint -- a user picking it to save GPU time would
    // have been billed for it.
    // The offline suite pins both halves: that engine still ignores kind, and that this
    // panel carries the caveat.
    root.appendChild(labelled('mode', select('kind', [
      ['retouch', 'Retouch'],
      ['erase', 'Erase'],
      ['inpaint', 'Replace'],
      ['localize_stylize', 'Local style'],
      ['darkroom', 'Darkroom (not implemented)']
    ])));
    root.appendChild(mk('div', 'tb-hint tb-deadnote',
      'all modes run the same repaint graph today — the prompt and the mask are what change ' +
      'the image; deterministic Darkroom ops are not built yet'));
    el.prompt = document.createElement('textarea');
    el.prompt.id = 'tb_prompt'; el.prompt.rows = 2;
    el.prompt.placeholder = 'What to put there (Replace / Erase / Local style). Empty = just clean up.';
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
    var DEAD_KNOBS = ['strength', 'opacity', 'blend_mode', 'color_match', 'preserve_detail', 'variants'];
    var DEAD_EL = { blend_mode: 'blend', color_match: 'colormatch', preserve_detail: 'detail' };
    for (var di = 0; di < DEAD_KNOBS.length; di++) {
      var node = el[DEAD_EL[DEAD_KNOBS[di]] || DEAD_KNOBS[di]];
      if (node) {
        node.disabled = true;
        node.title = 'Recorded with the job, not wired into the render graph yet — changing this does nothing today';
      }
    }
    root.appendChild(mk('div', 'tb-hint tb-deadnote',
      'greyed knobs are recorded with the job but not wired into the render graph yet — ' +
      'only prompt, seed and the mask geometry affect the image today'));
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
        strokes.push(stamp({ mode: 'load', img: im, size: 0, hardness: 1, erase: false }));
        rasterize();
        status('Auto-mask loaded — refine it with brush/eraser, then Preview.');
      };
      im.src = 'data:image/png;base64,' + r.mask_png;
    }).catch(function (e) { status('Auto-mask failed: ' + e.message, 'bad'); });
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

  function pollJob(id, tries, tok) {
    // Poll rather than hold a connection open: an editor session should not depend on a
    // 10-minute in-flight request surviving a daemon restart or a proxy idle timeout.
    if (tries > 400) { status('Render timed out.', 'bad'); return; }
    req('/toolbox/jobs/poll', { job_id: id }, tok).then(function (r) {
      if (r.error) throw new Error(r.error);
      if (r.state === 'done' || r.state === 'error') {
        jobTimer = null; jobId = null; jobToken = null;   // terminal: drop the spent job token
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
               + (r.elapsed_s ? ' · ' + r.elapsed_s.toFixed(0) + 's' : ''), 'ok');
        return;
      }
      status('Rendering… (' + (r.state || 'queued') + ', ' + (tries * 2) + 's)');
      jobTimer = setTimeout(function () { jobTimer = null; pollJob(id, tries + 1, tok); }, 2000);
    }).catch(function (e) { jobTimer = null; status('Render failed: ' + e.message, 'bad'); });
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
        // Handle first, then body, then nothing: grabbing the corner square means scale,
        // grabbing the box means move, and an empty tap must CLEAR the selection —
        // otherwise a hidden selected object keeps silently receiving edits.
        var o = selObj(), grabbed = false;
        if (o) {
          var b = objBBox(o), hs = 12 / scale();
          if (Math.abs(p.x - b.x1) <= hs && Math.abs(p.y - b.y1) <= hs) {
            selDrag = { kind: 'scale', cx: (b.x0 + b.x1) / 2, cy: (b.y0 + b.y1) / 2,
                        r0: Math.max(1, Math.hypot(p.x - (b.x0 + b.x1) / 2,
                                                   p.y - (b.y0 + b.y1) / 2)) };
            grabbed = true;
          } else if (p.x >= b.x0 && p.x <= b.x1 && p.y >= b.y0 && p.y <= b.y1) {
            selDrag = { kind: 'move', last: p };
            grabbed = true;
          }
        }
        if (!grabbed) {
          var hit = hitTest(p);
          if (hit >= 0) { selectObj(hit); selDrag = { kind: 'move', last: p }; }
          else selectObj(-1);
        }
        dragEnabled = !!selDrag;
        try { viewC.setPointerCapture(ev.pointerId); } catch (e) { /* not fatal */ }
        return;
      }
      if (sel >= 0 && selObj()) selectObj(-1);   // any other tool drops the selection
      if (t === 'polygon') {                    // click a vertex; never a drag
        if (!active || active.mode !== 'poly') active = { mode: 'poly', pts: [], size: 0, hardness: 1, erase: false };
        active.pts.push(p); dragEnabled = false; compose(); return;
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
        } else {
          var sr = Math.hypot(sp.x - selDrag.cx, sp.y - selDrag.cy);
          var sf = sr / Math.max(1, selDrag.r0);
          if (Math.abs(sf - 1) > 0.02) { scaleSel(sf); selDrag.r0 = Math.max(1, sr); }
        }
        return;
      }
      // hover (button up): still move the ring so brush/eraser show their footprint
      if (!dragEnabled || !active) {
        if (!jobId && baseC && (tool === 'brush' || tool === 'eraser')) { ringPt = toNatural(ev); drawRing(); }
        return;
      }
      var p = toNatural(ev);
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
      if (tool === 'select') { selDrag = null; return; }
      if (!active) return;
      var m = active.mode;
      if (m === 'free') { if (active.pts.length) strokes.push(stamp(active)); }
      else if (m === 'draw') { if (active.pts.length > 2) strokes.push(stamp(active)); }   // auto-close
      else if (m === 'rect' || m === 'ellipse') {
        var a = active.pts[0], b = active.pts[1];
        if (Math.abs(b.x - a.x) > 2 && Math.abs(b.y - a.y) > 2) strokes.push(stamp(active));
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
      var cap = CFG.max_side || 2048;
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
      status('Ready — ' + W + 'x' + H + '. Paint the area; the mask preview updates live.');
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
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();

