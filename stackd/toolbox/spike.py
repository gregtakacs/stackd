"""
M0 feasibility spike — a standalone server for the editor, run by hand:

    cd LLM-Tools/stackd
    python3 -m stackd.toolbox.spike --port 8191 [--image /path/to/photo.jpg]

It exists to answer ONE question, expensively enough that M1 isn't built on a wrong mount
assumption: can a mask be *authored* at all under the conditions Open WebUI's rich-UI
embed imposes (sandboxed srcdoc iframe, opaque origin, no cookies, pointer/touch painting,
PNG export, cross-origin POST back)? Nothing here is production surface — but every
/toolbox/* call is served by the SAME stackd.toolbox.api.Toolbox.dispatch the daemon will
mount in M1, so a green spike is evidence about the real system rather than about a
throwaway imitation of it.

Stdlib only, like serve.py. Bound to 0.0.0.0 by default because a phone has to reach it
(phone-vs-desktop is itself one of the unknowns); the token it signs is bound to --email
and the only thing it can do is edit the one image you handed it.
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import pathlib
import secrets
import socket
import sys
import urllib.parse

from stackd.toolbox import api as _api

log = logging.getLogger("stackd.toolbox.spike")


def make_source(image_path: str | None):
    """The photo under test, served from disk.

    Without --image a synthetic photo is generated instead: the spike then still
    exercises the whole transport, and a high-contrast synthetic target makes a
    mis-resized mask obvious rather than subtle. It is deliberately NOT a flat colour —
    a flat image hides exactly the registration errors this module exists to catch."""
    path = pathlib.Path(image_path) if image_path else None
    if path is not None and not path.is_file():
        raise SystemExit(f"--image is not a file: {path}")

    def source(email: str, ref: str) -> bytes | None:
        if path is not None:
            try:
                return path.read_bytes()
            except OSError as e:
                log.warning("spike: cannot read %s: %s", path, e)
                return None
        from stackd.toolbox import masks as _m
        if not _m.HAS_PIL:
            return None
        from PIL import Image, ImageDraw
        import io
        img = Image.new("RGB", (1024, 768), (36, 58, 92))
        d = ImageDraw.Draw(img)
        d.ellipse((312, 184, 712, 584), fill=(212, 175, 145))       # the "face"
        d.ellipse((420, 320, 468, 356), fill=(40, 40, 50))          # "blemish" 1
        d.ellipse((556, 402, 596, 434), fill=(150, 60, 50))         # "blemish" 2
        d.rectangle((0, 640, 1024, 768), fill=(70, 96, 70))
        d.text((24, 16), "COMFY TOOLBOX SPIKE TARGET", fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    return source



class Handler(http.server.BaseHTTPRequestHandler):
    """Host shim. Its _send_json/_send_bytes/_read_body are deliberately the same shapes
    as serve.py's _Handler, so api.py cannot end up depending on a convenience only one
    of the two hosts happens to provide."""

    server_version = "stackd-toolbox-spike/0.1"
    protocol_version = "HTTP/1.1"
    toolbox: _api.Toolbox = None        # injected in main()

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send_json(self, code, payload, extra_headers=None):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _send_bytes(self, code, raw, ctype, cache_s=0, etag=None):
        self.send_response(code)
        if code != 304:
            charset = "; charset=utf-8" if ctype.startswith("text/") else ""
            self.send_header("content-type", ctype + charset)
            self.send_header("content-length", str(len(raw)))
        if cache_s < 0:
            self.send_header("cache-control", "no-cache")
        elif cache_s:
            self.send_header("cache-control", f"max-age={cache_s}")
        self.end_headers()
        if code != 304 and self.command != "HEAD":
            self.wfile.write(raw)

    def _read_body(self):
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _serve(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("location", "/toolbox/spike")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if path.startswith("/toolbox/") and self.toolbox.dispatch(self):
            return
        self._send_json(404, {"error": f"spike has no route {path}"})

    do_GET = do_POST = do_OPTIONS = do_HEAD = _serve


def lan_urls(port: int) -> list[str]:
    """The URL a phone on the wifi would actually open. Printing a guess and having it
    come up blank is precisely the false negative this spike must not produce.

    socket.gethostbyname_ex(gethostname()) is the obvious call and it is useless here:
    on this box the hostname resolves to 127.0.1.1, so filtering loopback leaves nothing
    and the spike prints only a localhost URL — which a phone cannot open, and the
    resulting blank page reads as "the sandbox is broken" when nothing is broken at all.
    So: ask the kernel which local address it would route off-box with (connect() on a
    UDP socket sends no packets), and keep loopback as the desktop fallback. Docker
    bridge addresses are deliberately NOT offered — a phone cannot reach them, and
    suggesting them manufactures a false negative.
    """
    ips = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))          # no traffic; just route selection
        ips.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    if not ips:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
    if "127.0.0.1" not in ips:
        ips.append("127.0.0.1")                 # desktop panel, always useful
    return [f"http://{ip}:{port}/toolbox/spike" for ip in ips]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m stackd.toolbox.spike",
                                 description="M0 feasibility spike for the Comfy Toolbox editor")
    ap.add_argument("--port", type=int, default=8191)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--image", default="", help="photo to edit (default: synthetic test photo)")
    ap.add_argument("--email", default="spike@local", help="identity the spike signs for")
    ap.add_argument("--secret", default="", help="token HMAC key (default: random per run)")
    ap.add_argument("--print-urls", action="store_true", help="print the LAN URLs and exit")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.print_urls:
        for u in lan_urls(args.port):
            print(u)
        return 0

    secret = args.secret or secrets.token_hex(16)
    tb = _api.Toolbox(secret=secret, spike_enabled=True,
                      source=make_source(args.image or None), logger=log)
    tb.dev_email = args.email
    Handler.toolbox = tb

    srv = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print("Comfy Toolbox — M0 spike")
    print(f"  pillow      : {_api._masks.HAS_PIL}")
    print(f"  identity    : {args.email}")
    print(f"  source      : {args.image or '(synthetic test photo)'}")
    print(f"  token key   : {'supplied' if args.secret else 'random for this run'}")
    print(f"  this machine: http://127.0.0.1:{args.port}/toolbox/spike")
    for u in lan_urls(args.port):
        print(f"  phone on wifi: {u}")
    print("\nThree things must go green, in both panels of the harness:")
    print("  1. a drag actually paints (events seen + canvas got events = YES)")
    print("  2. the mask arrives server-side with real coverage (Preview mask status line)")
    print("  3. the iframe reports its own height (height reported > 0)")
    print("Then Ctrl-C.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
