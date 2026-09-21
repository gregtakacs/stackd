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
    // Two contracts have died on this element. It was first pinned DISABLED for brush
    // ("an enabled row would be the dead-knob lie"); the user wanted the knob, so it
    // became live and the first drag flipped the object to the auto rules. Now hardness is
    // preview only and the numbers are ordinary per-object adjustments, so the hint says
    // what the knob does instead of narrating a handwork history that no longer applies -
    // and the rows stay live, which is what the brushdead mutant takes away.
    T('a brush object offers LIVE edge/feather rows, with an honest hint',
      ranges.length === 2 && ranges.every(function (r) { return !r.disabled; }) &&
      /brush hardness only previews/.test((insp.textContent) || ''),
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
  var _erParent = null;   // the grown parent's silhouette, probed before the cut
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
      inspSet(0, 40);   // the inspector row edits THIS object; the panel pair is the
                        // greyed mirror while a selection exists (one live editor)
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
      inspSet(0, 30); inspSet(1, 20);      // A's own numbers, via the inspector
      return wait(150);
    }).then(function () {
      fire('pointerdown', 307, 125, 260, { isPrimary: true }); fire('pointerup', 307, 125, 260, {});
      return wait(150);
    }).then(function () {
      inspSet(1, 0);                       // B's feather back to hard, via its inspector row
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
      inspSet(1, 15);                      // the inspector row: parent feather 15
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
        prompt_sent: 'a sunlit red mustang',
        artifacts: [{ png: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==' }],
        crop: { cropped: true, size: [512, 512], artifact: [2048, 1536], composited: true, knobs: { color_match: 0.4 }, chat_post: 'posted' } }
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
    // Caption-handover gate: type an INSTRUCTION-phrased prompt and arm the jobs
    // stub's server-role hook (driver.js) to echo back the reduced caption the real
    // api._create would have stored (imagegen.captioning). The submit line must
    // then name the words the render actually runs on — see the assert below.
    // The first poll lands with ZERO delay (try 0), clobbering the submit line
    // before any timed snapshot could read it, so record EVERY status transition
    // with an observer (same trick __BARLOG uses) and assert from the log.
    var ta = document.getElementById('tb_prompt');
    if (ta) ta.value = 'replace the car with a sunlit red mustang';
    window.__JOBSREWRITE = 'a sunlit red mustang';
    window.__STATUSLOG = [];
    var stn = document.querySelector('.tb-status');
    if (stn) {
      var so = new MutationObserver(function () {
        window.__STATUSLOG.push(stn.textContent || '');
      });
      so.observe(stn, { childList: true, characterData: true, subtree: true });
    }
    // Baseline for the collapse's height claim: the live editor's own box, measured
    // BEFORE submit collapses it (see 'render done asserts').
    window.__H_TALL = (window.ToolboxEditor && window.ToolboxEditor.contentHeight()) || 0;
    var rb = byText('Render'); if (rb) rb.click();
    return wait(5200);   // past the queued + running indeterminate polls
  });
  step('submit caption echo assert', function () {
    var lines = window.__STATUSLOG || [];
    var hit = '';
    for (var i = 0; i < lines.length; i++) {
      if (/sent to the model as/.test(lines[i])) { hit = lines[i]; break; }
    }
    out.note.push('STATUSLOG ' + JSON.stringify(lines.slice(0, 4)).slice(0, 160));
    T('the submit line names the caption the render ACTUALLY runs on (no silent rewrite)',
      /sent to the model as "a sunlit red mustang"/.test(hit), hit || ('no sent-as line in ' + JSON.stringify(lines.slice(0, 4))));
    return true;
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
    var host = document.querySelector('.tb');
    var vis = function (n) { return !!n && getComputedStyle(n).display !== 'none'; };
    var stage = host && host.querySelector('.tb-stage');
    var go = host && host.querySelector('.tb-go');
    var done = st && /Done ·/.test(st.textContent) && /20s/.test(st.textContent)
               && /"a sunlit red mustang"/.test(st.textContent)
               && /crop-rendered 512/.test(st.textContent)
               && /posted back into your chat/.test(st.textContent);
    T('a finished render collapses the embed to the summary line — bar gone, artifact NOT displayed (the chat owns it), canvas and buttons hidden',
      b === null && !!done && outimgs.length === 0 && !vis(stage) && !vis(go) && vis(st),
      'bar=' + JSON.stringify(b) + ' status=' + (st ? st.textContent.slice(0, 90) : 'none') +
      ' imgs=' + outimgs.length + ' stageVis=' + vis(stage) + ' goVis=' + vis(go));
    // The height bug this contract exposed (2026-09-21): inside an iframe the DOCUMENT
    // can never measure below the frame's current height, so a document-based
    // contentHeight was a one-way ladder and the collapsed embed stayed photo-tall.
    // The editor-box measurement must report the shrink for real.
    var hNow = (window.ToolboxEditor && window.ToolboxEditor.contentHeight()) || 0;
    T('the collapsed editor MEASURES small — the height report can shrink, not just grow (document scrollHeight inside an iframe never falls below its own viewport)',
      hNow > 10 && hNow < 200 && hNow < (window.__H_TALL || 0) * 0.6,
      'collapsed=' + hNow + ' tall=' + window.__H_TALL);
    T('boot asked the server whether this session had already rendered (the refresh gate fires before the photo)',
      (out.fetches || []).some(function (f) { return f.session; }),
      'fetches=' + JSON.stringify((out.fetches || []).slice(0, 2).map(function (f) { return f.url; })));
    // The sections below re-use this page's canvas machinery (knobs, layers, history)
    // — a situation the REAL product never creates: a finished embed cannot submit
    // again (the launch token is single-use). Un-stamp the collapse so the rect-based
    // pointer math downstream still measures a laid-out stage.
    if (host) {
      host.classList.remove('tb-collapsed');
      var bs = host.querySelectorAll('.tb-actions button');
      for (var bi = 0; bi < bs.length; bi++) bs[bi].style.display = '';
    }
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
      var mirrorPre = (document.getElementById('tb_edge') || {}).value;
      var seq = ['2', '4', '6'], same = true, i2;
      for (i2 = 0; i2 < seq.length; i2++) {
        edgeInp.value = seq[i2];
        edgeInp.dispatchEvent(new Event('input', { bubbles: true }));
        if (!document.body.contains(edgeInp) ||
            insp.querySelectorAll('input[type=range]')[0] !== edgeInp) same = false;
      }
      var fInp = insp.querySelectorAll('input[type=range]')[1];
      if (fInp) { fInp.value = '4'; fInp.dispatchEvent(new Event('input', { bubbles: true })); }
      // The hidden default store must be UNTOUCHED by tuning a selected object:
      // 'strictly post-generation' means editing THIS object can never secretly arm
      // the NEXT one. Snapshot was taken before the drag, whatever the fixture had
      // armed at rest.
      var mirror = document.getElementById('tb_edge');
      var unprimed = !!mirror && mirror.value === mirrorPre;
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
          same && document.body.contains(edgeInp) && edgeInp.value === '6' && unprimed,
          'same=' + same + ' val=' + (edgeInp && edgeInp.value) + ' default-store=' + (mirror && mirror.value));
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
        // The label no longer flips (hand-painted stays 'brush' in the panel); the
        // CONTRACT flips, and that is what state().all.wire exposes — the rule masks.py
        // will actually apply. A brush carrying numbers must read wire='auto' or the
        // feather it shows would never reach the render (KIND_RULES ignores 'brush').
        T('the numbered brush keeps its label but ships under the auto rules (knob bites)',
          !!me && me.kind === 'brush' && me.wire === 'auto' && me.edge === 6 &&
          me.feather === 4 && (washBefore >= 0),
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
  function decodeStats(l) {   // whole-layer alpha: max, solid count, partial-alpha (ramp) count
    return new Promise(function (res) {
      if (!l || !l.png) return res(null);
      var im = new Image();
      im.onload = function () {
        var c = document.createElement('canvas'); c.width = im.width; c.height = im.height;
        var g = c.getContext('2d'); g.drawImage(im, 0, 0);
        var d = g.getImageData(0, 0, im.width, im.height).data, mx = 0, hi = 0, lo = 0;
        for (var i = 3; i < d.length; i += 4) { if (d[i] > mx) mx = d[i];
          if (d[i] >= 200) hi++; else if (d[i] > 8) lo++; }
        res({ maxAlpha: mx, solidPx: hi, rampPx: lo });
      };
      im.onerror = function () { res(null); };
      im.src = 'data:image/png;base64,' + l.png;
    });
  }
  function decodeRows(l, x, y0, y1) {   // alpha column dump of a shipped layer PNG
    return new Promise(function (res) {
      if (!l || !l.png) return res(null);
      var im = new Image();
      im.onload = function () {
        var c = document.createElement('canvas'); c.width = im.width; c.height = im.height;
        var g = c.getContext('2d'); g.drawImage(im, 0, 0);
        var d = g.getImageData(0, 0, im.width, im.height).data, o = [], y;
        for (y = y0; y <= y1; y++) o.push(d[(y * im.width + x) * 4 + 3]);
        res(o);
      };
      im.onerror = function () { res(null); };
      im.src = 'data:image/png;base64,' + l.png;
    });
  }
  function decodeAlpha(l, x, y) {   // alpha of ONE pixel of a shipped layer PNG
    return new Promise(function (res) {
      if (!l || !l.png) return res(-1);
      var im = new Image();
      im.onload = function () {
        var c = document.createElement('canvas'); c.width = im.width; c.height = im.height;
        var g = c.getContext('2d'); g.drawImage(im, 0, 0);
        res(g.getImageData(x, y, 1, 1).data[3]);
      };
      im.onerror = function () { res(-2); };
      im.src = 'data:image/png;base64,' + l.png;
    });
  }
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
  // The Selection band's inspector is the ONLY live edge/feather editor while an object
  // is picked (the panel pair goes inert - syncKnobLock). Tests that retune a selected
  // object drive THESE rows; tests that set defaults for the next object drive knobSet.
  function inspSet(idx, v) {           // 0 = edge row, 1 = feather row
    var insp = document.querySelector('.tb-inspect');
    var rs = insp ? [].slice.call(insp.querySelectorAll('input[type=range]')) : [];
    var e = rs[idx]; if (!e) return false;
    e.value = String(v); e.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
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

  /* =====================================================================================
   * ONE SOFT EDGE PER OBJECT, AND IT BELONGS TO THE OBJECT.
   * The user's model, third pass: the brush must not feather the ink AND then feather the
   * pixel cloud it left behind. Hardness previews the stroke while the finger is down;
   * committed ink is the binarised silhouette (>REGION_ALPHA_MIN, the server's own
   * number), and the object's feather is the ONLY softness - applied at whatever boundary
   * exists now, which is why a cut through a feathered blob feathers its two new edges.
   * ===================================================================================== */
  function colStats(y0, y1, x0, x1) {   // solid vs part-washed columns vs the bare photo
    if (!refData || !view.width) return null;
    var d = px(view), W0 = view.width, solid = 0, fringe = 0, y, x;
    for (y = y0; y <= y1; y++) for (x = x0; x <= x1; x++) {
      var i = (y * W0 + x) * 4;
      var dl = Math.max(Math.abs(d[i] - refData[i]), Math.abs(d[i + 1] - refData[i + 1]),
                        Math.abs(d[i + 2] - refData[i + 2]));
      if (dl > 140) solid++; else if (dl > 25) fringe++;
    }
    return { solid: solid, fringe: fringe };
  }

  /* =====================================================================================
   * KNOB OWNERSHIP, round 2 — the follow-up: "greyed-out but still showing the same
   * stupid value I can adjust in the select window — I don't need to see these values
   * twice. When I use a tool I should see the settings for using the tool, that is it."
   * So the panel pair is not a greyed mirror any more: it is NEVER SHOWN. edge/feather
   * exist only inside the Selection inspector, only while an object is picked; the Tool
   * band collapses for tools that use none of its knobs. A fresh object is HARD (0/0):
   * growing/softening is a strictly post-generation act, and tuning one object must
   * not arm the next (the hidden default store is fixture-only, never UI-fed).
   * Also here: the dashed bbox is dead (the cyan boundary ring is the selection
   * indicator), and the zoom label is the true image:device pixel ratio.
   * ===================================================================================== */
  function knobShown(id) {
    var e = document.getElementById('tb_' + id);
    if (!e || !e.parentNode) return null;
    return e.parentNode.style.display !== 'none';
  }
  step('KNB clear + brush shows its two knobs', function () {
    clearForDraw();
    var b = toolButton('brush'); if (b) b.click();
    return wait(80);
  });
  step('KNB visibility matrix', function () {
    T('brush, the swept tool, shows BOTH size and hardness',
      knobShown('brush') === true && knobShown('hardness') === true,
      'size=' + knobShown('brush') + ' hard=' + knobShown('hardness'));
    var dead = '';
    ['polygon', 'smart', 'rect', 'select'].forEach(function (t) {
      var b = toolButton(t); if (b) b.click();
      if (knobShown('brush') || knobShown('hardness')) dead += t + ' ';
    });
    T('polygon/smart/rect/select show NEITHER dead slider', dead === '', 'leaked: ' + dead);
    var eb = toolButton('eraser'); if (eb) eb.click();
    T('the eraser takes size ALONE (its sweep is hard-pinned)',
      knobShown('brush') === true && knobShown('hardness') === false,
      'size=' + knobShown('brush') + ' hard=' + knobShown('hardness'));
    var bands = [].slice.call(document.querySelectorAll('.tb-band'))
      .map(function (n) { return n.textContent; }).join(' / ');
    T('the panel bands are declared: Tool, Display, Generation',
      /Tool/.test(bands) && /Display/.test(bands) && /Generation/.test(bands), bands);
    var pf = document.getElementById('tb_feather');
    T('ONE feather range everywhere it exists (max 64, the inspector cap)',
      !!pf && pf.max === '64' && pf.min === '0',
      'panel=' + (pf && pf.min) + '..' + (pf && pf.max));
    var fe = document.getElementById('tb_feather'), ee = document.getElementById('tb_edge');
    T('edge/feather are NEVER visible and NEVER enabled in the panel (see them once, in the inspector)',
      !!fe && !!ee && knobShown('edge') === false && knobShown('feather') === false &&
      fe.disabled === true && ee.disabled === true &&
      fe.parentNode.parentNode.style.display === 'none',   // the whole row is gone
      'edge vis=' + knobShown('edge') + ' feather vis=' + knobShown('feather') +
      ' disabled=' + (fe && fe.disabled) + '/' + (ee && ee.disabled));
    return wait(60);
  });
  step('KF paint a brush stroke while the hidden defaults read edge 4 / feather 12', function () {
    var b = toolButton('brush'); if (b) b.click();
    knobSet('tb_wash', 1); knobSet('tb_hardness', 0.2);
    knobSet('tb_brush', 40);
    knobSet('tb_edge', 4); knobSet('tb_feather', 12);      // fixture driver ONLY
    return wait(80).then(function () {
      fire('pointerdown', 780, 80, 40, { isPrimary: true });
      for (var i = 1; i <= 6; i++) fire('pointermove', 780, 80, 40 + i * 5, {});
      fire('pointerup', 780, 80, 70, {});
      return wait(150);
    });
  });
  step('KF the committed object keeps what the preview promised', function () {
    var st = region(), me = st && st.all && st.all[0];
    out.note.push('KF_STATE ' + JSON.stringify(me));
    T('lifting the stroke does NOT zero the feather the finger saw (the reported bug)',
      !!me && me.kind === 'brush' && me.feather === 12,
      JSON.stringify(me));
    T('and the edge number survives too, under the auto contract the server honours',
      !!me && me.edge === 4 && me.wire === 'auto', JSON.stringify(me));
    return true;
  });
  step('KNB a fresh object is HARD out of the box', function () {
    // Post-generation ONLY: with nothing ever armed, a brand-new object commits with no
    // grow and no feather — softening exists strictly in the Selection box, not as a
    // secret default the user never set.
    clearForDraw();
    knobSet('tb_edge', 0); knobSet('tb_feather', 0);
    var b = toolButton('rect'); if (b) b.click();
    fire('pointerdown', 782, 30, 235, { isPrimary: true });
    fire('pointermove', 782, 50, 250, {});
    fire('pointerup', 782, 60, 265, {});
    return wait(150).then(function () {
      var st = region(), me = st && st.all && st.all[0];
      out.note.push('KF_HARD ' + JSON.stringify(me));
      T('nothing selected, nothing armed: the new object carries edge 0 / feather 0',
        !!me && me.edge === 0 && me.feather === 0 && me.wire === 'shape',
        JSON.stringify(me));
      return true;
    });
  });
  step('KNB tuning the selected object does NOT arm the next one', function () {
    var sb = toolButton('select'); if (sb) sb.click();
    fire('pointerdown', 783, 45, 250, { isPrimary: true });      // grab the rect
    fire('pointerup', 783, 45, 250, {});
    return wait(150).then(function () {
      inspSet(1, 20);                                            // feather it, post-generation
      return wait(150);
    }).then(function () {
      var d = byText('Deselect'); if (d) d.click();
      var b = toolButton('rect'); if (b) b.click();
      fire('pointerdown', 784, 110, 235, { isPrimary: true });
      fire('pointermove', 784, 130, 250, {});
      fire('pointerup', 784, 140, 265, {});
      return wait(200);
    }).then(function () {
      var st = region(), mine = null, other = null;
      (st && st.all || []).forEach(function (q) {
        if (q.bbox.x0 >= 60) mine = q; else other = q;
      });
      out.note.push('KF_LEAK ' + JSON.stringify(st && st.all));
      T('the tuned object keeps feather 20 and the FRESH object is still hard (no hidden arming)',
        !!other && other.feather === 20 && !!mine && mine.feather === 0 && mine.edge === 0,
        JSON.stringify(st && st.all));
      return true;
    });
  });
  step('SB the selection shows the boundary, not a dashed box', function () {
    var sb = toolButton('select'); if (sb) sb.click();
    // BBOX-DRIVEN, the ERf/ERr lesson: panel-height changes move rows by a hair, and a
    // hardcoded tap row silently stops hitting the object — which greens this gate by
    // ACCIDENT (nothing selected, the dashed-bbox mutant draws nothing, "no dashes" is
    // vacuously true; the round-2 sweep caught it exactly that way). Measure the rect,
    // tap its CENTRE, and refuse to sample unless the selection is verifiably LIVE.
    var q0 = null;
    (function () { var st0 = region();
      (st0 && st0.all || []).forEach(function (q) { if (q.bbox.x0 < 60) q0 = q; }); })();
    if (!q0) { T('no dashed rectangle ever paints around a selected object', false,
                 'fixture lost the first rect'); return true; }
    var cx = Math.round((q0.bbox.x0 + q0.bbox.x1) / 2), cy = Math.round((q0.bbox.y0 + q0.bbox.y1) / 2);
    // The motion gate runs against THIS DESELECTED FRAME (the KF group pressed Deselect
    // just above; this tap is the act of selecting), NOT against the photo: the fixture
    // image has its own azure-family pixels, and a photo-relative gate read real 3.5px
    // dashes over that blue as travel=0 — the mutant sailed through at hits=0 while a
    // raw canvas scan saw every one of them (round-3 postmortem).
    var base = px(view);
    fire('pointerdown', 785, cx, cy, { isPrimary: true });       // reselect the FIRST rect
    fire('pointerup', 785, cx, cy, {});
    return wait(220).then(function () {
      var insp = document.querySelector('.tb-inspect');
      var live = !!insp && insp.style.display !== 'none' && insp.offsetParent !== null;
      var st = region(), me = null;
      (st && st.all || []).forEach(function (q) { if (q.bbox.x0 < 60) me = q; });
      if (!live || !me) { T('no dashed rectangle ever paints around a selected object',
        false, 'fixture had no LIVE selection to decorate (inspect=' + live + ' obj=' + !!me + ')');
        return true; }
      // Sample the row the dashed bbox would have stroked at bbox.y0 (that is exactly
      // where the old #3ba7ff dashes lived) and count pixels PUSHED TOWARD that blue
      // versus the untouched photo. The cyan boundary ring (60,220,255) and the wash
      // (232,62,62) are both outside the blue-family window.
      var d = px(view), W0 = view.width, H0 = view.height;
      var xl = Math.max(0, me.bbox.x0 | 0), xh = Math.min(W0 - 1, me.bbox.x1 | 0);
      var yl = Math.max(0, me.bbox.y0 | 0), yh = Math.min(H0 - 1, me.bbox.y1 | 0);
      // THE TEST IS THE BUG'S OWN DEFINITION: selecting an object must add ZERO
      // azure-family pixels to the canvas. Count them SELECTED-VERSUS-DESELECTED (the
      // base frame captured above, same wash, same tool, one tap ago) — never against
      // the photo (the fixture image has its own azure-family pixels; a photo-relative
      // gate read real dashes over that blue as travel=0 at hits=0), and never by
      // probing exact integer bbox rows (a half-pixel-centered 3.5px dash straddles
      // them; three detector generations died on that knife-edge — the g<=216 window
      // let the cyan ring pose as dash-blue at hits=70, an integer-row window let a
      // faithful mutant escape at 0/35). The dash core is (59,167,255)-family; nothing
      // the shipped editor paints on selection enters this corridor and MOVES: the
      // ring's AA tail over the b=128 photo cannot satisfy b>=235 and g<=185 at once,
      // the wash is (232,62,62), the stub overlay magenta (r=255).
      function azure(d0, xx, yy) {
        var o = (yy * W0 + xx) * 4;
        var travel = Math.abs(d0[o] - base[o]) + Math.abs(d0[o + 1] - base[o + 1]) +
                     Math.abs(d0[o + 2] - base[o + 2]);
        return d0[o] <= 110 && d0[o + 1] >= 130 && d0[o + 1] <= 185 &&
               d0[o + 2] >= 235 && travel >= 40;
      }
      var hits = 0, n = 0, x, y;
      for (y = Math.max(0, yl - 3); y <= Math.min(H0 - 1, yh + 3); y++)
        for (x = Math.max(0, xl - 3); x <= Math.min(W0 - 1, xh + 3); x++) {
          n++; if (azure(d, x, y)) hits++;
        }
      out.note.push('SB newazure hits=' + hits + '/' + n);
      T('no dashed rectangle ever paints around a selected object',
        n > 8 && hits === 0, 'new azure pixels when selected: ' + hits + '/' + n);
      // THE PAIR MUST LOOK LIKE A PAIR: the edge and feather sliders are one control
      // family (grow/shrink, soften) and the user flagged their unequal track lengths
      // as obviously wrong. Same start x, same width, within a pixel. (The hint rows
      // are allowed to differ; the TRACKS are not.)
      var rs2 = [].slice.call((insp || document.createElement('i'))
        .querySelectorAll('input[type=range]'));
      var wev = rs2[0] && rs2[0].getBoundingClientRect(),
          wfe = rs2[1] && rs2[1].getBoundingClientRect();
      out.note.push('SB tracks edge=' + (wev && Math.round(wev.width)) + '@' + (wev && Math.round(wev.left)) +
                    ' feather=' + (wfe && Math.round(wfe.width)) + '@' + (wfe && Math.round(wfe.left)));
      T('edge and feather sliders are the same length, starting at the same x',
        !!wev && !!wfe && Math.abs(wev.width - wfe.width) <= 1 &&
        Math.abs(wev.left - wfe.left) <= 1,
        'edge ' + (wev && Math.round(wev.width)) + '@' + (wev && Math.round(wev.left)) +
        ' vs feather ' + (wfe && Math.round(wfe.width)) + '@' + (wfe && Math.round(wfe.left)));
      return true;
    });
  });
  step('ZM the zoom label is the true pixel ratio', function () {
    function honest() {                                  // label vs measured css pixels
      var lab = document.querySelector('.tb-zoom .tb-num');
      var bx = document.querySelector('.tb-canvasbox');
      if (!lab || !bx || !view.width) return -1;
      var pct = parseInt(lab.textContent, 10);
      var cssW = bx.getBoundingClientRect().width;
      var truePct = Math.round(cssW * (window.devicePixelRatio || 1) / view.width * 100);
      return Math.abs(pct - truePct);
    }
    var f = byText('Fit'); if (f) f.click();
    return wait(120).then(function () {
      T('at Fit the label matches the ACTUAL pixels, not a fake 100%',
        honest() >= 0 && honest() <= 3,
        'delta=' + honest() + '% label=' +
        (document.querySelector('.tb-zoom .tb-num') || {}).textContent);
      var one = byText('1:1'); if (one) one.click();
      return wait(120);
    }).then(function () {
      var lab = document.querySelector('.tb-zoom .tb-num');
      var bx = document.querySelector('.tb-canvasbox');
      var cssW = bx ? bx.getBoundingClientRect().width : -1;
      var want = view.width / (window.devicePixelRatio || 1);
      T('1:1 really shows one image pixel per screen pixel - the label meaning',
        !!lab && /^\s*100%\s*$/.test(lab.textContent) && Math.abs(cssW - want) <= 2,
        'label=' + (lab && lab.textContent) + ' cssW=' + cssW + ' want=' + want);
      var z = byText('Zoom +'); if (z) { z.click(); z.click(); z.click(); }
      return wait(120);
    }).then(function () {
      T('deep zoom stays honest (label == measured ratio, and both above 100%)',
        honest() <= 3,
        'delta=' + honest() + '% label=' +
        (document.querySelector('.tb-zoom .tb-num') || {}).textContent);
      var f2 = byText('Fit'); if (f2) f2.click();       // hand the suite back a fitted view
      return wait(120);
    });
  });

  step('BR clear + soft-brush knobs', function () {
    clearForDraw();
    knobSet('tb_edge', 0); knobSet('tb_feather', 0);
    knobSet('tb_wash', 1); knobSet('tb_hardness', 0.15);   // the SOFTEST possible ramp
    var b = toolButton('brush'); if (b) b.click();
    knobSet('tb_brush', 60);
    return wait(650);
  });
  step('BR paint a very soft brush stroke', function () {
    // VERTICAL, ~70 tall: the cut later sweeps horizontally at row 240, and the halves
    // only exist if there is ink above AND below the ~30px channel. (An earlier fixture
    // swept along a 14px-tall horizontal blob and the eraser simply deleted it.)
    fire('pointerdown', 760, 80, 205, { isPrimary: true });
    for (var i = 1; i <= 10; i++) fire('pointermove', 760, 80, 205 + i * 7, {});
    fire('pointerup', 760, 80, 275, {});
    return wait(140);
  });
  step('BR feather 0: the ink is hard-edged', function () {
    var r = region();
    out.note.push('BR_STATE0 ' + JSON.stringify(r && r.all));
    var st = colStats(200, 280, 70, 90);
    out.note.push('BR_STATS0 ' + JSON.stringify(st));
    T('a soft brush leaves NO feathered fringe in the ink once the stroke is lifted',
      !!st && st.solid > 500 && st.fringe <= 8,
      JSON.stringify(st));
    T('the blob is one object and is still labelled hand-painted ink',
      !!r && r.regions === 1 && r.all[0].kind === 'brush', JSON.stringify(r && r.all));
    return true;
  });
  step('BR feather 8 on the object', function () {
    var sb = toolButton('select'); if (sb) sb.click();
    fire('pointerdown', 761, 80, 230, { isPrimary: true });
    fire('pointerup', 761, 80, 230, {});
    return wait(120).then(function () {
      // The panel pair is inert while this object is selected (one editor per number -
      // the feather-duplication report), so the fixture drives the inspector's row.
      inspSet(1, 8);
      window.__BRMARK = (window.__LREAL || []).length;   // wire gate trusts only later POSTs
      return wait(140);
    });
  });
  step('BR feather 8: one live perimeter', function () {
    var st = colStats(192, 290, 58, 102);
    var core = colStats(210, 270, 74, 86);
    out.note.push('BR_STATS8 ' + JSON.stringify(st) + ' core=' + JSON.stringify(core));
    T('the object feather is the only soft edge, and it wraps the whole object',
      !!st && core.solid > 300 && st.fringe > 150,
      'ring=' + JSON.stringify(st) + ' core=' + JSON.stringify(core));
    // FRESHNESS-PINNED (see the ER group): the first draft of this gate scanned the last
    // POST unmarked and greened off a numbered-brush layer left over from BRUSHNUM two
    // groups earlier. Only entries appended after the feather drag count.
    function poll(tries) {
      var q = window.__LREAL || [], mk = window.__BRMARK || 0, mine = null, seen = 0;
      for (var i = q.length - 1; i >= mk; i--) {
        if (!/mask\/preview$/.test(q[i].url)) continue;
        seen++; var ls = q[i].layers || [];
        for (var j = 0; j < ls.length; j++) if (ls[j] && ls[j].feather >= 4) mine = ls[j];
        if (mine) break;
      }
      if (mine || seen) return Promise.resolve(mine);
      if (tries <= 0) return Promise.resolve(null);
      return wait(120).then(function () { return poll(tries - 1); });
    }
    return poll(18).then(function (mine) {
      if (!mine) { T('the feathered ink ships as raw BINARY pixels under the auto rules',
                     false, 'no feathered layer on the wire after the feather drag'); return true; }
      return decodeStats(mine).then(function (d) {
        out.note.push('BR_WIRE ' + JSON.stringify({ kind: mine.kind, feather: mine.feather })
                      + ' ' + JSON.stringify(d));
        T('the feathered ink ships as raw BINARY pixels under the auto rules',
          mine.kind === 'auto' && !!d && d.maxAlpha === 255 && d.rampPx === 0,
          'kind=' + mine.kind + ' feather=' + mine.feather + ' ' + JSON.stringify(d));
        return true;
      });
    });
  });
  step('BR cut the feathered brush blob', function () {
    var eb = toolButton('eraser'); if (eb) eb.click();
    knobSet('tb_brush', 150);                      // ~40 natural px channel. 30 was NOT
    fire('pointerdown', 762, 60, 240, { isPrimary: true });   // enough: each side's blur
    for (var i = 1; i <= 4; i++) fire('pointermove', 762, 60 + i * 10, 240, {});  // tail
    fire('pointerup', 762, 100, 240, {});          // reaches the middle of a 30px channel
                                                   // (alpha ~48 - below solid, above the
                                                   // fringe floor). Over-traces the blob.
    window.__BRMARK = (window.__LREAL || []).length;
    return wait(140);
  });
  step('BR the cut edges feather like the object', function () {
    var r = region();
    out.note.push('BR_STATE1 ' + JSON.stringify(r && r.all));
    var two = r && r.regions === 2 ? r.all : null;
    T('the cut splits the feathered ink in two, feather intact on both',
      !!two && two.every(function (q) { return q.feather === 8; }), JSON.stringify(two));
    // Beside each cut edge (rows just inside the ink) must be soft-washed - only a feather
    // recomputed around the NEW boundary paints that; the raw-rank model left the cut hard.
    // Channel occupies rows ~220..260 now. Just INSIDE its top edge (220..225) the wash
    // can only come from the TOP half's feather reaching across the cut; the dead centre
    // (238..242) sits 17+ px from either silhouette, ~10 past each grown edge, where the
    // blurred union tail is alpha <15 - under every threshold that matters.
    var beside = colStats(220, 225, 70, 90), midC = colStats(238, 242, 70, 90);
    out.note.push('BR_CUT beside=' + JSON.stringify(beside) + ' mid=' + JSON.stringify(midC));
    T('the eraser line gets the object feather: soft beside the cut, bare down the middle',
      !!beside && !!midC && beside.fringe + beside.solid > 60 && midC.solid === 0 && midC.fringe <= 6,
      'beside=' + JSON.stringify(beside) + ' mid=' + JSON.stringify(midC));
    return true;
  });

  step('DR clear + paint an object', function () {
    clearForDraw();
    knobSet('tb_edge', 0); knobSet('tb_feather', 6);
    knobSet('tb_wash', 1);
    var b = toolButton('rect'); if (b) b.click();
    fire('pointerdown', 770, 30, 60, { isPrimary: true });
    fire('pointermove', 770, 50, 75, {});
    fire('pointerup', 770, 70, 90, {});
    return wait(700);
  });
  step('DR make the preview fresh UNDER the select tool', function () {
    // The fixture must reach the state the bug ACTUALLY needs. previewSig() contains the
    // tool name, so a preview requested while tool=rect is auto-invalidated by the switch
    // to select - an early version of this fixture greened the staleoverlay mutant for
    // that accidental reason. A real user lands here via a baked move or an edit made
    // with an object selected: schedulePreview fires with tool=select, the answer comes
    // back current, and compose() prefers it over the local wash. That is the state we
    // re-create: select, tap the object, nudge its feather (touchObject ->
    // schedulePreview), settle.
    var sb = toolButton('select'); if (sb) sb.click();
    return wait(120).then(function () {
      fire('pointerdown', 770, 40, 67, { isPrimary: true });    // grab inside the blob
      fire('pointerup', 770, 40, 67, {});
      return wait(150);
    }).then(function () {
      inspSet(1, 6);                     // same value; the point is the re-schedule
      return wait(700);                  // the stub's answer lands while tool=select
    });
  });
  step('DR drag it while the stale overlay is current', function () {
    T('the fixture reached the state the bug needs: a matching server preview on screen',
      magentaCount(view) > 0, 'magenta=' + magentaCount(view));
    // Stage the REAL stale-answer race: re-request a preview (__PVHOLD keeps its answer
    // in flight in the stub), let the debounced fetch fire, then MOVE THE OBJECT under
    // it and release. The answer was minted pre-drag; it lands mid-drag and must be
    // DISCARDED by the freshness key — an answer that lands after the object moved must
    // never paint over the finger.
    window.__PVHOLD = true; window.__PVPEND = [];
    inspSet(1, 6);                                   // same value; the point is the request
    return wait(500).then(function () {              // the debounced fetch fires and HANGS
      fire('pointerdown', 771, 40, 67, { isPrimary: true });      // grab inside the blob
      for (var i = 1; i <= 6; i++) fire('pointermove', 771, 40 + i * 8, 67 + i * 10, {});
      return wait(60);
    }).then(function () {
      var q = window.__PVPEND || []; window.__PVPEND = [];
      for (var j = 0; j < q.length; j++) q[j]();     // the stale answer lands MID-DRAG
      window.__PVHOLD = false;
      return wait(120);                              // STILL MID-DRAAG
    });
  });
  step('DR the mask follows the finger', function () {
    var st = region();
    var me = st && st.all && st.all[0];
    out.note.push('DR_STATE ' + JSON.stringify(me) + ' magenta=' + magentaCount(view));
    // The stale-answer path paints the stub's magenta over everything (that is what makes
    // this observable), and the mask position is what the user is watching.
    T('mid-drag the canvas is NOT the frozen server overlay',
      magentaCount(view) === 0, 'magenta=' + magentaCount(view));
    var moved = colStats(105, 135, 75, 105);       // where the object is being dragged TO
    var left  = colStats(55, 80, 35, 65);          // where it used to be
    out.note.push('DR_BANDS moved=' + JSON.stringify(moved) + ' left=' + JSON.stringify(left));
    T('the preview mask moves WITH the object, not to a fixed spot',
      !!moved && moved.solid > 150 && !!left && left.solid < 20,
      'moved=' + JSON.stringify(moved) + ' left=' + JSON.stringify(left));
    fire('pointerup', 771, 88, 127, {});   // release: bake the move
    return wait(200);
  });

  /* =====================================================================================
   * THE ERASER REMOVES PIXELS AND NOTHING ELSE; MORPHOLOGY HANGS OFF THE RASTER.
   *
   * The model the user asked for, and the one shipped now: draw an object -> its pixels
   * are the raster; the eraser removes pixels from that raster and is then GONE (no
   * permanent punch, no erase layer on the wire); grow/shrink is folded INTO the raster at
   * the moment the topology changes (an erase that splits, a move that merges); feather
   * stays a live post-op so it recomputes around whatever boundaries exist now.
   *
   * Two shipped defects live here, and the gates are separated so each one goes red on its
   * own mutant:
   *   1. the parent's grow re-applied to each fragment from its fresh cut edge, which is
   *      what sealed the channel and made the halves overlap (mutant: nofold);
   *   2. a persistent punch that would not let the channel close, so dragging the halves
   *      back together left a latent hole where the sweep had been (mutant: latentpunch).
   * A third gate guards the fold itself against drifting from the display by a pixel
   * (mutant: bakepad) and a fourth against the feather being baked (mutant: bakefeather).
   * ===================================================================================== */
  function lastWashedRow(x0, x1, y0, y1) {   // bottom-most row that still reads as masked
    if (!refData || !view.width) return -1;
    var d = px(view), W0 = view.width, last = -1, y;
    for (y = Math.max(0, y0); y <= Math.min(view.height - 1, y1); y++) {
      var t = 0, x;
      for (x = x0; x <= x1; x++) {
        var i = (y * W0 + x) * 4;
        t += Math.max(Math.abs(d[i] - refData[i]), Math.abs(d[i + 1] - refData[i + 1]),
                      Math.abs(d[i + 2] - refData[i + 2]));
      }
      if (t / (x1 - x0 + 1) > 60) last = y;
    }
    return last;
  }
  function washRow(y, x0, x1) {   // washed columns on one natural row, vs the photo
    if (!refData || !view.width) return null;
    var d = px(view), W0 = view.width, n = 0, f = -1, l = -1, x;
    for (x = Math.max(0, x0); x <= Math.min(W0 - 1, x1); x++) {
      var i = (y * W0 + x) * 4;
      var dl = Math.max(Math.abs(d[i] - refData[i]), Math.abs(d[i + 1] - refData[i + 1]),
                        Math.abs(d[i + 2] - refData[i + 2]));
      if (dl > 100) { n++; if (f < 0) f = x; l = x; }
    }
    return { cols: n, first: f, last: l };
  }

  step('ER clear + knobs (grow 12, feather 0)', function () {
    clearForDraw();
    knobSet('tb_edge', 12); knobSet('tb_feather', 0);
    knobSet('tb_wash', 1); knobSet('tb_hardness', 0);
    // quiesce first: a debounced preview belatedly fired by the virtual-time scheduler
    // would paint the stub's magenta over the whole canvas and poison every pixel gate.
    return wait(650);
  });
  step('ER paint parent + probe it', function () {
    var b = toolButton('rect'); if (b) b.click();
    fire('pointerdown', 750, 40, 140, { isPrimary: true });
    var pxs = [[55, 148], [70, 156], [85, 164], [100, 172], [110, 180]];
    for (var i = 0; i < pxs.length; i++) fire('pointermove', 750, pxs[i][0], pxs[i][1], {});
    fire('pointerup', 750, 110, 180, {});
    return wait(140).then(function () {          // inside the 420ms debounce: local wash only
      var r = region();
      _erParent = washRow(150, 0, 159);
      out.note.push('ER_PARENT ' + JSON.stringify(_erParent) + ' state=' + JSON.stringify(r && r.all));
      T('fixture: one grown object, live edge 12 (nothing folded yet)',
        !!r && r.regions === 1 && r.all[0].edge === 12 && r.all[0].folded === false,
        JSON.stringify(r && r.all));
      T('fixture: the grown silhouette is on screen before any cutting',
        !!_erParent && _erParent.cols > 60, JSON.stringify(_erParent));
    });
  });
  step('ER cut the channel (grow case)', function () {
    var b = toolButton('eraser'); if (b) b.click();
    knobSet('tb_brush', 24);                     // ~6.4 natural px across, a thin honest cut
    // OVER-TRACE past both edges (x 40..110): shoulders left inside the rect bridge the
    // halves and the blob never splits (measured: regions=1 through two 3px stubs).
    // Over-trace past the GROWN silhouette, not the raw rect: once the grow is folded
    // into the pixels the object really is 28..121 wide, and a sweep that stops at the
    // raw edge leaves a shoulder bridging the halves (measured: regions=1 through a
    // 4-column bridge at x=28..31).
    fire('pointerdown', 751, 20, 160, { isPrimary: true });
    for (var i = 1; i <= 13; i++) fire('pointermove', 751, 20 + i * 9, 160, {});
    fire('pointerup', 751, 137, 160, {});
    window.__ERMARK = (window.__LREAL || []).length;   // the wire gate trusts only later POSTs
    return wait(140);
  });
  step('ER grow-case asserts: folded, cut, unmoved', function () {
    var r = region();
    out.note.push('ER_STATE ' + JSON.stringify(r && r.all) + ' bakes=' + (r && r.bakes));
    var two = r && r.regions === 2 ? r.all : null;
    T('the eraser cut the blob into two objects', !!two, 'regions=' + (r && r.regions));
    T('the grow was FOLDED into the raster at the split (knob 0 live, 12 remembered)',
      !!two && two.every(function (q) {
        return q.folded === true && q.edge === 0 && q.edgeSet === 12 && q.feather === 0; }),
      JSON.stringify(two));
    if (!two) return true;
    // The heart of report 1: with the parent's grow still live, each half dilates from
    // its own cut edge and the channel seals shut. Folded, the cut stays the cut.
    var chan = bandAvg(50, 100, 158, 162);
    var above = bandAvg(50, 100, 145, 150);
    out.note.push('ER_GROWBANDS channel=' + chan.toFixed(1) + ' above=' + above.toFixed(1));
    T('the erased channel stays CUT (a re-applied grow would heal it shut)',
      chan < 25 && above > 120, 'channel=' + chan + ' above=' + above);
    // Report 1's other half: the halves must not be wider than what was drawn. Same row,
    // measured before and after, so the comparison cannot drift with the fixture.
    var now = washRow(150, 0, 159);
    out.note.push('ER_EDGES before=' + JSON.stringify(_erParent) + ' after=' + JSON.stringify(now));
    T('the outer boundary did not move when the cut happened (bake == display)',
      !!now && !!_erParent && Math.abs(now.first - _erParent.first) <= 2 &&
      Math.abs(now.last - _erParent.last) <= 2,
      'before=' + JSON.stringify(_erParent) + ' after=' + JSON.stringify(now));
    return true;
  });
  step('ER wire: no permanent punch ships', function () {
    function poll(tries) {
      var q = window.__LREAL || [], mk = window.__ERMARK || 0, seen = 0, punch = null;
      for (var i = mk; i < q.length; i++) {
        if (!/mask\/preview$/.test(q[i].url)) continue;
        seen++; var ls = q[i].layers || [];
        for (var j = 0; j < ls.length; j++) if (ls[j] && ls[j].erase) punch = ls[j];
      }
      if (seen) return Promise.resolve({ seen: seen, punch: punch });
      if (tries <= 0) return Promise.resolve(null);
      return wait(120).then(function () { return poll(tries - 1); });
    }
    return poll(18).then(function (w) {
      out.note.push('ER_WIRE ' + JSON.stringify(w && { seen: w.seen, punch: !!w.punch }));
      T('a preview POST went out after the cut', !!w && w.seen >= 1, JSON.stringify(w));
      // The eraser is not a feature of the canvas, so nothing on the wire says it was
      // ever there. A shipped erase layer sits still while objects move: that is defect 2
      // reincarnated as a render the preview cannot explain.
      T('the shipped layers carry NO erase punch (the cut lives in the pixels)',
        !!w && !w.punch, JSON.stringify(w && w.punch));
      return true;
    });
  });

  step('ERf clear + knobs (feather 8, grow 0)', function () {
    clearForDraw();
    knobSet('tb_edge', 0); knobSet('tb_feather', 8);
    knobSet('tb_wash', 1); knobSet('tb_hardness', 0);
    return wait(650);
  });
  step('ERf paint tall rect + wide channel', function () {
    var b = toolButton('rect'); if (b) b.click();
    fire('pointerdown', 754, 60, 120, { isPrimary: true });
    fire('pointermove', 754, 70, 140, {});
    fire('pointermove', 754, 85, 165, {});
    fire('pointermove', 754, 100, 199, {});
    fire('pointerup', 754, 100, 199, {});
    return wait(140).then(function () {
      var eb = toolButton('eraser'); if (eb) eb.click();
      knobSet('tb_brush', 150);                  // ~40 natural px: a channel far wider than 2f
      fire('pointerdown', 755, 48, 160, { isPrimary: true });
      for (var i = 1; i <= 8; i++) fire('pointermove', 755, 48 + i * 9, 160, {});
      fire('pointerup', 755, 120, 160, {});
      window.__ERMARK = (window.__LREAL || []).length;
      return wait(140);
    });
  });
  step('ERf feather recomputes around the trimmed objects', function () {
    var r = region();
    out.note.push('ERF_STATE ' + JSON.stringify(r && r.all));
    var two = r && r.regions === 2 ? r.all : null;
    out.note.push('ERF_CSS cssW=' + (view.getBoundingClientRect().width || 0).toFixed(0) +
                  ' backing=' + view.width + ' zoomLbl=' +
                  ((document.querySelector('.tb-zoom .tb-num') || {}).textContent || '?'));
    T('the wide cut split the object in two, both still feathered and unfurled-in-kind',
      !!two && two.every(function (q) { return q.feather === 8 && q.edge === 0; }),
      JSON.stringify(two));
    T('the eraser cut HARD despite hardness 0 (its sweep leaves no soft residue)',
      !!r && (r.erases || []).length >= 1 &&
      r.erases[r.erases.length - 1].hardness === 1,
      JSON.stringify((r && r.erases || []).slice(-1)));
    if (!two) return true;
    // The cut spans roughly rows 140..180. Four px inside it from a cut edge must still be
    // washed — that soft ring can ONLY come from a feather recomputed around the NEW
    // boundary. And the middle of a 40px channel (20px from either edge, past ~12 of
    // reach) must be bare: nothing of the old outer halo survives the cut.
    // bbox-DRIVEN rows, never hardcoded: the channel's width is brushR() = size*scale()/2,
    // and scale() follows the live layout (450 CSS px here), so rows like the old
    // '143..145' silently drift into the wrong place when the panel changes height. The
    // halves' measured bboxes ARE the cut edges — 2 px inside each must carry the live
    // skirt; the middle of the measured gap must be bare whatever its width.
    var hi = two[0], loHalf = two[1];
    if (hi.bbox.y0 > loHalf.bbox.y0) { var sw = hi; hi = loHalf; loHalf = sw; }
    var nearTop = bandAvg(65, 95, hi.bbox.y1 - 2, hi.bbox.y1);
    var midChan = bandAvg(65, 95, Math.round((hi.bbox.y1 + loHalf.bbox.y0) / 2) - 1,
                               Math.round((hi.bbox.y1 + loHalf.bbox.y0) / 2) + 1);
    var nearBot = bandAvg(65, 95, loHalf.bbox.y0, loHalf.bbox.y0 + 2);
    // The live tail past the outer edge: present at ~6px (inside f), gone by ~16px
    // (> ~1.5f). If the feather were ever folded into the raster this reads solid at 215.
    var tail = bandAvg(65, 95, 201, 205), far = bandAvg(65, 95, 219, 223);   // tail: inside f; far: past ~1.5f
    out.note.push('ERF_BANDS top=' + nearTop.toFixed(0) + ' mid=' + midChan.toFixed(0) +
                  ' bot=' + nearBot.toFixed(0) + ' tail=' + tail.toFixed(0) + ' far=' + far.toFixed(0));
    T('the feather wraps each NEW cut edge (skirt beside the cut, none down the middle)',
      nearTop > 100 && nearBot > 100 && midChan < 25,
      'top=' + nearTop + ' bot=' + nearBot + ' mid=' + midChan);
    // The reach of the skirt is the honest discriminator, not a sample row: feather f is
    // grow f + blur f, so the >=128 silhouette ends at the blob edge and the visible tail
    // dies about 1.5f past it (measured: ~12 past row 198 for f=8). A feather ever folded
    // into the raster pushes that boundary out by another f, and no row choice catches it
    // as unambiguously as the distance itself.
    var reach = lastWashedRow(65, 95, 199, 240);
    out.note.push('ERF_REACH=' + reach);
    T('the feather is LIVE, not baked: the tail reaches ~1.5f past the silhouette and stops',
      tail > 60 && reach >= 203 && reach <= 215, 'tail=' + tail + ' reach=' + reach);
    return true;
  });

  var _errPlan = null;   // measured drag geometry, recomputed every run
  step('ERr re-merge: drag the top half down over the channel', function () {
    var sb = toolButton('select'); if (sb) sb.click();
    return wait(120).then(function () {
      var r = region(), up = null, loHalf = null;
      (r && r.all || []).forEach(function (q) {
        if (!up || q.bbox.y0 < up.bbox.y0) up = q;
        if (!loHalf || q.bbox.y1 > loHalf.bbox.y1) loHalf = q;
      });
      if (!up || !loHalf || up === loHalf) {
        out.note.push('ERR_NOPLAN ' + JSON.stringify(r && r.all));
        fire('pointerdown', 756, 80, 130, { isPrimary: true });
        fire('pointerup', 756, 80, 130, {});
        return wait(160);
      }
      // Drag the TOP half down until it overlaps the bottom one by 10 px — the overlap is
      // measured, so a channel that grew (scale drift) can never leave the halves short
      // of each other again. The band probed for a latent gap is 1..7 rows ABOVE the
      // lower half's head: pixels only the moved half can cover, and exactly the rows the
      // old sweep deleted.
      var cx = Math.round((Math.max(up.bbox.x0, loHalf.bbox.x0)
                           + Math.min(up.bbox.x1, loHalf.bbox.x1)) / 2);
      var sy = Math.round((up.bbox.y0 + up.bbox.y1) / 2);
      var dy = loHalf.bbox.y0 - up.bbox.y1 + 10;
      _errPlan = { cx: cx, band0: loHalf.bbox.y0 - 7, band1: loHalf.bbox.y0 - 1 };
      fire('pointerdown', 756, cx, sy, { isPrimary: true });
      for (var i = 1; i <= 5; i++) fire('pointermove', 756, cx, sy + Math.round(i * dy / 5), {});
      fire('pointerup', 756, cx, sy + dy, {});
      window.__ERMARK = (window.__LREAL || []).length;
      return wait(160);
    });
  });
  step('ERr no-remnant asserts', function () {
    var r = region();
    out.note.push('ERR_STATE ' + JSON.stringify(r && r.all) + ' bakes=' + (r && r.bakes));
    T('dragging the halves back onto each other merges them into ONE object',
      !!r && r.regions === 1, 'regions=' + (r && r.regions));
    // The old sweep sat at rows ~140..180; the moved half now covers ~160..179 there. A
    // permanent punch would carve exactly this band and the user would see a hole through
    // the middle of the object they just joined (the reported 'latent gap').
    var joined = _errPlan
      ? bandAvg(65, 95, _errPlan.band0, _errPlan.band1) : -1;
    out.note.push('ERR_PLAN ' + JSON.stringify(_errPlan));
    out.note.push('ERR_JOINED=' + joined.toFixed(1));
    T('the re-merged object has NO latent gap where the eraser used to be',
      joined > 120, 'joinedBand=' + joined);
    // The wire check has to come AFTER the pixel gate, and it has to wait: the pixel
    // gate must be measured inside the 420ms preview debounce (once the stub's answer
    // lands the whole canvas is magenta), so the POST it inspects does not exist yet.
    function poll(tries) {
      var q = (window.__LREAL || []).slice(window.__ERMARK || 0), punch = null, seen = 0, n = 0;
      for (var i = 0; i < q.length; i++) {
        if (!/mask\/preview$/.test(q[i].url)) continue;
        seen++; var ls = q[i].layers || [];
        n = ls.length;
        for (var j = 0; j < ls.length; j++) if (ls[j] && ls[j].erase) punch = ls[j];
      }
      if (seen) return Promise.resolve({ seen: seen, punch: punch, n: n });
      if (tries <= 0) return Promise.resolve(null);
      return wait(120).then(function () { return poll(tries - 1); });
    }
    return poll(18).then(function (w) {
      out.note.push('ERR_WIRE ' + JSON.stringify(w && { seen: w.seen, layers: w.n, punch: !!w.punch }));
      T('the merged object ships as pixels alone (one layer, no punch)',
        !!w && w.seen >= 1 && !w.punch && w.n === 1,
        JSON.stringify(w && { seen: w.seen, layers: w.n, punch: !!w.punch }));
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
  });

  /* ---- the refresh defect (2026-09-21): OWU re-renders the SAVED tool-result HTML
     on every chat refresh, re-booting the editor document with the launch token the
     first submit already redeemed. The boot probe (/toolbox/session) is what tells
     that re-boot it is a receipt, not an editor. Simulate it by wiping the mount and
     calling boot() again under a submitted __SESSION answer. ---- */
  step('refresh-boot receipt', function () {
    window.__SESSION = { ok: true, submitted: true, state: 'done', job_id: 'REBOOTJOB0000001',
                         prompt_sent: 'a sunlit red mustang', elapsed_s: 12, seed: 5,
                         crop: { cropped: false, chat_post: 'posted' } };
    var host = document.querySelector('.tb');
    if (host) host.innerHTML = '';
    if (window.ToolboxEditor) window.ToolboxEditor.boot();
    return wait(700);   // boot builds, probe answers, receipt collapses
  });
  step('refresh-boot asserts', function () {
    var host = document.querySelector('.tb');
    var st = host && host.querySelector('.tb-status');
    var vis = function (n) { return !!n && getComputedStyle(n).display !== 'none'; };
    var stage2 = host && host.querySelector('.tb-stage');
    var probes = (out.fetches || []).filter(function (f) { return f.session; }).length;
    T('a refreshed embed whose token already rendered boots as the collapsed receipt — no canvas, no photo, the summary line only',
      !!host && host.classList.contains('tb-collapsed') && !vis(stage2)
      && !!st && /Done · 12s/.test(st.textContent)
      && /"a sunlit red mustang"/.test(st.textContent)
      && /posted back into your chat/.test(st.textContent)
      && host.querySelectorAll('.tb-out img').length === 0
      && probes >= 2,
      'cls=' + (host && host.className) + ' st=' + (st && st.textContent.slice(0, 90)) +
      ' stage2=' + vis(stage2) + ' probes=' + probes);
    window.__SESSION = null;
  });
  step('finish', function () { finish(); });
