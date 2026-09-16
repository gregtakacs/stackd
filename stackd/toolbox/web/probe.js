/* M0 spike probe — injected into the embed document by web.embed_document(probe=True)
 * when it is being loaded by the spike harness. It is NEVER included in a real mount
 * (the OWU Function and the lab page both ask for probe=False), which is what keeps
 * feasibility scaffolding from surviving into the product.
 *
 * It does not touch the editor. It counts what the browser actually delivers, so the
 * three pass criteria are read off the screen as numbers rather than eyeballed:
 *   1. does a drag reach the frame as pointer events at all, and do they land on the
 *      canvas (canvasSeen) instead of being eaten by a scroll container,
 *   2. does a painted mask export to a PNG carrying real coverage (self-test below),
 *   3. can the frame talk to its parent (this very postMessage) — the only channel the
 *      embed mount has.
 */
(function () {
  'use strict';
  var counts = { pointerdown: 0, pointermove: 0, pointerup: 0, touchstart: 0,
                 touchmove: 0, mousedown: 0, mousemove: 0, prevented: 0 };
  var lastType = null, canvasSeen = false;
  // Declared before post() so no call path can observe it while undefined.
  var fetches = { calls: 0, ok: 0, bad: 0, last: 'none', lastError: '' };

  function post(extra) {
    var de = document.documentElement;
    try {
      parent.postMessage(Object.assign({
        type: 'tb-probe', counts: counts, lastType: lastType, canvasSeen: canvasSeen,
        // innerH is CONTENT height; vh is the VIEWPORT we were given. Reporting both is
        // the only way to tell "the editor is genuinely tall" apart from "the host left
        // this frame too short and the user is stuck with an internal scrollbar" — one
        // number cannot distinguish them, and they demand opposite fixes.
        innerH: de.scrollHeight, vh: window.innerHeight, ch: de.clientHeight,
        overflowY: de.scrollHeight - window.innerHeight,
        // Both axes. The X pair is the whole point: a vertical scrollbar steals ~15px of
        // width, so an element laid out at exactly 100% overflows sideways and a horizontal
        // scrollbar appears that the height fitter never asked for. Reporting only Y let the
        // "internal scrollbar" cell honestly say "no (fits)" while a user looked at a
        // horizontal bar — an unfalsifiable check is not a check.
        innerW: de.scrollWidth, cw: de.clientWidth,
        overflowX: de.scrollWidth - window.innerWidth,

        // the network view, which is what proves a cross-origin POST got through the
        // sandbox at all rather than being silently blocked
        lastFetch: fetches.last, fetchCalls: fetches.calls, fetchOk: fetches.ok,
        fetchBad: fetches.bad, fetchError: fetches.lastError,
        dpr: window.devicePixelRatio || 1, vw: window.innerWidth
      }, extra || {}), '*');
    } catch (e) { /* parent is gone; nothing to report to */ }
  }

  function bump(ev) {
    var t = ev.type;
    if (counts[t] === undefined) counts[t] = 0;
    counts[t]++;
    lastType = t;
    if (((ev.target && ev.target.tagName) || '') === 'CANVAS') canvasSeen = true;
    if (ev.defaultPrevented) counts.prevented++;
    post();
  }

  /* Network instrumentation. The editor's req() calls the GLOBAL fetch at call time, so
   * wrapping it here (the probe is appended after toolbox.js) still observes every call.
   * Without this the "server round-trip" cells can never fill, and — far worse — an
   * un-filled cell was indistinguishable from "the sandbox blocked my request", which is
   * the single most important thing this spike exists to discriminate.
   *
   * It must not alter what the editor sees: return the original promise, inspect a CLONE,
   * and swallow every possible probe error so a broken instrument can never break a render.
   */
  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  if (realFetch) {
    window.fetch = function (input, init) {
      var url = typeof input === 'string' ? input : ((input && input.url) || String(input));
      var meth = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      fetches.calls++;
      var short = url.replace(/^https?:\/\/[^/]+/, '').slice(0, 40);
      return realFetch(input, init).then(function (res) {
        try {
          fetches.last = meth + ' ' + short + ' ' + res.status;
          fetches.lastError = '';
          if (res.ok) fetches.ok++; else fetches.bad++;
          if (res.ok && /\/toolbox\/(mask\/preview|jobs)/.test(short)) {
            res.clone().json().then(function (j) {
              var m = j && (j.mask || j);
              var bits = [];
              if (m && m.coverage_after !== undefined) bits.push('cov=' + m.coverage_after);
              else if (j && j.coverage !== undefined) bits.push('cov=' + j.coverage);
              if (j && j.empty !== undefined) bits.push('empty=' + j.empty);
              if (j && j.tiny !== undefined) bits.push('tiny=' + j.tiny);
              if (j && j.state) bits.push('state=' + j.state);
              if (j && j.error) bits.push('ERR');
              fetches.last = meth + ' ' + short + ' ' + res.status + ' ' + bits.join(' ');
              post();
            }, function () { post(); });
            return res;
          }
          post();
        } catch (e) { /* instrument must never break the call */ }
        return res;
      }, function (err) {
        try {
          fetches.bad++;
          fetches.last = meth + ' ' + short + ' FAILED';
          fetches.lastError = String((err && err.name) || err || 'error');
          post();
        } catch (e2) {}
        throw err;                      // the editor must still see its own rejection
      });
    };
  }

  ['pointerdown', 'pointermove', 'pointerup', 'mousedown', 'mousemove'].forEach(function (t) {
    window.addEventListener(t, bump, true);
  });
  ['touchstart', 'touchmove'].forEach(function (t) {
    window.addEventListener(t, bump, { capture: true, passive: true });
  });

  window.addEventListener('load', function () {
    post();
    // Export self-test, deliberately independent of the server: paint one known disc on
    // a known-size canvas and report the measured alpha count against the analytic
    // expectation, so a broken canvas->PNG path is caught HERE instead of being blamed
    // on the backend further down the line.
    setTimeout(function () {
      var out = { exportTest: 'skipped' };
      try {
        var c = document.createElement('canvas'); c.width = 64; c.height = 64;
        var x = c.getContext('2d');
        x.fillStyle = 'rgba(255,255,255,1)';
        x.beginPath(); x.arc(32, 32, 16, 0, 6.2832); x.fill();
        var d = c.toDataURL('image/png');
        var px = x.getImageData(0, 0, 64, 64).data, on = 0;
        for (var i = 3; i < px.length; i += 4) if (px[i] > 50) on++;
        out = { exportTest: 'ok', alphaOn: on, expectAlphaOn: Math.round(Math.PI * 16 * 16),
                dataUrlHead: d.slice(0, 22), dataUrlLen: d.length };
      } catch (e) {
        out = { exportTest: 'ERROR ' + e.message };
      }
      post(out);
    }, 1200);
  });
  setInterval(post, 2000);
})();
