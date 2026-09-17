"""
Server-rendered documents + assets for the toolbox editor.

Why the editor's HTML/JS/CSS are generated here instead of being static files the
browser loads by URL: the in-chat mount hands Open WebUI a single `HTMLResponse` body,
which it renders into a sandboxed srcdoc iframe. A srcdoc document has an *opaque
origin* — a relative `<script src>` or `<link href>` in it resolves against nothing and
a cross-origin stylesheet/CSS import is one more thing to get wrong. So
`embed_document()` inlines everything into one self-contained blob and the browser's
only outbound calls are the explicit absolute fetch()es to the API. The same function
serves the standalone mount, which makes "works in chat" and "works full-screen" the
same code path rather than two implementations that drift.

`harness_document()` exists only for the M0 feasibility spike; see its docstring.
"""

from __future__ import annotations

import html
import json
import pathlib

_WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"
_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html",
          ".png": "image/png", ".svg": "image/svg+xml"}

# Memoised by (mtime_ns, size), NOT lru_cache, for the exact reason serve.py documents at
# its own copy of this logic: an lru_cache reads a file once per process, which is
# invisible while the image is immutable and fatal the moment a file changes under a
# running daemon (their dashboard fix "didn't work" for an hour this way). Iterating on
# the editor means editing these files under a live server, so re-stat every request.
_ASSETS: dict[str, tuple[int, int, bytes, str]] = {}


class UnknownAsset(Exception):
    pass


def asset(name: str) -> tuple[bytes, str]:
    """(bytes, content-type) for a file in stackd/toolbox/web/. `name` is a bare
    filename — separators and leading dots are refused outright, same traversal guard
    serve.py's _web_file uses."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise UnknownAsset(name)
    path = _WEB_DIR / name
    if not path.is_file():
        raise UnknownAsset(name)
    st = path.stat()
    key = str(path)
    hit = _ASSETS.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2], hit[3]
    raw = path.read_bytes()
    out = (st.st_mtime_ns, st.st_size, raw, _TYPES.get(path.suffix, "application/octet-stream"))
    _ASSETS[key] = out
    return raw, out[3]


def _inject(key: str, value: str, mapping: dict) -> str:
    for k, v in mapping.items():
        value = value.replace(k, v)
    return value


def _json_for_script(obj) -> str:
    """JSON safe to drop inside a <script> tag. The `</` escape is the load-bearing
    part: without it a title containing `</script>` would break out of the element. Case
    is not preserved by json.dumps for the escape below, so it is done on the dumped
    text rather than via a custom encoder."""
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def embed_document(cfg: dict, *, title: str = "Comfy Toolbox", probe: bool = False) -> str:
    """The complete, self-contained editor document.

    `cfg` is what the editor's JS reads as window.__TB__ (see web/toolbox.js's header for
    the field contract): api (absolute base of the /toolbox endpoints), token (launch
    HMAC token — a header on every call, never a cookie), image (a data: URI of the
    source photo, inlined server-side precisely so the opaque-origin iframe never needs
    the user's Open WebUI session to fetch it — AND already inside the render ceiling, so
    the iframe never has to downscale a photo the engine could not afford),
    source_ref/image_id (what to render), root, max_side (the ceiling itself, from
    masks.RENDER_MAX_SIDE), working_size, size_note.

    `probe=True` appends the M0 instrumentation (web/probe.js). It is reachable only from
    the spike route; a real mount must never ship it.
    """
    css, _ = asset("toolbox.css")
    js, _ = asset("toolbox.js")
    tail = ""
    if probe:
        pr, _ = asset("probe.js")
        tail = "<script>" + pr.decode("utf-8") + "</script>"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<style>{css.decode('utf-8')}</style>"
        f"<script>window.__TB__={_json_for_script(cfg)};</script>"
        f"<script>{js.decode('utf-8')}</script>"
        f"</head><body><div id='tb'></div>{tail}</body></html>"
    )



def harness_document(embed_url: str, *, api: str = "", email: str = "",
                     token_state: str = "") -> str:
    """The M0 feasibility page (see web/harness.html's comment for what it measures and
    why the two panels differ in exactly one variable).

    `embed_url` is fetched by the page itself, so the two panels are provably the same
    document: panel A loads it by URL (same-origin, unsandboxed control), panel B holds
    its bytes in a srcdoc under sandbox='allow-scripts' — Open WebUI's conditions.
    """
    shell, _ = asset("harness.html")
    script, _ = asset("harness.js")
    shell = shell.decode("utf-8") if isinstance(shell, bytes) else shell
    body = script.decode("utf-8").replace("__TB_EMBED_URL__", _json_for_script(embed_url))
    if "__TB_SCRIPT__" not in shell:
        # Fail at render time, not in someone's browser: a template that quietly stops
        # including the placeholder serves a page that looks fine and measures nothing.
        raise RuntimeError("harness.html lost its __TB_SCRIPT__ placeholder")
    # The controller is wrapped HERE, not in the template. That is deliberate: this line is
    # covered by smoke_toolbox.py, whereas tags living in a hand-editable HTML file regress
    # silently — losing them turned the entire controller into body text, so nothing
    # executed, no iframe ever received a src, and both panels rendered blank while every
    # substitution test in the suite still passed.
    out = shell.replace("__TB_SCRIPT__", "<script>\n" + body + "\n</script>")
    out = out.replace("__TB_API__", html.escape(api or "(none)"))
    out = out.replace("__TB_EMAIL__", html.escape(email or "(none)"))
    out = out.replace("__TB_TOKEN__", html.escape(token_state or "(none)"))
    return out

