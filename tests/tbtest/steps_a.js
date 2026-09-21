  step('boot', function () {
    view = document.getElementById('tb_view');
    setReference();                                   // photo alone, for pixel comparison
    ring = document.getElementById('tb_ring');
    stage = document.querySelector('.tb-stage');
    box = document.querySelector('.tb-canvasbox');
    T('editor booted (view canvas sized)', !!view && view.width > 0, view && (view.width + 'x' + view.height));
    T('ring canvas overlays the photo at the same size',
      !!ring && !!view && ring.width === view.width && ring.height === view.height,
      ring && (ring.width + 'x' + ring.height));
    var st = (document.querySelector('.tb-status') || {}).textContent || '';
    T('status no longer tells the user to press Preview', !/then Preview mask/i.test(st), st);
    T('Undo/Redo/Clear/Close-shape buttons exist',
      !!(byText('Undo') && byText('Redo') && byText('Clear') && byText('Close shape')));
    T('separate lasso and polygon tools exist', !!(toolButton('lasso') && toolButton('polygon')));
    return true;
  });

  // the reference photo decodes asynchronously; measuring before it lands reports -1,
  // which is a silent no-op rather than a false pass -- but it tests nothing either
  step('reference ready', function () { return wait(350); });

  // Guards the attribute-before-value ordering bug: a range input created without its
  // min/max/step snaps fractional defaults to whole numbers, which silently zeroed the
  // mask overlay and pinned hardness. Asserting the DOM value is the cheapest possible net.
  step('slider defaults', function () {
    // M2.1 review retired strength/variants/blend from the panel (frozen constants in
    // params()) and zeroed colour match / detail — a no-op default is the whole point
    // (non-zero defaults silently repainted every artifact, the "worse than ComfyUI's
    // own copy" bug). wash/hardness keep FRACTIONAL defaults: that half still catches
    // the attribute-before-value snap bug this step exists for.
    ['wash:0.45', 'hardness:0.55', 'opacity:1', 'detail:0', 'colormatch:0'].forEach(function (spec) {
      var id = spec.split(':')[0], want = spec.split(':')[1];
      var n = document.getElementById('tb_' + id);
      T('slider tb_' + id + ' keeps its default ' + want,
        !!n && Math.abs(parseFloat(n.value) - parseFloat(want)) < 1e-9, n ? n.value : 'MISSING');
    });
    ['tb_strength', 'tb_variants', 'tb_blend'].forEach(function (id) {
      T(id + ' retired from the panel (frozen constant, no theatre control)',
        !document.getElementById(id), 'still present');
    });
  });

  step('ring on hover', function () { fire('pointermove', 1, 40, 40, { isPrimary: true, buttons: 0 }); });
  step('ring drawn', function () { T('brush cursor ring appears on hover', nonEmpty(ring)); });

  step('knobs to hard defaults', function () {
    // feather 8 is the shipped slider default, and a numbered brush now COMMITS under
    // the auto wire rules (that is the honest new contract). This first block is about
    // HARD ink and the edge-axis only - so take the feather out of the equation at
    // rest, before anything is painted.
    var f = document.getElementById('tb_feather');
    if (f) { f.value = '0'; f.dispatchEvent(new Event('input', { bubbles: true })); }
    var e = document.getElementById('tb_edge');
    if (e) { e.value = '0'; e.dispatchEvent(new Event('input', { bubbles: true })); }
  });
  step('paint', function () {
    out.note.push('DBG ref=' + (refData ? 'yes' : 'NO') +
                  ' viewSize=' + view.width + 'x' + view.height +
                  ' css=' + JSON.stringify(view.getBoundingClientRect()) +
                  ' inkBefore=' + inkCount(view));
    fire('pointerdown', 2, 30, 40, { isPrimary: true });
    out.note.push('DBG afterDown ink=' + inkCount(view));
    for (var i = 0; i < 14; i++) fire('pointermove', 2, 30 + i * 3, 40 + i * 2, {});
    out.note.push('DBG afterMoves ink=' + inkCount(view));
    fire('pointerup', 2, 70, 65, {});
    out.note.push('DBG afterUp ink=' + inkCount(view));
  });
  step('paint shows ink', function () {
    out.note.push('DBGREG ' + JSON.stringify(window.ToolboxEditor && window.ToolboxEditor.state && window.ToolboxEditor.state()));
    T('a brush stroke paints (visible without any server round-trip)', inkCount(view) > 200,
      'ink=' + inkCount(view) + ' magenta=' + magentaCount(view));
  });

  // A SHAPE, in addition to the brush stroke, because the per-object geometry rules differ
  // by kind: a brush must carry no edge at all (hardness owns that edge) while a shape must
  // carry the user's signed edge. With only a brush on the canvas the shape invariants below
  // have no subject and pass vacuously -- which is exactly what happened to the first draft
  // of them (both the two-slider and no-derive mutants sailed through 49/49).
  step('draw a rect', function () {
    var b = toolButton('rect'); if (b) b.click();
    // Set the knob BEFORE drawing: the global sliders are defaults for the NEXT object, so a
    // value applied afterwards would legitimately not touch an already-committed shape and the
    // assertion would be measuring the wrong thing entirely.
    var e = document.getElementById('tb_edge');
    if (e) { e.value = '-40'; e.dispatchEvent(new Event('input', { bubbles: true })); }
    return wait(120);
  });
  step('rect drag', function () {
    fire('pointerdown', 90, 20, 120, { isPrimary: true });
    for (var i = 0; i < 8; i++) fire('pointermove', 90, 20 + i * 5, 120 + i * 4, {});
    fire('pointerup', 90, 60, 152, {});
    return wait(1200);
  });
  step('rect committed', function () {
    var L = (previews().slice(-1)[0] || {}).layers || [];
    T('the rect is on the canvas as its own layer (so shape rules have a subject)',
      L.length >= 2 && L.some(function (l) { return l.kind === 'shape'; }),
      JSON.stringify(L.map(function (l) { return { kind: l.kind, edge: l.edge,
        grow: l.grow, shrink: l.shrink }; })));
  });
  step('edge negative asserts', function () {
    var L = (previews().slice(-1)[0] || {}).layers || [];
    var shape = null, brush = null;
    for (var i = 0; i < L.length; i++) {
      if (!shape && L[i].kind === 'shape') shape = L[i];
      else if (!brush && L[i].kind === 'brush') brush = L[i];
    }
    T('a shape layer carries the signed edge the user set',
      !!shape && Number(shape.edge) === -40 && Number(shape.shrink) === 40 &&
      Number(shape.grow) === 0,
      JSON.stringify(shape && { edge: shape.edge, grow: shape.grow, shrink: shape.shrink }));
    T('the same negative edge leaves the brush stroke untouched (edge 0)',
      !!brush && Number(brush.edge) === 0 && Number(brush.grow) === 0 &&
      Number(brush.shrink) === 0,
      JSON.stringify(brush && { edge: brush.edge, grow: brush.grow, shrink: brush.shrink }));
    var e = document.getElementById('tb_edge');
    if (e) { e.value = '0'; e.dispatchEvent(new Event('input', { bubbles: true })); }
    // Hand the tool back: the self-overlap steps that follow assume the brush, and leaving
    // this block on 'rect' silently turned their strokes into rectangles.
    var bt = toolButton('brush'); if (bt) bt.click();
    return wait(900);
  });

  step('live preview settles', function () {
    return wait(1000).then(function () {
      var i = previews().length - 1;
      return maskInk((window.__B64 || [])[i] || '').then(function (alpha) {
        out.note.push('DBG exported-mask alpha px = ' + alpha);
      });
    });
  });
  step('live preview asserts', function () {
    var pv = previews();
    T('preview fired on its own — no button (the Preview mask affordance is retired)', pv.length >= 1,
      JSON.stringify(out.fetches.map(function (f) { return f.url.replace(/^.*\/toolbox/, ''); })));
    T('preview body carries the real API contract',
      pv.length && /^image,image_id,layers,mask_png,params$/.test(pv[0].keys), pv[0] && pv[0].keys);
    // The per-object contract: layers must be a real array of per-kind PNG layers, not
    // merely a key that exists. mask_png is still sent as the compatibility path for a
    // server (or spike) that does not understand layers -- both must be present.
    T('layers carry kind + geometry, and the brush layer is unfeathered',
      (function () {
        var L = pv[0].layers;
        if (!L || !L.length) return false;
        for (var i = 0; i < L.length; i++) {
          var l = L[i];
          if (!l.png || ['brush', 'shape', 'auto'].indexOf(l.kind) < 0) return false;
          if (typeof l.grow !== 'number' || typeof l.shrink !== 'number') return false;
          // The whole point of the refactor: a brush layer must never ask for a server
          // blur, because hardness already chose that edge and blurring a binarized mask
          // shrinks the region instead of softening it.
          if (l.kind === 'brush' && Number(l.feather) !== 0) return false;
        }
        return true;
      })(), pv[0] && JSON.stringify((pv[0].layers || []).map(function (l) {
        return { kind: l.kind, grow: l.grow, shrink: l.shrink, feather: l.feather,
                 erase: !!l.erase, png: (l.png || '').length }; })));
    // The merged bipolar control sends ONE signed intent plus the derived legacy pair. They
    // must never disagree: grow and shrink are now names for the same number, so a layer
    // with both non-zero would mean the client is still able to express the old hidden
    // closing state (grow 12 + shrink 12 filled a notch: 9704 -> 10251 px), and a layer
    // whose pair contradicts its sign would render differently on a server that reads
    // `edge` from one that still reads grow/shrink. Only the client derives these, so this
    // is checkable here and nowhere else.
    T('every layer sends one signed edge with a consistent legacy pair',
      (function () {
        var L = pv[0].layers || [];
        if (!L.length) return false;
        for (var i = 0; i < L.length; i++) {
          var l = L[i], e = Number(l.edge), g = Number(l.grow), r = Number(l.shrink);
          if (l.kind === 'brush') {
            if (e !== 0 || g !== 0 || r !== 0) return false;      // hardness owns that edge
            continue;
          }
          if (!isFinite(e) || !isFinite(g) || !isFinite(r)) return false;
          if (g < 0 || r < 0) return false;                        // magnitudes only
          if (e > 0 && (g !== e || r !== 0)) return false;
          if (e < 0 && (r !== -e || g !== 0)) return false;
          if (e === 0 && (g !== 0 || r !== 0)) return false;       // no hidden closing
        }
        return true;
      })(), pv[0] && JSON.stringify((pv[0].layers || []).map(function (l) {
        return { kind: l.kind, edge: l.edge, grow: l.grow, shrink: l.shrink }; })));
    T('the merged edge control is a single bipolar slider, not two',
      (function () {
        var e = document.getElementById('tb_edge');
        if (!e || e.type !== 'range') return false;
        if (document.getElementById('tb_expand') || document.getElementById('tb_shrink'))
          return false;                       // the cancel-each-other pair must be gone
        return Number(e.min) < 0 && Number(e.max) > 0 && Number(e.value) === 0;
      })(), 'tb_edge=' + (function () {
        var e = document.getElementById('tb_edge');
        return e ? e.min + '..' + e.max + ' v=' + e.value : 'absent';
      })() + ' expand=' + !!document.getElementById('tb_expand')
         + ' shrink=' + !!document.getElementById('tb_shrink'));
    T('mask_png compatibility path is still populated',
      pv.length && pv[0].mask_len > 100, 'mask_len=' + (pv[0] && pv[0].mask_len));
    T('preview is sent with the launch token', pv.length && pv[0].auth === 'Bearer TESTTOKEN', pv[0] && pv[0].auth);
    T('the SERVER overlay is what the canvas shows', magentaCount(view) > 100, 'magenta=' + magentaCount(view));
    var cov = document.querySelector('.tb-zoom .tb-hint');
    T('coverage readout populated from the server number', cov && /3%/.test(cov.textContent), cov && cov.textContent);
  });

  // Environment canary: if this Chrome uses overlay scrollbars, a vertical bar steals no
  // width and the Fit assertions below can only ever pass -- a vacuous green. Refuse to
  // pretend: prove the environment can exhibit the bug before crediting the fix.
  step('preview button retired asserts', function () {
    T('the Preview mask button is GONE from the actions row', !byText('Preview mask'),
      [].slice.call(document.querySelectorAll('button')).map(function (b) { return b.textContent; }).join(','));
    // The empty verdict the retired button used to deliver by hand must now arrive on
    // the AUTOMATIC round-trip: force the stub to answer 'empty' (a paint so small the
    // server calls it zero) and watch the status line, unprompted, say NOTHING SELECTED.
    window.__PVEMPTY = true;
    knobSet('tb_wash', 0.4);
    return wait(700);
  });
  step('empty verdict surfaces unprompted', function () {
    var st = (document.querySelector('.tb-status') || {}).textContent || '';
    out.note.push('PVEMPTY status=' + st.slice(0, 90));
    T('the live round-trip itself reports a zero mask as a FAILURE',
      /NOTHING SELECTED/.test(st), st.slice(0, 120));
    window.__PVEMPTY = false; knobSet('tb_wash', 0.45);  // shipped default
    return wait(150);
  });
  step('env canary', function () {
    var d = document.createElement('div');
    d.style.cssText = 'width:120px;height:80px;overflow:scroll;position:absolute;top:0;left:0;visibility:hidden';
    document.body.appendChild(d);
    var bar = d.offsetWidth - d.clientWidth;
    d.parentNode.removeChild(d);
    out.env = { scrollbarWidth: bar };
    T('environment has CLASSIC scrollbars (so the Fit test is not vacuous)', bar >= 8,
      'vertical bar takes ' + bar + 'px');
  });

  step('fit metrics', function () {
    T('no horizontal overflow at Fit', stage.scrollWidth - stage.clientWidth <= 1,
      'scrollWidth-clientWidth=' + (stage.scrollWidth - stage.clientWidth));
    // the stage is not the page: the user sees the DOCUMENT's bars. Measuring only the
    // stage let a photo wider than the viewport pass every stage assertion.
    T('no horizontal overflow on the PAGE',
      document.documentElement.scrollWidth - document.documentElement.clientWidth <= 1,
      'doc ' + document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth);
    var cw = document.documentElement.clientWidth, wide = [];
    var all = document.querySelectorAll('body *');
    for (var i = 0; i < all.length; i++) {
      var e = all[i], r = e.getBoundingClientRect();
      if (r.right > cw + 1 && wide.length < 6)
        wide.push((e.tagName + '.' + (e.className || '')).slice(0, 46) + '@' + Math.round(r.right));
    }
    out.wide = wide;
    T('the test-result <pre> is not what widens the page', !/^PRE/.test(wide[0] || ''), JSON.stringify(wide));
  });

  step('zoom cycle', function () {
    var z = byText('Zoom +');
    if (!z) { T('zoom controls present', false); return true; }
    for (var i = 0; i < 4; i++) z.click();
    return wait(80);
  });
  step('zoomed metrics', function () {
    T('zoom > 1 enlarges the photo (scroll to reach the rest is intended)',
      box.getBoundingClientRect().width > stage.clientWidth - 2,
      'box=' + Math.round(box.getBoundingClientRect().width) + ' stage=' + stage.clientWidth);
    var f = byText('Fit'); if (f) f.click();
    return wait(80);
  });
  step('refit metrics', function () {
    T('Fit clears horizontal overflow again', stage.scrollWidth - stage.clientWidth <= 1,
      'delta=' + (stage.scrollWidth - stage.clientWidth));
    T('no scrollbar ratchet after a zoom cycle', stage.clientWidth > 200, 'clientWidth=' + stage.clientWidth);
  });

  // The blend dropdown shipped with BLANK options: select() read opts[i][1] for
  // callers that passed bare values, so every textContent was undefined. The control
  // was then RETIRED outright (M2.1 review): inside a hard-gated painted selection the
  // paste is always Normal, so a blend legend over eight modes was theatre. This step
  // now guards the retirement: the removed controls must NOT come back, the kept ones
  // stay enabled, and no ghost dead-knob note may describe greyed controls that no
  // longer exist (the note only earns its place when something is really disabled).
  step('inert knobs are honest', function () {
    ['tb_blend', 'tb_variants', 'tb_strength'].forEach(function (id) {
      T(id + ' stays retired (params() freezes it)', !document.getElementById(id),
        'control is back');
    });
    ['tb_opacity', 'tb_detail', 'tb_colormatch'].forEach(function (id) {
      var n = document.getElementById(id);
      T(id + ' is enabled: wired into paste_back', !!n && n.disabled === false,
        n ? 'disabled=' + n.disabled : 'missing');
    });
    // The deadnote must be CONDITIONAL: DEAD_KNOBS keeps strength/variants as the
    // registry the smoke suite fails on if they ever reappear enabled-but-unwired, but
    // no such control is on screen, so a panel that still says "greyed knobs are
    // recorded..." would be describing a legend for nothing. Query the NODE, not
    // body.textContent: the driver script is inlined in this same document, so its own
    // quoted phrases poison any textContent search (measured: self-match false FAIL).
    T('no ghost dead-knob note while every on-screen control is live',
      !document.querySelector('.tb-deadknobs'),
      document.querySelector('.tb-deadknobs') &&
      document.querySelector('.tb-deadknobs').textContent.slice(0, 60));
    T('select() labels a two-value option pair (fallback for future dropdowns) — ' +
      'shipped select() must still handle [value, label]',
      /opts\[i\]\.length\s*>\s*1/.test(document.documentElement.innerHTML) ||
      !document.querySelector('select'));
    // (tb_edge is deliberately, permanently disabled now — it is the hidden default
    // store; the Selection inspector is the only edge/feather UI. Seed proves a genuinely
    // live control still ships enabled.)
    T('live controls are NOT disabled',
      (document.getElementById('tb_seed') || {}).disabled === false);
    T('the retired panel knobs stay hidden AND dead (never a second edge UI)',
      (document.getElementById('tb_edge') || {}).disabled === true &&
      (document.getElementById('tb_feather') || {}).disabled === true &&
      (document.getElementById('tb_edge') || {parentNode:{style:{}}}).parentNode.style.display === 'none');
    return true;
  });

  // ---- smart select (SAM3 click-to-object) ----
  // The whole point of the feature: a click POSTs the normalized point(s), and the returned
  // mask commits as ONE editable 'auto' layer (not a union of prior guesses). The fetch stub
  // answers /mask/click with a left-half mask, so an 'auto' layer genuinely appearing (and
  // disappearing on Undo) proves the load-stroke seam is wired, not just that a fetch fired.
  function decodeLayer(l) {
    return new Promise(function (res) {
      if (!l || !l.png) return res(null);
      var im = new Image();
      im.onload = function () {
        var c = document.createElement('canvas'); c.width = im.width; c.height = im.height;
        var g = c.getContext('2d'); g.drawImage(im, 0, 0);
        var d = g.getImageData(0, 0, im.width, im.height).data;
        var any = 0, x0 = 1e9, y0 = 1e9, x1 = -1, y1 = -1, x, y;
        for (y = 0; y < im.height; y++) for (x = 0; x < im.width; x++)
          if (d[(y * im.width + x) * 4 + 3] > 8) {
            any++; if (x < x0) x0 = x; if (y < y0) y0 = y; if (x > x1) x1 = x; if (y > y1) y1 = y;
          }
        res({ kind: l.kind, w: im.width, h: im.height, any: any, px: im.width * im.height,
              bb: any ? [x0, y0, x1, y1] : null });
      };
      im.onerror = function () { res(null); };
      im.src = 'data:image/png;base64,' + l.png;
    });
  }
  function lastRealLayers(urlRe) {
    var q = window.__LREAL || [];
    for (var i = q.length - 1; i >= 0; i--) if (urlRe.test(q[i].url)) return q[i].layers;
    return null;
  }
  step('smart select tool + plain click selects the object', function () {
    clearForDraw();
    var b = toolButton('smart');
    T('Smart select tool button exists', !!b);
    if (!b) return true;
    b.click();
    T('smart tool activates (button shows active state)', b.classList.contains('tb-on'), b.className);
    fire('pointerdown', 300, Math.round(view.width / 2), Math.round(view.height / 2), { isPrimary: true });
    fire('pointerup', 300, Math.round(view.width / 2), Math.round(view.height / 2), {});
    return wait(1600).then(function () {
      var c = out.fetches.filter(function (f) { return /mask\/click$/.test(f.url); });
      T('the click reached /toolbox/mask/click', c.length >= 1, 'n=' + c.length);
      T('the click request carried >=1 positive point and 0 negatives',
        c.length && c[0].click_pts >= 1 && c[0].click_neg === 0,
        c[0] && ('pts=' + c[0].click_pts + ' neg=' + c[0].click_neg + ' P=' + c[0].click_pts_json + ' N=' + c[0].click_neg_json));
      T('the click was authenticated (launch token)', c.length && /^Bearer /.test(c[0].auth), c[0] && c[0].auth);
      var L = ((previews().slice(-1)[0]) || {}).layers || [];
      var anyAuto = previews().some(function (pv) {
        return (pv.layers || []).some(function (l) { return l.kind === 'auto'; });
      });
      T('the selection landed as ONE editable auto layer',
        anyAuto, 'last=' + JSON.stringify(L.map(function (l) { return l.kind; })));
      // The reported defect's wire-level witness: a layer shipped as a bbox CROP gets
      // LANCZOS-stretched over the whole photo by masks._layer_coverage (it resamples any
      // png whose size disagrees), so the smart-select's left-half mask painted the
      // ENTIRE image. Every layer must cross the wire at full-canvas size, with its ink
      // where the user's object is — not smeared to fill its own bounding box.
      var wire = lastRealLayers(/mask\/preview$/);
      var autoL = null;
      if (wire) for (var wi = 0; wi < wire.length; wi++)
        if (wire[wi].kind === 'auto') autoL = wire[wi];
      return decodeLayer(autoL).then(function (d) {
        T('the auto layer ships FULL-CANVAS with its ink in place (never a stretched crop)',
          !!d && d.w === view.width && d.h === view.height &&
          d.any / d.px > 0.2 && d.any / d.px < 0.75 &&
          d.bb && d.bb[0] <= 2 && d.bb[2] <= view.width * 0.65,
          d ? JSON.stringify(d) : 'no auto layer on the wire');
      });
    });
  });
  step('smart select: alt+click adds a negative point', function () {
    var b = toolButton('smart'); if (b && !b.classList.contains('tb-on')) b.click();
    fire('pointerdown', 320, Math.round(view.width / 2), Math.round(view.height * 0.85),
         { isPrimary: true, altKey: true });
    fire('pointerup', 320, Math.round(view.width / 2), Math.round(view.height * 0.85), { altKey: true });
    return wait(1600).then(function () {
      var c = out.fetches.filter(function (f) { return /mask\/click$/.test(f.url); });
      var last = c[c.length - 1];
      T('an alt+click re-sent the WHOLE selection incl. a negative point',
        !!last && last.click_neg >= 1 && last.click_pts >= 1,
        last && ('pts=' + last.click_pts + ' neg=' + last.click_neg));
    });
  });
  step('smart select layer is undoable', function () {
    var u = byText('Undo'); if (u) u.click();            // pop the auto layer
    return wait(500).then(function () {
      // Prove it is a NORMAL stack entry: an empty mask fires no preview (by design), so we
      // can't assert the post-undo preview is empty. Instead paint a brush stroke now — if
      // Undo had left a ghost auto layer, it would reappear in the layers alongside the brush.
      var b = toolButton('brush'); if (b) b.click();
      var fz = document.getElementById('tb_feather');
      if (fz) { fz.value = '0'; fz.dispatchEvent(new Event('input', { bubbles: true })); }
      // hard ink here: the gate is about the GHOST, and a feathered brush would
      // legitimately ship as an auto layer and drown it in false positives.
      fire('pointerdown', 400, 30, 30, { isPrimary: true });
      for (var i = 0; i < 6; i++) fire('pointermove', 400, 30 + i * 6, 30 + i * 4, {});
      fire('pointerup', 400, 70, 60, {});
      return wait(1200);
    }).then(function () {
      var L = ((previews().slice(-1)[0]) || {}).layers || [];
      var kinds = L.map(function (l) { return l.kind; });
      T('Undo removed the auto layer (a later brush leaves no auto ghost)',
        kinds.indexOf('auto') < 0 && kinds.indexOf('brush') >= 0, JSON.stringify(kinds));
    });
  });

  // ---- smart-select object model + manual/auto lasso + overlay opacity (the follow-up) ----
  function clickReqs() {
    return out.fetches.filter(function (f) { return /mask\/click$/.test(f.url); });
  }
  function lastPreviewKinds() {
    var L = ((previews().slice(-1)[0]) || {}).layers || [];
    return L.map(function (l) { return l.kind; });
  }

  step('smart: each plain click is a NEW object; shift accumulates on the current one', function () {
    clearForDraw();
    var b = toolButton('smart'); if (b) b.click();
    var base = clickReqs().length;   // ignore clicks from earlier steps
    function tap(id, fx, fy, mods) {
      fire('pointerdown', id, Math.round(view.width * fx), Math.round(view.height * fy),
           Object.assign({ isPrimary: true }, mods || {}));
      fire('pointerup', id, Math.round(view.width * fx), Math.round(view.height * fy), mods || {});
    }
    tap(900, 0.30, 0.40);                              // object A: 1 positive
    return wait(1100).then(function () {
      tap(901, 0.34, 0.44, { shiftKey: true });        // refine A: +1 positive -> 2
      return wait(1100);
    }).then(function () {
      tap(902, 0.62, 0.50);                            // object B: a FRESH selection -> back to 1
      return wait(1100);
    }).then(function () {
      var c = clickReqs().slice(base).map(function (f) { return f.click_pts; });
      T('plain-click starts a new object (1pt), shift accumulates (2pt), next plain click resets (1pt)',
        c.length >= 3 && c[0] === 1 && c[1] === 2 && c[c.length - 1] === 1,
        JSON.stringify(c));
    });
  });

  step('smart Auto-lasso drags a loop and re-infers via SAM3 (a click request with interior points)', function () {
    clearForDraw();
    var b = toolButton('smart'); if (b) b.click();
    var sm = document.querySelector('button[data-smartmode]');
    if (sm && /Manual/.test(sm.textContent)) sm.click();          // force Auto
    var before = clickReqs().length;
    var cx = Math.round(view.width * 0.3), cy = Math.round(view.height * 0.4);
    fire('pointerdown', 910, cx, cy, { isPrimary: true });
    // trace a loose box well beyond a 6px move so it is a lasso, not a tap
    var loop = [[-30,-30],[40,-30],[40,40],[-30,40],[-30,-30]];
    for (var i = 0; i < loop.length; i++)
      fire('pointermove', 910, cx + loop[i][0], cy + loop[i][1], {});
    fire('pointerup', 910, cx + loop[0][0], cy + loop[0][1], {});
    return wait(1200).then(function () {
      var c = clickReqs();
      T('an Auto loop fired /mask/click with interior seed points',
        c.length > before && c[c.length - 1].click_pts >= 1,
        'new=' + (c.length - before) + ' pts=' + (c[c.length - 1] || {}).click_pts);
    });
  });

  step('smart Manual-lasso commits the traced pixels as a shape (no SAM3 round-trip)', function () {
    clearForDraw();
    var b = toolButton('smart'); if (b) b.click();
    var sm = document.querySelector('button[data-smartmode]');
    if (sm && /Auto/.test(sm.textContent)) sm.click();            // force Manual
    var before = clickReqs().length;
    var cx = Math.round(view.width * 0.6), cy = Math.round(view.height * 0.5);
    fire('pointerdown', 920, cx, cy, { isPrimary: true });
    var loop = [[-30,-30],[45,-30],[45,45],[-30,45],[-30,-30]];
    for (var i = 0; i < loop.length; i++)
      fire('pointermove', 920, cx + loop[i][0], cy + loop[i][1], {});
    fire('pointerup', 920, cx + loop[0][0], cy + loop[0][1], {});
    return wait(700).then(function () {
      var c = clickReqs();
      T('a Manual loop fired NO new /mask/click (pixel-accurate, no second guess)',
        c.length === before, 'before=' + before + ' after=' + c.length);
      T('a Manual loop left a shape layer on the canvas',
        lastPreviewKinds().indexOf('shape') >= 0, JSON.stringify(lastPreviewKinds()));
      // hand the tool back and restore Auto for anything after
      if (sm && /Manual/.test(sm.textContent)) sm.click();
      var br = toolButton('brush'); if (br) br.click();
    });
  });

  step('the auto selection is selectable and offers grow/shrink + feather', function () {
    clearForDraw();
    var b = toolButton('smart'); if (b) b.click();
    // The stub answers /mask/click with a LEFT-half mask, so the object lives at x<0.5W.
    fire('pointerdown', 930, Math.round(view.width * 0.25), Math.round(view.height * 0.5), { isPrimary: true });
    fire('pointerup', 930, Math.round(view.width * 0.25), Math.round(view.height * 0.5), {});
    return wait(1200).then(function () {
      var st = toolButton('select'); if (st) st.click();
      // (1) tap FAR OUTSIDE the object to DESELECT. This only hides the inspector if the
      // select down()/hitTest path runs to completion on a load stroke — the real test.
      fire('pointerdown', 931, Math.round(view.width * 0.92), Math.round(view.height * 0.06), { isPrimary: true });
      fire('pointerup', 931, Math.round(view.width * 0.92), Math.round(view.height * 0.06), {});
      return wait(250);
    }).then(function () {
      var insp = document.querySelector('.tb-inspect');
      T('an empty-area tap DESELECTS the auto object (the select path runs on a load stroke)',
        !!insp && (insp.style.display === 'none' || !(insp.textContent || '').match(/edge|feather/i)),
        'display=' + (insp ? insp.style.display : 'none'));
      // (2) tap back INSIDE the object: reopening the inspector here is only possible if
      // objBBox gives the load stroke a real box and hitTest returns it (guard-order fix).
      fire('pointerdown', 932, Math.round(view.width * 0.22), Math.round(view.height * 0.5), { isPrimary: true });
      fire('pointerup', 932, Math.round(view.width * 0.22), Math.round(view.height * 0.5), {});
      return wait(300);
    }).then(function () {
      var insp = document.querySelector('.tb-inspect');
      var txt = insp ? (insp.textContent || '') : '';
      var ranges = insp ? insp.querySelectorAll('input[type=range]').length : 0;
      T('re-tapping the auto object REOPENS an edge(grow/shrink) + feather inspector',
        !!insp && insp.style.display !== 'none' && /edge/i.test(txt) && /feather/i.test(txt) && ranges >= 1,
        'shown=' + (insp ? insp.style.display : 'none') + ' ranges=' + ranges + ' txt=' + txt.slice(0, 48));
      var br = toolButton('brush'); if (br) br.click();
    });
  });

  step('the overlay opacity slider reaches the server (drives the baked tint alpha)', function () {
    var w = document.getElementById('tb_wash');
    T('overlay slider exists', !!w);
    if (!w) return true;
    clearForDraw();
    var br = toolButton('brush'); if (br) br.click();
    fire('pointerdown', 940, 40, 40, { isPrimary: true });
    for (var i = 0; i < 6; i++) fire('pointermove', 940, 40 + i * 8, 40 + i * 6, {});
    fire('pointerup', 940, 90, 80, {});
    return wait(650).then(function () {
      w.value = '0.85'; w.dispatchEvent(new Event('input', { bubbles: true }));
      return wait(650);
    }).then(function () {
      var pv = previews();
      var last = pv[pv.length - 1] || {};
      var vals = pv.map(function (f) { return f.overlay_alpha; });
      T('changing the overlay slider sends overlay_alpha to the preview endpoint',
        vals.indexOf(0.85) >= 0 || last.overlay_alpha === 0.85,
        'seen=' + JSON.stringify(vals));
    });
  });

  // Issue-2 regression (the reported "grow/shrink/feather re-render but nothing changes"):
  // with a smart-select (auto) object SELECTED, the edge/feather numbers MUST reach THAT
  // object. They used to feed only the global mask_expand/feather, which the layered server
  // path (normalize_layers) ignores entirely, so dragging them re-fired the preview and
  // returned an identical overlay. The knob-ownership model puts exactly ONE live editor
  // on those numbers while an object is picked: the inspector rows (the panel pair is the
  // greyed mirror of them - editing through a mirror is the duplication this replaces).
  // This asserts the auto layer reaches the wire carrying the moved edge + feather. A
  // `--mutate autoslider` mutant (routing removed) turns this RED, so it genuinely tests the fix.
  step('the toolbar edge/feather sliders edit a SELECTED auto object (reaches the wire)', function () {
    clearForDraw();
    var b = toolButton('smart'); if (b) b.click();
    // The stub returns a LEFT-half mask, so the object lives at x<0.5W; tap there to create it.
    fire('pointerdown', 960, Math.round(view.width * 0.25), Math.round(view.height * 0.5), { isPrimary: true });
    fire('pointerup', 960, Math.round(view.width * 0.25), Math.round(view.height * 0.5), {});
    return wait(1300).then(function () {
      var st = toolButton('select'); if (st) st.click();
      fire('pointerdown', 961, Math.round(view.width * 0.22), Math.round(view.height * 0.5), { isPrimary: true });
      fire('pointerup', 961, Math.round(view.width * 0.22), Math.round(view.height * 0.5), {});
      return wait(300);
    }).then(function () {
      var ed = document.getElementById('tb_edge'), fe = document.getElementById('tb_feather');
      T('edge + feather sliders exist', !!ed && !!fe);
      // Drag to values the commit-time stamp would NOT have used (stub committed edge from the
      // current slider, which is whatever the prior left; force distinct, unmistakable values).
      inspSet(0, 30); inspSet(1, 20);      // the inspector is the one live editor
      // Belt on the lock itself: the panel pair must be INERT while this object is picked.
      T('the panel pair is a mirror, not a second editor (disabled while selected)',
        !!ed && !!fe && ed.disabled === true && fe.disabled === true);
      return wait(800);
    }).then(function () {
      var pv = previews();
      var last = (pv[pv.length - 1] || {}).layers || [];
      var auto = null;
      for (var i = 0; i < last.length; i++) if (last[i].kind === 'auto') auto = last[i];
      T('the selected auto layer carries the TOOLBAR edge as grow and the feather to the wire',
        !!auto && auto.grow === 30 && auto.shrink === 0 && auto.feather === 20,
        JSON.stringify(last));
      var br = toolButton('brush'); if (br) br.click();
    });
  });
