/* Spike harness controller.
 *
 * It receives tb-probe messages and iframe:height reports from BOTH frames and sizes both
 * of them. That detail matters: the first version resized only panel B, so panel A sat at
 * its 240px min-height with an internal scrollbar and looked, to a human reading the page,
 * exactly like "A is small and scrollable, B is tall". A harness that treats its own
 * control panel differently from the thing under test measures nothing, so both frames now
 * go through one code path and report side by side.
 *
 * Messages are attributed by `e.source`, not by arrival order — the only reliable way to
 * tell two same-content frames apart, and it works across an opaque origin where the frame
 * cannot be introspected at all.
 */
(function () {
  'use strict';
  var frames = { A: document.getElementById('fa'), B: document.getElementById('fb') };
  var state = { A: {}, B: {} };
  // Cells are tracked with their SOURCE, because the two kinds mean opposite things:
  //   probe  — the frame must fill these unprompted; a missing one IS a finding.
  //   action — only fillable after the human acts (drag / press Preview mask); a missing
  //            one means "you have not done that step yet", never "the sandbox blocked me".
  // Conflating them is what produced the misleading "probe stayed silent" verdict while the
  // real story was simply that the round-trip step had not been performed.
  var CELLS = {
    A: [{ id: 'cnta', kind: 'probe', what: 'A: events' },
        { id: 'canvasa', kind: 'probe', what: 'A: canvas events' },
        { id: 'ha', kind: 'probe', what: 'A: height' },
        { id: 'scrolla', kind: 'probe', what: 'A: scrollbar' },
        { id: 'exporta', kind: 'probe', what: 'A: export self-test' },
        { id: 'servera', kind: 'action', what: 'A: server round-trip' }],
    B: [{ id: 'cnt', kind: 'probe', what: 'B: events' },
        { id: 'canvas', kind: 'probe', what: 'B: canvas events' },
        { id: 'h', kind: 'probe', what: 'B: height' },
        { id: 'scrollb', kind: 'probe', what: 'B: scrollbar' },
        { id: 'export', kind: 'probe', what: 'B: export self-test' },
        { id: 'server', kind: 'action', what: 'B: server round-trip' }]
  };
  var filled = {}, loaded = { A: false, B: false }, bootAt = Date.now();

  function put(id, txt) {
    var e = document.getElementById(id);
    if (!e) return;
    if (txt === undefined || txt === null || txt === '') { e.textContent = '—'; return; }
    e.textContent = String(txt);
    filled[id] = true;
  }
  // Hint text that deliberately does NOT mark a cell filled, so the deadman still knows the
  // step was never performed even though the reader can see what to do.
  function hint(id, txt) { var e = document.getElementById(id); if (e) e.textContent = txt; }
  function verdict(cls, txt) {
    var v = document.getElementById('verdict');
    if (!v) return;
    v.className = 'verdict ' + cls;
    v.textContent = txt;
  }
  function which(win) { for (var k in frames) if (frames[k] && frames[k].contentWindow === win) return k; return null; }
  function log(s) {
    var el = document.getElementById('log');
    if (!el) return;
    el.textContent += s + '\n';
    if (el.textContent.length > 40000) el.textContent = el.textContent.slice(-28000);
    el.scrollTop = el.scrollHeight;
  }
  ['A', 'B'].forEach(function (k) {
    if (frames[k]) frames[k].addEventListener('load', function () {
      loaded[k] = true;
      log(k + ': frame fired load');
    });
  });

  /* Deadman: after 5s every unfilled cell says so in words, and the verdict explains which
   * stage died — distinguishing "the sandbox blocked it" from "the harness never ran" from
   * "you have not done this step yet", which is the whole reason this page exists.
   * Missing cells are NAMED: an anonymous count once told a user the probe was silent when
   * the truth was that they simply had not painted in that panel yet. */
  function missingOf(k, kind) {
    return CELLS[k].filter(function (c) { return c.kind === kind && !filled[c.id]; });
  }
  ['A', 'B'].forEach(function (k) {
    CELLS[k].forEach(function (c) {
      if (c.kind === 'action') hint(c.id, 'not yet — paint a stroke in panel ' + k);
    });
  });
  setInterval(function () {
    var mA = missingOf('A', 'probe'), mB = missingOf('B', 'probe');
    var pending = missingOf('A', 'action').concat(missingOf('B', 'action'));
    var silentProbe = mA.concat(mB);
    if (Date.now() - bootAt < 5000) {
      if (!silentProbe.length) verdict('wait', 'reporting…');
      return;
    }
    silentProbe.concat(pending).forEach(function (c) {
      var e = document.getElementById(c.id);
      if (e && !filled[c.id] && c.kind === 'probe') e.textContent = 'NO DATA';
    });
    if (!silentProbe.length && !pending.length) {
      verdict('pass', 'Both frames reported everything, including the server round-trip. ' +
                      'Read the four cells per panel — that IS the M0 result.');
      return;
    }
    if (!silentProbe.length) {
      verdict('wait', 'RIG IS HEALTHY — waiting on you, not on the browser. Not yet done: ' +
              pending.map(function (c) { return c.what; }).join(', ') +
              '. Drag on the photo in BOTH panels — the server round-trip fires on its own once you paint.');
      return;
    }
    var why;
    if (!loaded.A && !loaded.B) {
      why = 'NEITHER PANEL EVER LOADED. This is a harness/transport failure, not a sandbox ' +
            'result: check the "B document fetch" cell and the log below.';
    } else if (!loaded.B) {
      why = 'Panel B never loaded its srcdoc — check the fetch cell (CORS / bad URL).';
    } else if (!loaded.A) {
      why = 'Panel A never loaded — the control is broken, so no B result can be interpreted.';
    } else {
      why = 'Both frames loaded but the probe never reported [' +
            silentProbe.map(function (c) { return c.what; }).join(', ') +
            ']: instrumentation is not attached, or postMessage to the parent is blocked. ' +
            'Needs ?probe=1 AND spike_enabled — see /toolbox/health "spike".';
    }
    verdict('fail', 'INCOMPLETE: ' + why);
  }, 1000);

  /* Size a frame to the height it reported (+ its border). The real Open WebUI renderer
   * honours iframe:height the same way, so matching it is what makes panel B a fair
   * simulation of the embed mount rather than a flattering one. */
  function applyHeight(k, h) {
    var f = frames[k]; if (!f || !h) return;
    state[k].h = h;
    var want = h + 4;
    if ((parseInt(f.style.height, 10) || 0) !== want) f.style.height = want + 'px';
  }

  window.addEventListener('message', function (e) {
    var d = e.data; if (!d || typeof d !== 'object') return;
    var k = which(e.source);
    if (d.type === 'iframe:height') {
      // An unattributable report still has to size *something*; guess B, the panel under
      // test, but never silently drop it — a dropped report is what hides a working frame.
      if (d.height > 60) applyHeight(k || 'B', d.height);
      put(k === 'A' ? 'ha' : 'h', d.height + 'px');
      return;
    }
    if (d.type !== 'tb-probe' || !k) return;
    var st = state[k], c = d.counts || {};
    st.events = (c.pointerdown || 0) + (c.pointermove || 0) + (c.touchmove || 0) +
                (c.touchstart || 0) + (c.mousemove || 0);
    st.canvas = d.canvasSeen ? 'YES' : 'no';
    st.content = d.innerH || 0; st.vh = d.vh || 0;
    put(k === 'A' ? 'cnta' : 'cnt', st.events);
    put(k === 'A' ? 'canvasa' : 'canvas', st.canvas);
    // Content taller OR wider than the viewport we were given == a scrollbar the user
    // must fight. Name which axis, because the two have different causes and different
    // fixes: Y-too-tall means the height report under-shot; X-too-wide is almost always
    // the vertical scrollbar stealing width out from under a 100%-laid-out element.
    var st2 = state[k];
    var overY = d.overflowY || 0, overX = d.overflowX || 0;
    var msg = [];
    if (overY > 4) msg.push('↓ ' + overY + 'px hidden');
    if (overX > 4) msg.push('→ ' + overX + 'px hidden');
    put(k === 'A' ? 'scrolla' : 'scrollb', msg.length ? ('YES — ' + msg.join(', ')) : 'no (fits)');
    st2.overflowX = overX; st2.overflowY = overY;

    if (d.exportTest && d.exportTest !== 'skipped') {
      put(k === 'A' ? 'exporta' : 'export',
          d.exportTest === 'ok' ? ('alpha ' + d.alphaOn + ' / expect ' + d.expectAlphaOn) : d.exportTest);
    }
    if (d.lastFetch && d.lastFetch !== 'none') {
      // Show the probe's own rendering verbatim ("POST /toolbox/mask/preview 200
      // cov=0.0149 empty=false tiny=false") rather than re-slicing it by position: the
      // field order is the probe's business, and positional parsing here once turned a
      // coverage number into "cov=empty=false".
      put(k === 'A' ? 'servera' : 'server', String(d.lastFetch).slice(0, 90));
    }
  }, false);

  /* Same-origin introspection is only possible for panel A: panel B is an opaque-origin
   * srcdoc frame whose contentDocument is null, which is itself the finding under test.
   * Use it to cross-check A's self-report so a broken probe cannot mask a harness bug. */
  setInterval(function () {
    try {
      // Only once A has actually fired load. Against a frame that never loaded,
      // scrollHeight on the about:blank document is small and this would stamp
      // "no (fits)" — a *confident* reading for a panel that was never there.
      if (!loaded.A) return;
      var doc = frames.A.contentDocument;
      if (!doc || !doc.documentElement) return;
      var de = doc.documentElement;
      var sh = de.scrollHeight, ch = frames.A.clientHeight;
      var sw = de.scrollWidth, cw = frames.A.clientWidth;
      var axes = [];
      if (sh - ch > 4) axes.push('↓ ' + (sh - ch) + 'px');
      if (sw - cw > 4) axes.push('→ ' + (sw - cw) + 'px');   // the axis I used to be blind to
      put('scrolla', axes.length ? ('YES — ' + axes.join(', ') + ' hidden') : 'no (fits)');
      put('ha', (state.A.h || 0) + 'px reported (doc ' + sh + 'x' + sw + ', box ' + ch + 'x' + cw + ')');
    } catch (e) { /* cross-origin: expected, ignored */ }
  }, 1500);

  /* Synthetic stroke, so "does a drag reach the frame?" is a number and not an argument.
   * Only same-origin (panel A) can be scripted; on panel B this says so out loud instead
   * of failing silently, and the probe is what answers the question there. */
  function paintIn(k) {
    var f = frames[k];
    try {
      var doc = f.contentDocument;
      if (!doc) {
        log(k + ': contentDocument is null (opaque origin — cannot be scripted). That is ' +
            'expected for panel B; read the probe cells there instead.');
        return;
      }
      var c = doc.getElementById('tb_view');
      if (!c) { log(k + ': no canvas yet'); return; }
      var r = c.getBoundingClientRect();
      var x0 = r.left + r.width * 0.4, y0 = r.top + r.height * 0.4;
      ['pointerdown', 'pointermove', 'pointermove', 'pointerup'].forEach(function (t, i) {
        var ev = new PointerEvent(t, {
          bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true,
          clientX: x0 + (t === 'pointermove' ? i * 22 : 0), clientY: y0 + i * 11
        });
        c.dispatchEvent(ev);
      });
      log(k + ': synthetic stroke dispatched');
    } catch (e) { log(k + ': script failed — ' + e.message); }
  }

  // (clickPreview retired with the Preview mask button: the server round-trip cell
  //  fills from the AUTOMATIC preview now, so there is nothing left to click.)


  var ta = document.getElementById('t-a'), tb = document.getElementById('t-b'),
      tall = document.getElementById('t-all'),
      rel = document.getElementById('reload');
  if (ta) ta.addEventListener('click', function () { paintIn('A'); });
  if (tb) tb.addEventListener('click', function () { paintIn('B'); log('B is unscribable by design — drag on it yourself.'); });
  if (tall) tall.addEventListener('click', function () { paintIn('A'); paintIn('B'); });
  // Bound here rather than in an inline script element in harness.html: the template is
  // meant to carry markup only, so every line of controller logic lives in one file that
  // the suite actually reads. (An inline handler was the last one, and it is gone. Do not
  // write the opening/closing tag names out literally in this file either — the suite
  // counts them to prove the controller sits inside exactly one script element.)
  if (rel) rel.addEventListener('click', function () { location.reload(); });

  (function init() {
    /* The embed URL arrives as the literal token below, which web.harness_document()
     * replaces with a JSON-quoted string (web.py:115). Reading it out of the query string
     * instead — which an earlier rewrite of this file did — leaves `embed` null, so neither
     * frame is ever given a source and the ENTIRE rig silently reports its own "?"
     * placeholders as if they were measurements.
     *
     * CONTRACT, stated once so greps can verify it: the placeholder on the assignment line
     * below must appear ONLY there and must NOT be wrapped in quotes. web.harness_document()
     * substitutes it via _json_for_script() (json.dumps), which already supplies the quotes
     * and performs the `</` escaping; wrapping it here as well gave `embed` a leading quote
     * character, so both iframes requested a bogus path and neither panel ever loaded.
     * Same convention as the config bootstrap in embed_document (the window global the
     * editor reads), which is likewise injected unquoted.
     *
     * Therefore: never spell the placeholder token in prose in this file. str.replace
     * substitutes EVERY occurrence, comments included, so a spelled token stops being a
     * sentinel and fools the greps that guard this seam. Refer to it descriptively.
     *
     * The typeof guard keeps an unsubstituted template from throwing: a bare undeclared
     * identifier would raise ReferenceError and take the controller down before the deadman
     * could report anything, whereas `typeof` on an undeclared name is safe. */
    var embed = (typeof __TB_EMBED_URL__ === 'string') ? __TB_EMBED_URL__ : '';
    if (!embed || /^_{2}TB_/.test(embed))
      embed = new URLSearchParams(location.search).get('embed') || '';
    if (!embed) {
      // Fail LOUD. Returning quietly is what made a dead rig look like a failed experiment.
      put('netb', 'NO EMBED URL — the placeholder was never substituted');
      log('FATAL: the spike did not substitute the embed-URL placeholder, so no frame can ' +
          'load. Every cell below will read NO DATA. This is a harness bug, not an OWU result.');
      verdict('fail', 'HARNESS BROKEN BEFORE THE TEST STARTED: no embed URL was injected, so ' +
                      'neither panel was ever loaded. Nothing on this page is a measurement.');
      return;
    }
    if (/^["']/.test(embed)) {   // the double-quote regression, said out loud if it returns
      verdict('fail', 'HARNESS BUG: the injected embed URL is wrapped in stray quote ' +
                      'characters, so both frames request a nonexistent path.');
      log('FATAL: embed URL starts with a quote character: ' + embed.slice(0, 20));
      return;
    }
    // One URL for both panels, so the two frames differ ONLY in how they are mounted and
    // nothing else. The probe is appended for the spike only; api.h_embed refuses it on a
    // real mount.
    var probed = embed + (embed.indexOf('?') >= 0 ? '&' : '?') + 'probe=1';
    log('target: ' + embed);
    put('netb', 'A assigned, fetching for B…');
    frames.A.src = probed;
    fetch(probed, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' for the embed document');
      return r.text();
    }).then(function (doc) {
      frames.B.srcdoc = doc;
      put('netb', 'fetched ' + doc.length + ' bytes');
      log('embed document fetched (' + doc.length + ' bytes) → injected as srcdoc');
    }).catch(function (e) {
      put('netb', 'FAILED ' + e.message);
      verdict('fail', 'CANNOT FETCH EMBED: ' + e.message + ' — this is what the sandboxed ' +
                      'mount hits if CORS is not allowed for the api origin.');
    });
  })();
})();

