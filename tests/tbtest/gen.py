"""Build a real-browser test document for the toolbox editor.

Uses the SHIPPED assets through the SHIPPED assembler (stackd.toolbox.web.embed_document),
so this exercises the same bytes the daemon serves into the iframe. Only two things are
substituted: window.fetch is stubbed (no daemon round-trip needed to test the client), and
a driver script drives real DOM PointerEvents. `mutate` exists so the same driver can be
pointed at a deliberately broken copy of the JS — a test that cannot fail on the old code
has not tested the fix.
"""
import base64, json, pathlib, struct, sys, zlib

ROOT = pathlib.Path(__file__).resolve().parents[2]   # .../stackd
sys.path.insert(0, str(ROOT))
from stackd.toolbox import web as TBWEB

WEB = ROOT / "stackd" / "toolbox" / "web"


def png_bytes(w, h):
    """An opaque PNG built from scratch (no PIL): a horizontal RGB gradient."""
    lines = []
    for _ in range(h):
        row = b"\x00"
        for x in range(w):
            row += bytes([0, 60 + (x * 180) // max(1, w - 1), 255 - (x * 180) // max(1, w - 1)])
        lines.append(row)
    data = zlib.compress(b"".join(lines), 9)

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", data)
            + chunk(b"IEND", b""))


SHIPPED_JS = (WEB / "toolbox.js").read_text()


def build(out_path, driver, *, mutate=None, cfg_extra=None, png=(160, 320), js=None,
          doc_mutate=None):
    """Assemble the test page. `driver` is appended at end of body: late enough that the
    editor has booted, early enough that it runs before any stubbed fetch could matter
    (the editor only ever calls fetch in response to a gesture)."""
    cfg = {"api": "http://stub.invalid", "token": "TESTTOKEN",
           "image": "data:image/png;base64," + base64.b64encode(png_bytes(*png)).decode(),
           "image_id": "/api/v1/files/test/content", "root": "tb", "max_side": 1024}
    cfg.update(cfg_extra or {})
    doc = TBWEB.embed_document(cfg)
    shipped = js if js is not None else SHIPPED_JS
    if mutate:
        shipped = mutate(shipped)
    if shipped != SHIPPED_JS:
        assert SHIPPED_JS in doc, "could not locate the inlined editor script"
        doc = doc.replace("<script>" + SHIPPED_JS + "</script>",
                          "<script>" + shipped + "</script>")
    doc = doc.replace("</body></html>", "<script>" + driver + "</script></body></html>")
    if doc_mutate:
        doc = doc_mutate(doc)
    pathlib.Path(out_path).write_text(doc)
    return out_path


