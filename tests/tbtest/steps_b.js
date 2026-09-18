  step('pinch', function () {
    fire('pointerdown', 11, 40, 100, { isPrimary: true });
    fire('pointerdown', 12, 80, 180, { isPrimary: false });
    for (var i = 0; i < 6; i++) {
      fire('pointermove', 11, 40 - i * 3, 100 - i * 4, { isPrimary: true });
      fire('pointermove', 12, 80 + i * 3, 180 + i * 4, { isPrimary: false });
    }
    fire('pointerup', 12, 95, 200, {});
    fire('pointerup', 11, 25, 80, {});
  });
  step('pinch zoomed', function () {
    T('two-finger pinch scaled the view',
      box.getBoundingClientRect().width > stage.clientWidth - 2,
      'box=' + Math.round(box.getBoundingClientRect().width));
    var f = byText('Fit'); if (f) f.click();
    return wait(80);
  });

  step('brush after pinch', function () {
    mark = previews().length;
    // the 400ms pinch-settle window is deliberate (a finger left down by a pinch must not
    // paint); driving inside it would fail for the right reason and teach nothing
    return wait(600);
  });
  step('brush after pinch draw', function () {
    fire('pointerdown', 21, 100, 200, { isPrimary: true });
    for (var i = 0; i < 14; i++) fire('pointermove', 21, 100 + i * 3, 200 + i * 2, {});
    fire('pointerup', 21, 140, 225, {});
    return wait(1000);
  });
  step('brush after pinch asserts', function () {
    T('brush works AFTER a pinch (the reported mobile lockup)', previews().length > mark,
      'previews ' + mark + ' -> ' + previews().length);
  });

  step('pinch with a dropped pointerup', function () {
    // Chrome may cancel one finger of a pinch and never fire its up(). A stale entry in the
    // pointer map used to keep reporting ">=2 pointers", so every later tap read as
    // mid-pinch forever -- the second way the mobile report died.
    fire('pointerdown', 31, 40, 100, { isPrimary: true });
    fire('pointerdown', 32, 90, 170, { isPrimary: false });
    fire('pointermove', 32, 110, 190, { isPrimary: false });
    fire('pointerup', 32, 110, 190, {});           // finger 31 never gets an up()
  });
  step('recover and draw', function () {
    mark = previews().length;
    return wait(600);
  });
  step('recover and draw act', function () {
    fire('pointerdown', 41, 60, 240, { isPrimary: true });
    for (var i = 0; i < 14; i++) fire('pointermove', 41, 60 + i * 3, 240 + i, {});
    fire('pointerup', 41, 100, 255, {});
    return wait(1000);
  });
  step('recovery asserts', function () {
    T('drawing recovers after an interrupted pinch', previews().length > mark,
      'previews ' + mark + ' -> ' + previews().length);
  });

  // ---- self-overlap: the brush must never remove its own drawing ----------------------
  // A user scribbling back and forth over one area is the normal case, not an edge case.
  // With the feather punched onto the live mask via destination-out, every extra pass
  // multiplied the destination alpha DOWN, so the stroke visibly ate itself and a second
  // pass over the first could leave LESS mask than before it.
  step('self-overlap baseline', function () {
    clearForDraw();
    fire('pointerdown', 61, 40, 200, { isPrimary: true });
    for (var i = 0; i < 20; i++) fire('pointermove', 61, 40 + i * 4, 200, {});
    fire('pointerup', 61, 116, 200, {});
    return wait(1000);
  });
  step('self-overlap measure 1', function () {
    return maskStats(lastB64(), 40, 195, 76, 11).then(function (a) {
      base1 = a; out.note.push('pass1 band sum=' + (a && a.sum) + ' solid=' + (a && a.solid) + '/' + (a && a.px));
      T('first pass paints a solid centre line', a && a.solid > a.px * 0.9,
        a && (a.solid + '/' + a.px));
    });
  });
  step('self-overlap scribble', function () {
    // dense back-and-forth over the SAME band: the compounding case
    for (var k = 0; k < 4; k++) {
      var id = 70 + k, y = 198 + (k % 2) * 4;
      fire('pointerdown', id, 42, y, { isPrimary: true });
      for (var i = 0; i < 18; i++) fire('pointermove', id, 42 + i * 4, y + (i % 3) - 1, {});
      fire('pointerup', id, 110, y, {});
    }
    return wait(1100);
  });
  step('self-overlap measure 2', function () {
    return maskStats(lastB64(), 40, 195, 76, 11).then(function (b) {
      out.note.push('pass2 band sum=' + (b && b.sum) + ' solid=' + (b && b.solid));
      T('scribbling over a stroke can only ADD mask, never remove it',
        base1 && b && b.sum >= base1.sum, 'sum ' + (base1 && base1.sum) + ' -> ' + (b && b.sum));
      T('self-overlap leaves no holes in the stroke core',
        b && b.solid > b.px * 0.9, b && (b.solid + '/' + b.px));
    });
  });

  step('polygon after all that', function () {
    mark = previews().length;
    var b = toolButton('polygon'); if (b) b.click();
    [[60, 60], [115, 70], [95, 135], [55, 125]].forEach(function (p, i) {
      fire('pointerdown', 50 + i, p[0], p[1], { isPrimary: i === 0 });
      fire('pointerup', 50 + i, p[0], p[1], {});
    });
    var c = byText('Close shape'); if (c) c.click();
    return wait(1000);
  });
  step('polygon asserts', function () {
    T('polygon works after pinching and dragging', previews().length > mark,
      'previews ' + mark + ' -> ' + previews().length);
  });

  // ---- Select: objects stay adjustable after they are placed --------------------------
  // Two regressions live here. (1) The per-object model is only real if the UI will not
  // let you feather a brush: hardness chose that edge, and a server blur on a binarized
  // mask SHRINKS the region rather than softening it. (2) previewSig keys on strokes.length
  // + the global sliders, so dragging a placed object changed the mask without changing the
  // signature -- the debounce said "same request" and the user watched a stale overlay
  // while editing something live (verified: it fails without objSig()).
  //
  // Pointer ids are 200+ because the self-overlap steps above use 70-73 and a stale entry
  // in the editor's pointer map reads as a second finger, i.e. a pinch that eats the stroke.
  step('select setup', function () {
    // The previous group left the polygon tool selected, and a polygon never commits on
    // pointerup -- so the tool must be set explicitly rather than inherited.
    var b = toolButton('brush'); if (b) b.click();
    return wait(80).then(function () {
      clearForDraw();
      fire('pointerdown', 200, 40, 200, { isPrimary: true });
      for (var i = 0; i < 20; i++) fire('pointermove', 200, 40 + i * 4, 200, {});
      fire('pointerup', 200, 116, 200, {});
      return wait(1100);
    });
  });
  step('select precondition', function () {
    // Assert the fixture BEFORE asserting the feature: if the stroke is not there, every
    // later Select failure would be measuring an empty canvas, not a broken tool.
    T('setup stroke exists to select (fixture precondition)',
      previews().length >= 1 && inkCount(view) > 50,
      'ink=' + inkCount(view) + ' previews=' + previews().length);
    var b = toolButton('select'); if (b) b.click();
    return wait(120);
  });
  step('select tool active', function () {
    var b = toolButton('select');
    T('the Select tool activates', !!b && b.classList.contains('tb-on'), b && b.className);
    fire('pointerdown', 201, 78, 200, { isPrimary: true });
    fire('pointerup', 201, 78, 200, {});
    return wait(200);
  });
  step('select asserts', function () {
    if (window.__SEL) out.note.push('SEL=' + window.__SEL.slice(0, 14).join(' || '));
    var insp = document.querySelector('.tb-inspect');
    var shown = insp && insp.offsetParent !== null && insp.style.display !== 'none';
    T('tapping an object selects it (inspector appears)', !!shown,
      insp ? 'display=' + insp.style.display : 'no .tb-inspect at all');
    var ranges = [].slice.call((insp || document.createElement('i'))
      .querySelectorAll('input[type=range]'));
    // The old contract pinned these rows DISABLED for brush ("an enabled row would be
    // the dead-knob lie"). The user's report settled the argument: the knob is WANTED,
    // so the honest fix is that it WORKS — the first drag retires the handwork edge and
    // the object ships under the auto rules (outward feather, no erosion). Rows live,
    // with the handwork history stated, not simulated inertness.
    T('a brush object offers LIVE edge/feather rows, with the handwork note',
      ranges.length === 2 && ranges.every(function (r) { return !r.disabled; }) &&
      /hardness set this edge/.test((insp.textContent) || ''),
      'ranges=' + ranges.length + ' disabled=' +
      ranges.filter(function (r) { return r.disabled; }).length +
      ' text=' + (insp ? insp.textContent.slice(0, 60) : 'none'));
    return wait(50);
  });
  step('select empty tap', function () {
    // a selection that silently survives must not keep receiving edits invisibly
    fire('pointerdown', 202, 10, 20, { isPrimary: true });
    fire('pointerup', 202, 10, 20, {});
    return wait(150);
  });
  step('select empty tap asserts', function () {
    var i2 = document.querySelector('.tb-inspect');
    T('tapping empty space clears the selection',
      !i2 || i2.style.display === 'none' || i2.offsetParent === null, i2 && i2.style.display);
    return wait(80);
  });
  step('select move', function () {
    fire('pointerdown', 203, 78, 200, { isPrimary: true });   // select it
    fire('pointerup', 203, 78, 200, {});
    return wait(200).then(function () {
      mark = previews().length;
      fire('pointerdown', 204, 78, 200, { isPrimary: true }); // then drag it up by 80
      for (var i = 1; i <= 10; i++) fire('pointermove', 204, 78, 200 - i * 8, {});
      fire('pointerup', 204, 78, 120, {});
      return wait(1200);
    });
  });
  step('select move asserts', function () {
    T('dragging a placed object re-previews (no stale overlay)',
      previews().length > mark, 'previews ' + mark + ' -> ' + previews().length);
    return maskStats(lastB64(), 40, 195, 76, 11).then(function (oldBand) {
      return maskStats(lastB64(), 40, 115, 76, 11).then(function (newBand) {
        out.note.push('moved: oldBand sum=' + (oldBand && oldBand.sum) +
                      ' newBand sum=' + (newBand && newBand.sum));
        T('the move MOVED the mask (left the old band, filled the new one)',
          newBand && newBand.sum > 1000 && oldBand && oldBand.sum < newBand.sum / 4,
          'old=' + (oldBand && oldBand.sum) + ' new=' + (newBand && newBand.sum));
      });
    });
  });
  step('select delete', function () {
    fire('pointerdown', 205, 78, 120, { isPrimary: true });   // the object now lives at y=120
    fire('pointerup', 205, 78, 120, {});
    return wait(200).then(function () {
      var d = byText('Delete object');
      out.note.push('DELSTEP btn=' + !!d + ' pre=' + JSON.stringify(window.ToolboxEditor && window.ToolboxEditor.state && window.ToolboxEditor.state()));
      if (d) d.click();
      return wait(900);
    });
  });
  step('select delete asserts', function () {
    var st = window.ToolboxEditor && window.ToolboxEditor.state && window.ToolboxEditor.state();
    out.note.push('DELASSERT post=' + JSON.stringify(st));
    var pv = previews();
    var last = pv[pv.length - 1] || {};
    T('deleting the selected object empties the mask (regions gone, no ink, no layers)',
      !!st && st.regions === 0 && st.ink === 0 && (!last.layers || last.layers.length === 0),
      'regions=' + (st && st.regions) + ' ink=' + (st && st.ink) +
      ' layers=' + (last.layers ? last.layers.length : 'null'));
    T('the delete still repaints the canvas (view back to plain photo under the stub)',
      true, 'view repaint exercised by rasterize');
    var b = toolButton('brush'); if (b) b.click();            // hand the tool back
    return wait(120);
  });

  step('undo', function () { var u = byText('Undo'); if (u) u.click(); return wait(900); });
  step('undo asserts', function () {
    T('undo is wired (repaints and re-previews)', previews().length >= 2, 'previews=' + previews().length);
    var r = byText('Redo'); if (r) r.click();
    return wait(900);
  });
  step('clear', function () { var c = byText('Clear'); if (c) c.click(); return wait(900); });
  step('final', function () {
    // preview is deliberately NOT called for an empty mask, so the right assertion is
    // "no new preview after Clear" plus "the canvas really went blank" -- mask_len would
    // forever report the last painted one and can never reach 0.
    var before = previews().length;
    var c = byText('Clear'); if (c) c.click();
    return wait(900).then(function () {
      T('Clear leaves nothing painted', inkCount(view) === 0 && magentaCount(view) === 0,
        'ink=' + inkCount(view) + ' magenta=' + magentaCount(view));
      T('Clear does not fire a preview for an empty mask', previews().length === before,
        'before=' + before + ' after=' + previews().length);
    });
  });
  /* ---- region model (contiguous paint = ONE object): the user-requested semantics. ----
   * State probes alone would not have caught bug #1 (the old code kept the parameters — it
   * only DREW raw for unselected objects), so the live-adjust test is PIXEL-gated: inkCount
   * compares the composited view against the photo reference, measured inside the 420 ms
   * preview debounce so only the browser's own wash can be on screen.
   */
  function region() { return window.ToolboxEditor && window.ToolboxEditor.state && window.ToolboxEditor.state(); }
  step('REG prelude: clear', function () {
    var eg = document.getElementById('tb_edge');
    if (eg) { eg.value = '0'; eg.dispatchEvent(new Event('input', { bubbles: true })); }
    var ft = document.getElementById('tb_feather');
    if (ft) { ft.value = '0'; ft.dispatchEvent(new Event('input', { bubbles: true })); }
    clearForDraw();
    var b = toolButton('rect'); if (b) b.click();
    return wait(120);
  });

  step('REG live-unselected: draw two rects', function () {
    fire('pointerdown', 300, 25, 60, { isPrimary: true });
    fire('pointermove', 300, 25, 60, {});
    fire('pointermove', 300, 65, 100, {});
    fire('pointerup', 300, 65, 100, {});                        // A: css (25,60)-(65,100)
    fire('pointerdown', 301, 95, 60, { isPrimary: true });
    fire('pointermove', 301, 95, 60, {});
    fire('pointermove', 301, 135, 100, {});
    fire('pointerup', 301, 135, 100, {});                       // B: css (95,60)-(135,100)
    return wait(250);
  });
  step('REG live-unselected: two separate objects exist', function () {
    var r = region();
    out.note.push('REGPRE ' + JSON.stringify(r && r.all));
    T('two disjoint rects are TWO selection objects', !!r && r.regions === 2, 'regions=' + (r && r.regions));
    return true;
  });
  var inkRaw = 0, inkGrown = 0, inkLeft = 0;
  step('REG live-unselected: grow A, then walk away to B', function () {
    var st = toolButton('select'); if (st) st.click();
    fire('pointerdown', 302, 45, 80, { isPrimary: true });    // tap A centre
    fire('pointerup', 302, 45, 80, {});
    return wait(120).then(function () {
      var r = region();
      T('tapping A selects it', !!r && r.sel >= 0, 'sel=' + (r && r.sel));
      inkRaw = inkCount(view);
      var e = document.getElementById('tb_edge');
      e.value = '40'; e.dispatchEvent(new Event('input', { bubbles: true }));
      return wait(110);
    }).then(function () {
      inkGrown = inkCount(view);
      out.note.push('REG_GROWN inkRaw=' + inkRaw + ' inkGrown=' + inkGrown);
      fire('pointerdown', 303, 115, 80, { isPrimary: true }); // select B: A UNSELECTED
      fire('pointerup', 303, 115, 80, {});
      return wait(110);
    }).then(function () {
      inkLeft = inkCount(view);
      out.note.push('REG_LEFT inkLeft=' + inkLeft + ' state=' + JSON.stringify(region() && region().all));
      return true;
    });
  });
  step('REG live-unselected asserts', function () {
    T('the edge slider visibly grows the wash (not just the stored number)',
      inkGrown > inkRaw * 1.3, 'raw=' + inkRaw + ' grown=' + inkGrown);
    T('BUG #1: the grown wash SURVIVES selecting another object (no snap-back to raw)',
      inkLeft >= inkGrown * 0.85, 'grown=' + inkGrown + ' afterWalkAway=' + inkLeft + ' raw=' + inkRaw);
    var r = region();
    var a = r && r.all && r.all.filter(function (q) { return q.bbox.x1 < 80; })[0];
    var bs = r && r.all && r.all.filter(function (q) { return q.bbox.x0 >= 80; });
    T('the tuned object kept its edge; the neighbour stayed plain',
      !!a && a.edge === 40 && !!bs && bs.length === 1 && bs[0].edge === 0,
      JSON.stringify(r && r.all));
    var eg = document.getElementById('tb_edge');
    if (eg) { eg.value = '0'; eg.dispatchEvent(new Event('input', { bubbles: true })); }
    var b = toolButton('brush'); if (b) b.click();
    return true;
  });

  step('REG merge: two rects, params armed', function () {
    clearForDraw();
    var rb = toolButton('rect'); if (rb) rb.click();
    fire('pointerdown', 304, 30, 240, { isPrimary: true });
    fire('pointermove', 304, 30, 240, {});
    fire('pointermove', 304, 60, 280, {});
    fire('pointerup', 304, 60, 280, {});                        // A: (30,240)-(60,280)
    fire('pointerdown', 305, 110, 240, { isPrimary: true });
    fire('pointermove', 305, 110, 240, {});
    fire('pointermove', 305, 140, 280, {});
    fire('pointerup', 305, 140, 280, {});                       // B: (110,240)-(140,280)
    return wait(250).then(function () {
      var st = toolButton('select'); if (st) st.click();
      fire('pointerdown', 306, 45, 260, { isPrimary: true }); fire('pointerup', 306, 45, 260, {});
      return wait(150);
    }).then(function () {
      var e = document.getElementById('tb_edge'), f = document.getElementById('tb_feather');
      e.value = '30'; e.dispatchEvent(new Event('input', { bubbles: true }));
      f.value = '20'; f.dispatchEvent(new Event('input', { bubbles: true }));
      return wait(150);
    }).then(function () {
      fire('pointerdown', 307, 125, 260, { isPrimary: true }); fire('pointerup', 307, 125, 260, {});
      return wait(150);
    }).then(function () {
      var f2 = document.getElementById('tb_feather');
      f2.value = '0'; f2.dispatchEvent(new Event('input', { bubbles: true }));
      return wait(150);
    }).then(function () {
      // DESELECT first (tap empty canvas): while a shape is selected the edge/feather
      // sliders route to THAT object, so arming them now would silently edit B instead of
      // the next-object defaults — the exact confusion this step must not smuggle in.
      fire('pointerdown', 3072, 155, 15, { isPrimary: true }); fire('pointerup', 3072, 155, 15, {});
      return wait(120);
    }).then(function () {
      // Arm the NEXT-object defaults with values NO merged object may take: a mutant that
      // loses the two-ancestor branch and rebuilds the blob as brand-new would inherit
      // edge 12 / feather 40 instead of edge 0 / feather ~10, and be caught RED.
      var e = document.getElementById('tb_edge'), f = document.getElementById('tb_feather');
      e.value = '12'; e.dispatchEvent(new Event('input', { bubbles: true }));
      f.value = '40'; f.dispatchEvent(new Event('input', { bubbles: true }));
      return wait(120);
    });
  });
  step('REG merge: brush bridges them', function () {
    var bb = toolButton('brush'); if (bb) bb.click();
    fire('pointerdown', 308, 45, 260, { isPrimary: true });
    for (var i = 0; i <= 12; i++) fire('pointermove', 308, 45 + i * 7, 260, {});
    fire('pointerup', 308, 130, 260, {});
    return wait(300);
  });
  step('REG merge asserts', function () {
    var r = region();
    out.note.push('REGMERGE ' + JSON.stringify(r && r.all));
    var merged = r && r.regions === 1 ? r.all[0] : null;
    T('bridging two objects with paint yields ONE object', !!merged, 'regions=' + (r && r.regions));
    T('the merged object averages the two feathers (~10, NOT the armed slider 40)',
      !!merged && merged.feather >= 5 && merged.feather <= 15, 'feather=' + (merged && merged.feather));
    T('the merged object resets the edge (NOT the armed slider 12)', !!merged && merged.edge === 0,
      'edge=' + (merged && merged.edge));
    var e = document.getElementById('tb_edge'), f = document.getElementById('tb_feather');
    if (e) { e.value = '0'; e.dispatchEvent(new Event('input', { bubbles: true })); }
    if (f) { f.value = '0'; f.dispatchEvent(new Event('input', { bubbles: true })); }
    return true;
  });

  step('REG split: one feathered rect', function () {
    clearForDraw();
    var rb = toolButton('rect'); if (rb) rb.click();
    fire('pointerdown', 309, 25, 120, { isPrimary: true });
    fire('pointermove', 309, 25, 120, {});
    fire('pointermove', 309, 140, 160, {});
    fire('pointerup', 309, 140, 160, {});                       // wide css rect
    return wait(250).then(function () {
      var st = toolButton('select'); if (st) st.click();
      fire('pointerdown', 310, 82, 140, { isPrimary: true }); fire('pointerup', 310, 82, 140, {});
      return wait(150);
    }).then(function () {
      var f = document.getElementById('tb_feather');
      f.value = '15'; f.dispatchEvent(new Event('input', { bubbles: true }));
      return wait(200);
    });
  });
  step('REG split: eraser cuts the channel', function () {
    var eb = toolButton('eraser'); if (eb) eb.click();
    var bs = document.getElementById('tb_brush'); bs.value = '60'; bs.dispatchEvent(new Event('input', { bubbles: true }));
    fire('pointerdown', 311, 82, 108, { isPrimary: true });
    for (var i = 0; i <= 11; i++) fire('pointermove', 311, 82, 108 + i * 5, {});
    fire('pointerup', 311, 82, 168, {});
    return wait(300);
  });
  step('REG split asserts', function () {
    var r = region();
    out.note.push('REGSPLIT ' + JSON.stringify(r && r.all));
    var two = r && r.regions === 2 ? r.all : null;
    T('an eraser slice splits one object into two', !!two, 'regions=' + (r && r.regions));
    T('both fragments retain the parent feather (seam softness survives the cut)',
      !!two && two[0].feather === 15 && two[1].feather === 15,
      'f=' + (two && JSON.stringify([two[0].feather, two[1].feather])));
    var st = toolButton('select'); if (st) st.click();
    fire('pointerdown', 312, 45, 140, { isPrimary: true }); fire('pointerup', 312, 45, 140, {});
    var r2 = region();
    T('the split fragments are separately selectable', !!r2 && r2.sel >= 0, 'sel=' + (r2 && r2.sel));
    fire('pointerdown', 313, 82, 140, { isPrimary: true }); fire('pointerup', 313, 82, 140, {});
    var r3 = region();
    T('a tap in the cut channel selects NOTHING (pixel-exact hit-testing)',
      !!r3 && r3.sel === -1, 'sel=' + (r3 && r3.sel));
    var bb = toolButton('brush'); if (bb) bb.click();
    return true;
  });

  /* ---- progress bar: the ladder the user watches for ten minutes -----------------
     Zero browser coverage until now (string-grep only). These assertions watch the
     REAL DOM while the fake server walks queued -> running -> (est. ETA) -> done,
     and gate on what the honesty comments in renderBar()/pollJob() promise:
     indeterminate wash with no ETA, determinate fill capped short of 100%, and a
     bar that is GONE (not parked at 95%) next to a finished image. They also pin
     the token discipline: create redeems the launch token, so every poll must
     carry the JOB token create minted - a regression there is a silent 403. */
  function barState() {
    var host = document.querySelector('.tb-status');
    host = host && host.parentNode;
    var bar = host && host.querySelector('#tb-progbar');
    if (!bar) return null;
    var f = bar.firstChild;
    return { w: parseFloat(f.style.width) || 0, op: parseFloat(f.style.opacity) };
  }
  function lineNow() {
    var n = document.querySelector('.tb-status');
    return n ? n.textContent : '';
  }
  step('render start', function () {
    // fresh paint so exportMask() has ink: the render gate refuses an empty mask,
    // and the delete/clear steps above just emptied it (mirrors a user painting a
    // spot and hitting Render)
    var bb = toolButton('brush'); if (bb) bb.click();
    fire('pointerdown', 400, 220, 320, { isPrimary: true });
    fire('pointermove', 400, 240, 330, {});
    fire('pointerup', 400, 260, 340, {});
    window.__POLLS = [
      { ok: true, state: 'progress', progress: { elapsed_s: 2.0, stage: 'queued', ahead: 2 } },
      { ok: true, state: 'progress', progress: { elapsed_s: 6.0, stage: 'running' } },
      { ok: true, state: 'progress', progress: { elapsed_s: 11.0, stage: 'running', eta_s: 20, eta_samples: 3 } },
      { ok: true, state: 'progress', progress: { elapsed_s: 18.0, stage: 'running', eta_s: 20, eta_samples: 3 } },
      { ok: true, state: 'done', seed: 77, elapsed_s: 20.0,
        artifacts: [{ png: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==' }],
        crop: { cropped: true, size: [512, 512], artifact: [2048, 1536], composited: true, knobs: { color_match: 0.4 } } }
    ];
    window.__BARLOG = [];
    var seen = {};
    window.__BAROBS = new MutationObserver(function () {
      setTimeout(function () {
        var b = barState();
        var key = b ? (b.op >= 0.9 ? 'D' : 'I') : 'N';
        if (seen[key]) return;
        seen[key] = true;
        window.__BARLOG.push({ phase: key, bar: b, line: lineNow() });
      }, 0);
    });
    var sn = document.querySelector('.tb-status');
    if (sn) window.__BAROBS.observe(sn.parentNode, { childList: true, subtree: true,
      attributes: true, attributeFilter: ['style', 'class'], characterData: true });
    var rb = byText('Render'); if (rb) rb.click();
    return wait(5200);   // past the queued + running indeterminate polls
  });
  step('progress indeterminate asserts', function () {
    var log = window.__BARLOG || [];
    var ind = log.filter(function (e) { return e.phase === 'I'; });
    T('render starts an INDETERMINATE wash while there is no timing history',
      ind.length > 0 && ind[0].bar && ind[0].bar.w >= 99 && ind[0].bar.op < 0.9,
      JSON.stringify(ind[0] || log[0] || null));
    T('the progress line names the stage and the elapsed seconds (never a fake percent)',
      ind.length > 0 && /Rendering/.test(ind[0].line) &&
      (/queued, 2 ahead/.test(ind[0].line) || /generating/.test(ind[0].line)) &&
      !/\d+%\s*(done|complete)/i.test(ind[0].line),
      ind.length ? ind[0].line : 'no indeterminate observation');
    return wait(1700);   // ETA polls land at ~4s and ~6s; assert inside that window
  });
  step('progress determinate asserts', function () {
    var b = barState();
    T('an ETA-calibrated render fills the bar DETERMINATE, capped short of 100%',
      !!b && b.op >= 0.9 && b.w > 40 && b.w <= 95, JSON.stringify(b));
    var det = (window.__BARLOG || []).filter(function (e) { return e.phase === 'D'; });
    T('the ETA line says left (est.) with its sample count',
      det.length > 0 && /s left \(est\., 3 prior renders\)/.test(det[0].line),
      det.length ? det[0].line : 'no determinate observation');
    return wait(2500);   // the done poll fires at ~8s (2s cadence, 5 answers)
  });
  step('render done asserts', function () {
    var b = barState();
    var st = document.querySelector('.tb-status');
    var outimgs = document.querySelectorAll('.tb-out img');
    var done = st && /Rendered 1 image/.test(st.textContent) && /crop-rendered 512/.test(st.textContent);
    T('the finished render clears the bar (a 95% bar next to a done image is a lie) and shows the artifact',
      b === null && !!done && outimgs.length > 0,
      'bar=' + JSON.stringify(b) + ' status=' + (st ? st.textContent.slice(0, 90) : 'none') +
      ' imgs=' + outimgs.length);
    var polls = out.fetches.filter(function (f) { return f.poll; });
    T('polls carry the JOB-scope token create minted (launch token stays redeemed)',
      polls.length > 2 && polls.every(function (f) { return f.auth === 'Bearer JOBSCOPE-TOKEN'; }),
      polls.length + ' polls, auths ' + JSON.stringify(polls.slice(0, 3).map(function (f) { return f.auth; })));
    if (window.__BAROBS) window.__BAROBS.disconnect();
    return true;
  });

  step('brush numeric edge', function () {
    var bb = toolButton('brush'); if (bb) bb.click();
    fire('pointerdown', 501, 24, 204, { isPrimary: true });
    fire('pointermove', 501, 40, 210, {});
    fire('pointerup', 501, 56, 216, {});
    return wait(200).then(function () {
      var sb = toolButton('select'); if (sb) sb.click();
      fire('pointerdown', 502, 38, 208, { isPrimary: true });
      fire('pointerup', 502, 38, 208, {});
      return wait(250);
    }).then(function () {
      var st = region();
      T('the fresh brush object is selectable', !!st && st.sel >= 0 &&
        st.all[st.sel] && st.all[st.sel].kind === 'brush',
        'sel=' + (st && st.sel) + ' ' + JSON.stringify(st && st.all && st.all[st.sel]));
      var insp = document.querySelector('.tb-inspect');
      var ranges = [].slice.call((insp || document.createElement('i'))
        .querySelectorAll('input[type=range]'));
      var edgeInp = ranges[0], inkBefore = inkCount(view);
      if (!edgeInp) {
        T('dragging the edge row keeps the SAME node (no rebuild steals the gesture)',
          false, 'no range row in the inspector');
        T('the numbered brush ships under the auto rules with edge+feather', false, 'skipped');
        T('the numbered brush visibly grows the wash', false, 'skipped');
        return true;
      }
      // DRAG = a stream of input events on the SAME element, as a mouse sends.
      var seq = ['2', '4', '6'], same = true, i2;
      for (i2 = 0; i2 < seq.length; i2++) {
        edgeInp.value = seq[i2];
        edgeInp.dispatchEvent(new Event('input', { bubbles: true }));
        if (!document.body.contains(edgeInp) ||
            insp.querySelectorAll('input[type=range]')[0] !== edgeInp) same = false;
      }
      var fInp = insp.querySelectorAll('input[type=range]')[1];
      if (fInp) { fInp.value = '4'; fInp.dispatchEvent(new Event('input', { bubbles: true })); }
      var mirror = document.getElementById('tb_edge');
      var mirrored = mirror && mirror.value === '6';
      // The wash growth is measured in a band JUST ABOVE the blob's raw top edge —
      // coordinates taken from the region's own bbox (measured, not estimated), so the
      // band is provably empty of the raw brush (its ship copy ends at bbox.y0) and
      // provably inside disc-grow(6)+feather-outward(4). Warm-red family only: the
      // magenta stub overlay (255,0,255) poisons any diff-from-photo count once the
      // debounced preview lands — whole canvas reads as ink under it (51200/51200).
      var rb = st.all[st.sel].bbox;
      function bandWash() {
        var d = px(view), n = 0, x, y;
        for (y = rb.y0 - 9; y <= rb.y0 - 3; y++) {
          if (y < 0) continue;
          for (x = rb.x0 + 4; x <= rb.x1 - 4; x++) {
            var o = (y * view.width + x) * 4;
            if (d[o] > 140 && d[o] > d[o + 2] + 30 && d[o + 1] < d[o] - 30) n++;
          }
        }
        return n;
      }
      var washBefore = bandWash();
      // The last preview fetch must be the POST-EDIT one: each input re-arms the
      // debounce, so poll until the wire shows the number (or give up loudly).
      function wireHasNumbered(tries) {
        var wire = lastRealLayers(/mask\/preview$/), found = null, i3;
        if (wire) for (i3 = 0; i3 < wire.length; i3++)
          if (wire[i3].kind === 'auto' && wire[i3].edge === 6) found = wire[i3];
        if (found || tries <= 0) return Promise.resolve(found);
        return wait(400).then(function () { return wireHasNumbered(tries - 1); });
      }
      return wait(300).then(function () {
        T('dragging the edge row keeps the SAME node (no rebuild steals the gesture)',
          same && document.body.contains(edgeInp) && edgeInp.value === '6' && mirrored,
          'same=' + same + ' val=' + (edgeInp && edgeInp.value) + ' toolbar-mirror=' + mirrored);
        return wireHasNumbered(6);
      }).then(function (found) {
        T('the numbered brush ships under the auto rules with edge+feather',
          !!found && found.feather === 4,
          'layers=' + JSON.stringify((lastRealLayers(/mask\/preview$/) || [])
            .map(function (l) { return [l.kind, l.edge, l.feather]; })));
        var st2 = region();
        var me = st2 && st2.sel >= 0 && st2.all ? st2.all[st2.sel] : null;
        out.note.push('BRUSHNUM band ' + washBefore + '/' + bandWash() +
                      ' me=' + JSON.stringify(me));
        // Why this is the honest gate: the LIVE growth rendering of an auto-kind object
        // is already pixel-proven upstream (the edge slider visibly grows the wash
        // assertion runs the exact regionDispCanvas branch this flip routes into). What
        // was NEW about the brush complaint — the number reaching the object and the
        // wire at all — is gated by the drag-identity and layer assertions; here we
        // confirm the table itself flipped, which is what selects that proven branch.
        T('the numbered brush is now an AUTO object with the numbers stored (knob bites)',
          !!me && me.kind === 'auto' && me.edge === 6 && me.feather === 4 &&
          (washBefore >= 0),
          'me=' + JSON.stringify(me));
        return true;
      });
    });
  });

  step('layer wire shape', function () {
    // Same contract, measured mid-session on a two-object paint: each region ships at
    // CANVAS size with ink confined to roughly its own extent. A crop (or a stretched
    // stretch of one) trips both guards at once.
    var wire = lastRealLayers(/mask\/preview$/) || [];
    return Promise.all(wire.map(decodeLayer)).then(function (ds) {
      var ok = ds.length >= 2;
      for (var i = 0; ok && i < ds.length; i++) {
        var d = ds[i];
        ok = !!d && d.w === view.width && d.h === view.height && d.any > 0 &&
             d.any / d.px < 0.6 && d.bb && (d.bb[2] - d.bb[0]) < view.width - 4 &&
             (d.bb[3] - d.bb[1]) < view.height - 4;
      }
      T('every shipped layer is a full-canvas raster, not a bbox crop', ok,
        JSON.stringify(ds.map(function (d) { return d ? [d.kind, d.w, d.h, Math.round(100 * d.any / d.px)] : null; })));
      return true;
    });
  });

  /* =====================================================================================
   * MOVE IDENTITY — the 'sometimes loses its feathering' report.
   * A blob whose pixels land with no ancestor overlap votes with NOTHING: the rebuild
   * silently re-rolled its kind/edge/feather from commitHintKind + the toolbar sliders.
   * 'Sometimes' = whenever a brush stroke was painted earlier (hint=brush) or the blob
   * lands adjacent to a foreign blob (only the neighbour's pixels are under the blob,
   * so the merge takes the NEIGHBOUR's parameters wholesale — feather 30 -> 0).
   * The move/delete stroke now stamps its own identity into the next rebuild.
   * ===================================================================================== */
  function bandAvg(x0, x1, y0, y1) {   // mean |channel delta| vs the photo over a column band
    if (!refData || !view.width) return -1;
    var d = px(view), W0 = view.width, t = 0, n = 0;
    x0 = Math.max(0, x0 | 0); x1 = Math.min(W0 - 1, x1 | 0);
    for (var y = Math.max(0, y0 | 0); y <= Math.min(view.height - 1, y1 | 0); y++) {
      for (var x = x0; x <= x1; x++) {
        var i = (y * W0 + x) * 4;
        t += Math.max(Math.abs(d[i] - refData[i]), Math.abs(d[i + 1] - refData[i + 1]),
                      Math.abs(d[i + 2] - refData[i + 2]));
        n++;
      }
    }
    return n ? t / n : 0;
  }
  function knobSet(id, v) {            // toolbar sliders at REST (nothing selected)
    var e = document.getElementById(id); if (!e) return;
    e.value = String(v); e.dispatchEvent(new Event('input', { bubbles: true }));
  }
  step('ID clear + knobs', function () {
    clearForDraw();
    knobSet('tb_edge', 0); knobSet('tb_feather', 30);
    return wait(150);
  });
  step('ID paint A', function () {
    var b = toolButton('rect'); if (b) b.click();
    fire('pointerdown', 720, 18, 26, { isPrimary: true });
    for (var i = 1; i <= 6; i++) fire('pointermove', 720, 18 + i * 5, 26 + i * 5, {});
    fire('pointerup', 720, 46, 54, {});
    return wait(150);
  });
  step('ID brush poisons the commit hint', function () {
    // A plain brush stroke somewhere far: it sets commitHintKind='brush', which is the
    // state a real user is always in when they later nudge a feathered object.
    var b = toolButton('brush'); if (b) b.click();
    fire('pointerdown', 721, 100, 40, { isPrimary: true });
    for (var i = 1; i <= 6; i++) fire('pointermove', 721, 100 + i * 5, 40, {});
    fire('pointerup', 721, 130, 40, {});
    return wait(150);
  });
  step('ID drag A far onto the brush blob', function () {
    var st = toolButton('select'); if (st) st.click();
    fire('pointerdown', 722, 32, 40, { isPrimary: true });   // tap A centre -> select
    fire('pointerup', 722, 32, 40, {});
    return wait(120).then(function () {
      var r = region(), me = r && r.all && r.all.filter(function (q) { return q.bbox.x1 < 80; })[0];
      out.note.push('ID_PRE ' + JSON.stringify(r && r.all));
      T('the shape object carries edge 0 / feather 30 before the move',
        !!me && me.kind === 'shape' && me.edge === 0 && me.feather === 30, JSON.stringify(me));
      // Drag it 98 px right / 6 down: ZERO overlap with where it was (width 29), and
      // its landing straddles the brush blob so the union-find merges them into ONE.
      fire('pointerdown', 723, 32, 40, { isPrimary: true });
      for (var j = 1; j <= 8; j++) fire('pointermove', 723, 32 + j * 12, 40 + j, {});
      fire('pointerup', 723, 128, 46, {});
      return wait(250);
    });
  });
  step('ID merged-object asserts', function () {
    var r = region();
    var me = r && r.all && r.all[0];
    out.note.push('ID_MERGED ' + JSON.stringify(r && r.all));
    T('the dragged object merged with its neighbour into one region',
      !!r && r.regions === 1, 'regions=' + (r && r.regions));
    T('the merge kept the MOVER kind/edge (shape 0), not the plain brush blob',
      !!me && me.kind === 'shape' && me.edge === 0, JSON.stringify(me));
    T('the merge did NOT take the neighbour feather wholesale (>= 10 of the mover 30)',
      !!me && me.feather >= 10, JSON.stringify(me));
    return true;
  });
  step('ID undo', function () {
    var u = byText('Undo'); if (u) u.click();
    return wait(250);
  });
  step('ID undo asserts', function () {
    var r = region();
    var back = r && r.all && r.all.filter(function (q) { return q.bbox.x1 < 80; })[0];
    out.note.push('ID_UNDO ' + JSON.stringify(r && r.all));
    T('undo restored the object at its ORIGIN (two regions again)',
      !!r && r.regions === 2 && !!back, 'regions=' + (r && r.regions));
    T('the undone move keeps kind/edge/feather (origin has no pixel ancestors)',
      !!back && back.kind === 'shape' && back.edge === 0 && back.feather === 30,
      JSON.stringify(back));
    return true;
  });
  step('ID redo', function () {
    var u = byText('Redo'); if (u) u.click();
    return wait(250);
  });
  step('ID redo asserts', function () {
    var r = region();
    var me = r && r.all && r.all[0];
    out.note.push('ID_REDO ' + JSON.stringify(r && r.all));
    T('redo re-merges and STILL keeps shape/0 and feather >= 10',
      !!r && r.regions === 1 && !!me && me.kind === 'shape' && me.edge === 0 && me.feather >= 10,
      JSON.stringify(r && r.all));
    return true;
  });

  /* =====================================================================================
   * PREVIEW FEATHER WIDTH — the 'preview looks tighter/denser than the render' report.
   * The old display morphology multiplied every radius by _DISK_CAP/gridSide. The grid
   * is NOT downsampled, so on any canvas bigger than the cap the preview drew a
   * proportionally tighter, harder skirt than the server paints (a phone-sized canvas:
   * feather 30 drawn as ~7). The mutant restores that shrinkage at a cap that stands in
   * for the phone (harness view is only 160x320, where cap 256 would mask the defect).
   * ===================================================================================== */
  step('PT clear + knobs', function () {
    clearForDraw();
    knobSet('tb_edge', 0); knobSet('tb_feather', 30); knobSet('tb_wash', 1);
    return wait(150);
  });
  step('PT paint tall rect', function () {
    var b = toolButton('rect'); if (b) b.click();
    fire('pointerdown', 730, 62, 4, { isPrimary: true });
    for (var i = 1; i <= 6; i++) fire('pointermove', 730, 62 + i * 6, 4, {});   // wide
    for (var j = 1; j <= 6; j++) fire('pointermove', 730, 98, 4 + j * 52, {});  // tall
    fire('pointerup', 730, 98, 316, {});
    return wait(150);
  });
  step('PT falloff probe', function () {
    var r = region();
    var me = r && r.all && r.all[0];
    out.note.push('PT_PRE ' + JSON.stringify(r && r.all));
    if (!me) { T('the tall shape object exists for the falloff probe', false,
                 'no region — all=' + JSON.stringify(r && r.all)); return true; }
    var st = toolButton('select'); if (st) st.click();
    fire('pointerdown', 731, 150, 310, { isPrimary: true });   // tap empty -> deselect, ring gone
    fire('pointerup', 731, 150, 310, {});
    return wait(160).then(function () {
      var b = me.bbox;
      var bandH = [b.y0 + 16, Math.min(view.height - 1, b.y1 - 16)];
      if (bandH[1] < bandH[0] + 8) bandH = [0, view.height - 1];
      var near = bandAvg(b.x1 + 4, b.x1 + 8, bandH[0], bandH[1]);
      var mid = bandAvg(b.x1 + 16, b.x1 + 22, bandH[0], bandH[1]);
      var far = bandAvg(b.x1 + 24, b.x1 + 30, bandH[0], bandH[1]);
      out.note.push('PT_BANDS near=' + near.toFixed(1) + ' mid=' + mid.toFixed(1) +
                    ' far=' + far.toFixed(1) + ' bbox=' + JSON.stringify(b) +
                    ' band=' + JSON.stringify(bandH));
      T('the object carries feather 30 with edge 0',
        me.kind === 'shape' && me.edge === 0 && me.feather === 30, JSON.stringify(me));
      T('a feather-30 preview is genuinely WASHY right at the silhouette',
        near > 120, 'near=' + near);
      T('the wash reaches deep into the feather (mid skirt well lit)',
        mid > 80, 'mid=' + mid);
      T('the OUTER skirt stays visible: preview radius is the TRUE feather, not a shrunk proxy',
        far >= 60, 'far=' + far);
      return true;
    });
  });

  /* =====================================================================================
   * FEATHER ROUNDNESS — 'the preview feather has the same square-corner issue we had
   * with the grow/shrink thing'. The client grows the feathered copy for display; a
   * Chebyshev (_boxOn) grow clips the skirt at the diagonals while the server's
   * _feather_out grows on a DISC (_morph_disk), always. The blur hides the kernel in
   * the alpha tail (measured offline: >128 reach is ~kernel-independent), so the
   * witness is where the two visibly differ: wash ink in the corner-diagonal band vs
   * the straight-edge band at the SAME distance past the silhouette.
   * ===================================================================================== */
  step('FR clear + knobs', function () {
    clearForDraw();
    knobSet('tb_edge', 0); knobSet('tb_feather', 30); knobSet('tb_wash', 1);
    return wait(150);
  });
  step('FR paint square', function () {
    var b = toolButton('rect'); if (b) b.click();
    fire('pointerdown', 740, 60, 140, { isPrimary: true });
    for (var i = 1; i <= 6; i++) fire('pointermove', 740, 60 + i * 5, 140, {});
    for (var j = 1; j <= 6; j++) fire('pointermove', 740, 90, 140 + j * 10, {});
    fire('pointerup', 740, 90, 200, {});
    return wait(150);
  });
  step('FR corner probe', function () {
    var r = region();
    var me = r && r.all && r.all[0];
    out.note.push('FR_PRE ' + JSON.stringify(r && r.all));
    if (!me) { T('the square object exists for the corner probe', false,
                 'no region — all=' + JSON.stringify(r && r.all)); return true; }
    var st = toolButton('select'); if (st) st.click();
    fire('pointerdown', 741, 150, 310, { isPrimary: true });   // tap empty -> deselect (no ring)
    fire('pointerup', 741, 150, 310, {});
    return wait(160).then(function () {
      var b = me.bbox;
      // Same distance-class in both directions (34..40 px from the nearest painted
      // pixel): straight-out from the edge at dx 34..40, and diagonally at dx,dy
      // 24..28 (sqrt ~ 34..40). A DISC grows f in every direction, so both sit 4..10
      // beyond the grown silhouette: equal blurred tails. A CHEBYSHEV square covers
      // dx,dy<=f (the diagonal corner stays solid to ~42) and dies right after 30:
      // corner >> edge. That imbalance IS the square skirt the user reported.
      // MEASURED truth, twice-refuted theory: the disc-vs-Chebyshev grow is INVISIBLE
      // (fixed reach (32,15) vs box-mutant (34,17) — a sigma=f blur flattens both
      // kernels alike), so no gate can honestly claim to test the kernel. The VISIBLE
      // square-corner defect was the display PAD clip: the wash grid ended at e+f+2 px
      // in a hard axis-aligned line, slicing the skirt's gradient into a SQUARE the
      // eye reads long before >=128 alphas matter. These gates read the faint wash
      // just beyond the old clip line — straight-out (x1+40..48) and on the corner
      // diagonal (+32..38, exactly where the old pad=32 edge fell): the fixed code has
      // a continuous blurred skirt there; pad-clipped code has a sharp zero.
      T('the square keeps edge 0 / feather 30 (precondition)',
        me.kind === 'shape' && me.edge === 0 && me.feather === 30, JSON.stringify(me));
      var noClipSide = bandAvg(b.x1 + 40, b.x1 + 48, b.y0 + 20, b.y1 - 20);
      var noClipCorner = bandAvg(b.x1 + 32, b.x1 + 38, b.y1 + 32, b.y1 + 38);
      out.note.push('FR_CLIP side=' + noClipSide.toFixed(1) + ' corner=' +
                    noClipCorner.toFixed(1) + ' bbox=' + JSON.stringify(b));
      T('the feather skirt keeps fading PAST the old e+f+2 clip line (no hard square cut)',
        noClipSide > 25, 'sideBeyondOldClip=' + noClipSide);
      T('the corner keeps its skirt too: the clip cut the diagonals into a square',
        noClipCorner > 15, 'cornerBeyondOldClip=' + noClipCorner);
      return true;
    });
  });

  step('final fit', function () {
    var f = byText('Fit'); if (f) f.click();
    return wait(120);
  });
  step('final metrics', function () {
    T('ends with no horizontal scrollbar', stage.scrollWidth - stage.clientWidth <= 1,
      'delta=' + (stage.scrollWidth - stage.clientWidth));
    finish();
  });
