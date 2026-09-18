"""Diagnostic-only JS injection used by `run_tbtest.py --dbg`.

compose() is the suspect: the exported mask (maskC.toDataURL) demonstrably contains the
stroke, yet the composited view shows none of it. Rather than edit the shipped file to find
out why, mutate a COPY at build time -- the product code is never touched, and the probe
disappears when the flag does.
"""

PROBE = """
    try {
      var _dbg = { W: W, H: H, strokes: strokes.length,
                   active: active ? active.mode + ':' + active.pts.length : null,
                   hasPreview: !!(preview && preview.img),
                   sigMatch: !!(preview && preview.img && preview.sig === previewSig()),
                   wash: val('tb_wash', 0.45) };
      if (maskC) {
        var _md = maskC.getContext('2d').getImageData(0, 0, W, H).data;
        _dbg.maskPx = 0;
        for (var _i = 3; _i < _md.length; _i += 4) if (_md[_i] > 8) _dbg.maskPx++;
      }
      window.__DBG = window.__DBG || [];
      if (window.__DBG.length < 40) window.__DBG.push(_dbg);
    } catch (e) { window.__DBGERR = String(e); }
"""


def inject(js):
    anchor = "  function compose() {\n"
    assert anchor in js, "compose() anchor not found -- the editor changed shape"
    return js.replace(anchor, anchor + PROBE, 1)
