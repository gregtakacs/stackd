/* Browser-side driver for the toolbox editor regression tests.
 *
 * Everything is asserted on OBSERVABLE state, not internals: canvas pixels (what the user
 * sees) and layout metrics (what makes a scrollbar appear). The closure vars are
 * unreachable from here by design — if a test passed only by poking privates it would not
 * catch a regression in the paint -> rasterize -> compose path, which is where both reported
 * bugs actually lived.
 *
 * Pointer events go through the real DOM listener chain, so down/move/up order, the pointer
 * map and every guard run as they do under a finger. Honest limit: this does not exercise
 * Chrome's own gesture/scroll machinery, only our handlers. */
(function () {
  'use strict';
  var out = { tests: [], fetches: [], note: [] };
  function T(name, pass, detail) { out.tests.push({ name: name, pass: !!pass, detail: detail == null ? '' : String(detail) }); }
  var finished = false;
  function finish() {
    if (finished) return; finished = true;
    out.dbg = window.__DBG || null; out.dbgerr = window.__DBGERR || null;
    var p = document.createElement('pre');
    p.id = '__TBRESULT__';
    p.textContent = JSON.stringify(out);
    document.body.appendChild(p);
  }
  function fail(e) { out.note.push('THROW ' + ((e && e.stack) || e)); finish(); }

  /* fetch stub. The preview answer is unmistakable magenta: the photo is a red->blue
   * gradient with a fixed green channel of 40 and the local wash is translucent red, so
   * magenta can ONLY be the image the server sent back. Seeing it in #tb_view therefore
   * proves the live server preview is what got composited, with no button pressed. */
  window.fetch = function (url, opts) {
    var body = {};
    try { body = JSON.parse((opts && opts.body) || '{}'); } catch (e) {}
    b64s.push(body.mask_png || ''); window.__B64 = b64s;
    // Keep the layers so assertions can inspect the per-object contract, but replace each
    // PNG with an equal-length run of 'x': assertions need truthiness + length, and the
    // dumped result JSON must not carry hundreds of KB of base64.
    var layersForLog = null;
    if (body.layers) {
      layersForLog = [];
      for (var _li = 0; _li < body.layers.length; _li++) {
        var _l = body.layers[_li] || {};
        var _ph = '', _n = (_l.png || '').length;
        while (_ph.length < _n) _ph += 'x';
        layersForLog.push({ kind: _l.kind, edge: _l.edge, grow: _l.grow, shrink: _l.shrink,
                            feather: _l.feather, erase: _l.erase, png: _ph });
      }
    }
    out.fetches.push({ url: String(url), keys: Object.keys(body).sort().join(','),
                       mask_len: (body.mask_png || '').length,
                       layers: layersForLog,
                       click_pts: (body.points || []).length,
                       click_neg: (body.negative_points || []).length,
                       overlay_alpha: (body.params || {}).overlay_alpha,
                       click_pts_json: JSON.stringify(body.points || []),
                       click_neg_json: JSON.stringify(body.negative_points || []),
                       auth: ((opts || {}).headers || {}).authorization || '' });
    if (/mask\/preview$/.test(String(url))) {
      var c = document.createElement('canvas'); c.width = 12; c.height = 12;
      var g = c.getContext('2d'); g.fillStyle = 'rgb(255,0,255)'; g.fillRect(0, 0, 12, 12);
      var b64 = c.toDataURL('image/png').split(',')[1];
      return Promise.resolve({ ok: true, status: 200, json: function () {
        return Promise.resolve({ ok: true, overlay_png: b64, coverage: 0.0314,
                                 info: { size: [160, 320] }, empty: false, tiny: false });
      }});
    }
    if (String(url).indexOf('mask/click') !== -1) {
      // A left-half opaque mask: unmistakably NOT a default and NOT empty, so committing it
      // as a load layer must show solid ink on the left, none on the right. If the click
      // path regressed to unioning or no-op, this asymmetry is what catches it.
      var cc = document.createElement('canvas'); cc.width = 16; cc.height = 16;
      var gg = cc.getContext('2d'); gg.fillStyle = 'rgba(255,255,255,1)'; gg.fillRect(0, 0, 8, 16);
      var cb = cc.toDataURL('image/png').split(',')[1];
      return Promise.resolve({ ok: true, status: 200, json: function () {
        return Promise.resolve({ ok: true, mask_png: cb, coverage: 0.5, empty: false });
      }});
    }
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ ok: true }); } });
  };
  var b64s = [];   // side map: base64 stays OUT of the dumped JSON (a multi-KB
                      // unbreakable token inside the result <pre> would itself widen the page
                      // and every page-level measurement would be measuring the fixture).

  var view, ring, stage, box;
  var mark = 0, base1 = null, magBefore = -1, magDuring = -1;   // previews seen before the gesture under test
  function byText(txt) {
    var g = document.querySelectorAll('button');
    for (var i = 0; i < g.length; i++) if (g[i].textContent.trim() === txt) return g[i];
    return null;
  }
  function toolButton(t) {
    var g = document.querySelectorAll('button[data-tool]');
    for (var i = 0; i < g.length; i++) if (g[i].getAttribute('data-tool') === t) return g[i];
    return null;
  }
  function px(c) { return c.getContext('2d').getImageData(0, 0, c.width, c.height).data; }
  function magentaCount(c) {
    var d = px(c), n = 0;
    for (var i = 0; i < d.length; i += 4) if (d[i] > 200 && d[i + 1] < 60 && d[i + 2] > 200 && d[i + 3] > 200) n++;
    return n;
  }
  var refData = null;
  function setReference() {   // the photo alone, at the canvas's own size
    var im = new Image();
    im.onload = function () {
      var c = document.createElement('canvas'); c.width = view.width; c.height = view.height;
      c.getContext('2d').drawImage(im, 0, 0, view.width, view.height);
      refData = px(c);
    };
    im.src = (window.__TB__ || {}).image;
  }
  function inkCount(c) {
    if (!refData || !c || !c.width) return -1;
    var d = px(c), n = 0;
    for (var i = 0; i < d.length; i += 4) {
      if (Math.abs(d[i] - refData[i]) > 14 || Math.abs(d[i + 1] - refData[i + 1]) > 14 ||
          Math.abs(d[i + 2] - refData[i + 2]) > 14) n++;
    }
    return n;
  }
  function nonEmpty(c) {
    if (!c || !c.width) return false;
    var d = px(c);
    for (var i = 3; i < d.length; i += 4) if (d[i] > 8) return true;
    return false;
  }
  function clientPt(nx, ny) {
    var r = view.getBoundingClientRect();
    return { clientX: r.left + nx * (r.width / view.width), clientY: r.top + ny * (r.height / view.height) };
  }
  function fire(type, id, nx, ny, o) {
    o = o || {};
    var p = clientPt(nx, ny);
    view.dispatchEvent(new PointerEvent(type, {
      pointerId: id, pointerType: o.pointerType || 'touch', isPrimary: !!o.isPrimary,
      bubbles: true, cancelable: true, clientX: p.clientX, clientY: p.clientY,
      shiftKey: !!o.shiftKey, altKey: !!o.altKey, ctrlKey: !!o.ctrlKey, metaKey: !!o.metaKey,
      button: 0, buttons: o.buttons == null ? 1 : o.buttons
    }));
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  // Coverage stats over a rect of the EXPORTED mask (that is literally maskC), which is the
  // only artifact the render consumes -- checking the composited view would let a pretty
  // overlay hide a hole in the actual mask.
  function maskStats(b64, x, y, w, h) {
    return new Promise(function (res) {
      if (!b64) return res(null);
      var im = new Image();
      im.onload = function () {
        var c = document.createElement('canvas'); c.width = im.width; c.height = im.height;
        var g = c.getContext('2d'); g.drawImage(im, 0, 0);
        var d = g.getImageData(x, y, w, h).data, sum = 0, solid = 0, any = 0, n = d.length / 4;
        for (var i = 0, j = 3; i < n; i++, j += 4) { sum += d[j]; if (d[j] > 8) any++; if (d[j] >= 240) solid++; }
        res({ sum: sum, solid: solid, any: any, px: n });
      };
      im.onerror = function () { res(null); };
      im.src = 'data:image/png;base64,' + b64;
    });
  }
  function clearForDraw() { var c = byText('Clear'); if (c) c.click(); }
  function lastB64() { var a = (window.__B64 || []); return a[a.length - 1] || ''; }
  // measure the EXPORTED mask itself (that is literally maskC): alpha>8 pixels tell us
  // whether the stroke ever reached the bitmap, independent of any compositing question
  function maskInk(b64) {
    return new Promise(function (res) {
      if (!b64) return res(-1);
      var im = new Image();
      im.onload = function () {
        var c = document.createElement('canvas'); c.width = im.width; c.height = im.height;
        var g = c.getContext('2d'); g.drawImage(im, 0, 0);
        var d = g.getImageData(0, 0, c.width, c.height).data, n = 0;
        for (var i = 3; i < d.length; i += 4) if (d[i] > 8) n++;
        res(n);
      };
      im.onerror = function () { res(-2); };
      im.src = 'data:image/png;base64,' + b64;
    });
  }
  function maskLens() {
    var a = out.fetches.filter(function (f) { return /mask\/preview$/.test(f.url); });
    return a.length ? a[a.length - 1].mask_len : -1;
  }
  function previews() { return out.fetches.filter(function (f) { return /mask\/preview$/.test(f.url); }); }

  var steps = [];
  function step(name, fn) { steps.push({ name: name, fn: fn }); }
  /*__PART2__*/

  function run(i) {
    if (i >= steps.length) { finish(); return; }
    // #shot halts right after the paint+preview asserts so a screenshot catches the mask
    // still on screen (the later Clear step would leave a blank photo to photograph)
    if (location.hash.indexOf('shot') >= 0 && steps[i].name === 'zoom cycle') { finish(); return; }
    try {
      var r = steps[i].fn();
      if (r && r.then) return r.then(function () { run(i + 1); }, fail);
      return run(i + 1);
    } catch (e) { return fail(e); }
  }
  function start() { run(0); }
  if (document.readyState === 'complete') setTimeout(start, 400);
  else window.addEventListener('load', function () { setTimeout(start, 400); });
  setTimeout(function () { out.note.push('TIMEOUT'); finish(); }, 60000);
})();
