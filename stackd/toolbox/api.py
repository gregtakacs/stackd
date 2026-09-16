"""
The `/toolbox/*` HTTP surface.

Stdlib only, and deliberately NOT a copy of serve.py's handler: this is a `dispatch()`
any host can delegate to — `stackd serve`'s `_Handler` will call it from its do_GET /
do_POST (M1), and `spike.py` runs it today on its own tiny server. Both hosts satisfy
one minimal duck type (see `_Http`), which is what keeps the spike exercising the real
code path instead of an imitation whose passing would prove nothing.

Auth model (see the package docstring): every state-touching route needs a launch token
and the token's `email` is who the job gets attributed to. The way `tokens.verify` is
called differs deliberately:

  * read routes (embed document, source image, health) verify with single_use=False —
    one editor session legitimately makes many of these and none change state;
  * the write route (`/toolbox/jobs` — the one that costs GPU time and writes files)
    redeems the token single_use, so a page cannot keep spawning jobs after its first
    submit. The created job then carries its own job-scope token for polling/cancel.

CORS: `*`, no credentials, and the editor never sends cookies. The routes a sandboxed
panel calls (/toolbox/mask/preview, /toolbox/jobs, /toolbox/mask/auto) are simple
text/plain POSTs needing no preflight, and /toolbox/embed + /toolbox/source.png are
plain GETs. That is exactly why the editor posts its JSON as text/plain: the
plain-stdlib front has no OPTIONS handler yet, so a preflight would make the embed mount
fail in a way that *looks* like a sandbox problem and isn't one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse

from stackd.toolbox import masks as _masks
from stackd.toolbox import tokens as _tokens
from stackd.toolbox import web as _web

VERSION = "0.1-m1"
# Reported by /toolbox/health so the running server states its own milestone, the same
# "make the debt/status visible" principle as redeem_on_create below. Bump when a milestone
# is human-verified, not when code is merely written. M1 code has LANDED (worker + real
# render seam mounted); the milestone string stays at M0-passed until a human verifies an
# end-to-end render on the live mounts, exactly the rule this line's comment sets.
MILESTONE = "M0-passed"
MAX_MASK_BYTES = 24 * 1024 * 1024      # a 4MP RGBA PNG is 2-6 MB; headroom, not an expected ceiling

MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_SIDE = 2048                        # mirrors workflows.MAX_SIDE; the editor caps its canvas the same way

# VISIBLE DEBT — now PAID. Job creation redeems the launch token single-use, so a leaked
# launch token cannot be replayed to mint unlimited GPU jobs (the point of the token per the
# package docstring). M0 left this False purely so the spike could render twice while the
# queue was a stub; with the M1 queue real (jobs.JobQueue + engine.comfy_render), creation
# costs real GPU time and it MUST be single-use. h_job_create reads it from exactly one
# place and /toolbox/health reports it so a regression back to False is visible.
REDEEM_ON_CREATE = True



def _parse_query(path: str) -> dict:
    q = path.split("?", 1)
    if len(q) < 2:
        return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(q[1]).items()}


def _as_int(value, default: int) -> int:
    """Query/form values arrive as strings; a UI field can arrive as null or ""."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class _Http:
    """The duck type a host handler must provide (serve.py's _Handler already does). Named
    as a class so the contract is checkable rather than folklore."""

    command: str
    path: str
    headers: dict

    def _send_json(self, code, payload, extra_headers=None): ...
    def _send_bytes(self, code, raw, ctype, cache_s=0, etag=None): ...
    def _read_body(self): ...


class Toolbox:
    """One instance per process, mounted by whoever serves HTTP.

    `secret`        — HMAC key for tokens (stackd's shared proxy secret)
    `spike_enabled` — whether /toolbox/spike and its dev-token fallback exist at all
    `source(email, ref) -> bytes | None`  — fetch the photo being edited
    `segmenter(bytes, text, threshold) -> (mask_png, info) | (None, err)`
                    — optional; wired to the ComfyUI CLIPSegMask path when resident
    `image_engine_up() -> bool`           — optional, for honest 503s
    `worker`        — an optional jobs.JobQueue. PRESENT -> /toolbox/jobs enqueues a real
                      render and /jobs/poll reads the store; ABSENT (the M0 spike) -> the
                      same routes keep their validate-and-echo stub behaviour. This is the
                      single switch the suite and the spike use to stay GPU-free while the
                      daemon passes a wired queue.
    `prewarm()`     — optional; called when an editor opens to bring an edit-capable model
                      resident (engine.prewarm_edit), so the first render isn't cold.
    """

    def __init__(self, *, secret: str = "", spike_enabled: bool = False, source=None,
                 segmenter=None, image_engine_up=None, logger=None, worker=None,
                 prewarm=None):
        self.secret = secret or ""
        self.spike_enabled = bool(spike_enabled)
        self._source = source
        self._segmenter = segmenter
        self._image_engine_up = image_engine_up
        self._worker = worker
        self._prewarm = prewarm
        self.log = logger
        # Dev-mode identity for the spike only (see h_spike).
        self.dev_email = ""
        # Which request header carries the forward-auth–verified email for /toolbox/launch.
        # Traefik's forward-auth plugin default is X-Auth-Request-User; some chains use
        # X-Auth-Request-Email. First non-empty wins. Configurable so the trust boundary is
        # explicit (see _launch_email) and matches whatever the lab router actually sets.
        self.launch_email_headers = ("x-auth-request-user", "x-auth-request-email",
                                     "x-forwarded-user")


    # ---------------- small helpers ----------------
    def _note(self, msg):
        if self.log:
            self.log.info("toolbox: %s", msg)

    def _cors(self):
        return {
            "access-control-allow-origin": "*",
            "access-control-allow-headers": "content-type, authorization",
            "access-control-allow-methods": "POST, GET, OPTIONS",
            "access-control-max-age": "600",
        }

    def _token_from(self, http, query):
        hdr = (http.headers.get("authorization") or "").strip()
        if hdr[:7].lower() == "bearer ":
            return hdr[7:].strip()
        return (query.get("token") or "").strip()

    def _identity(self, http, query, *, scope="launch", single_use=False,
                  job_id: str | None = None) -> str:
        """Verify the token and return its email. Raises TokenError -> 403 in dispatch.

        `scope` is not cosmetic: the editor polls with the job-scope token the create
        response handed it, so a poll that demanded a launch token would 403 every
        legitimate render. Job routes additionally pass the submitted job_id so a
        job token cannot address a DIFFERENT job."""
        payload = _tokens.verify(self.secret, self._token_from(http, query),
                                 scope=scope, single_use=single_use)
        if job_id is not None and payload.get("job_id") not in (None, job_id):
            raise _tokens.BadToken("that token is bound to a different job")
        return payload["email"]

    def _parse_json(self, raw: bytes) -> dict:
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError(f"body too large ({len(raw)} bytes)")
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise ValueError(f"body was not JSON ({e})")
        if not isinstance(obj, dict):
            raise ValueError("expected a JSON object")
        return obj

    def _json_body(self, http):
        # Read the stream EXACTLY once per request: BaseHTTPRequestHandler's rfile is
        # consumed by the first read, so a second _read_body() silently yields b"" and any
        # handler that "conveniently" re-reads its body works in tests (where the fake
        # returns the same bytes twice) and breaks against the real server.
        return self._parse_json(http._read_body() or b"")



    def _mask_from(self, body) -> bytes:
        """Decode the editor's base64 mask (what canvas.toDataURL yields, prefix
        stripped). It is never fetched from a caller-supplied URL: the same principle
        that keeps the *source* image out of the model's hands in imagegen/tools.py
        applies to the mask — an opaque payload we received, not an address we visit."""
        b64 = body.get("mask_png") or body.get("mask") or ""
        if isinstance(b64, bytes):
            data = b64
        else:
            text = str(b64).strip()
            if text.startswith("data:") or text[:100].count(","):
                text = text.split(",", 1)[-1]
            try:
                data = base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"mask was not valid base64 ({e})")
        if not data:
            raise ValueError("no mask was submitted")
        if len(data) > MAX_MASK_BYTES:
            raise ValueError(f"mask too large ({len(data)} bytes)")
        return data

    MAX_LAYERS = 40                      # a session with 40 objects is a mis-tap, not art

    def _layers_from(self, body):
        """Decode body["layers"] -> [{png, kind, grow, shrink, feather, erase}] | None.

        None means "not the layered path": an older editor (or the spike, or a hand-built
        request) still sends one flat mask_png, and that keeps working through
        masks.normalize(). Per-object geometry is only meaningful if the browser sends the
        objects, so this is additive rather than a breaking change to the contract.

        Numbers are clamped here, not trusted: these strings come from a browser inside an
        opaque-origin iframe, and masks._morph already caps its own radius -- the two
        guards have to agree or a client could request a 4000px MaxFilter and pin the CPU.
        """
        raw = body.get("layers")
        if not isinstance(raw, list) or not raw:
            return None
        if len(raw) > self.MAX_LAYERS:
            raise ValueError(f"too many mask layers ({len(raw)} > {self.MAX_LAYERS})")
        out = []
        for i, lay in enumerate(raw):
            if not isinstance(lay, dict):
                raise ValueError("mask layer %d was not an object" % i)
            png = self._mask_from({"mask_png": lay.get("png") or lay.get("mask_png")})
            kind = str(lay.get("kind") or "shape")
            if kind not in _masks.KIND_RULES:
                # An unknown kind must not silently inherit the brush rules (no threshold,
                # no feather) -- that would quietly change what gets painted. Fall back to
                # the strictest rule, which is the geometric one.
                kind = "shape"
            def num(key, hi):
                try:
                    return max(0, min(hi, int(float(lay.get(key) or 0))))
                except (TypeError, ValueError):
                    return 0
            # The signed one-axis offset, if this client speaks the new protocol. It is NOT
            # simply grow minus shrink: deriving it that way would fold the legacy two-field
            # form (where both set meant a morphological closing) into "no net change" and
            # silently re-render every saved mask that ever used both. `edge` present means
            # one axis was genuinely intended; absent means the old semantics, unchanged.
            has_edge = lay.get("edge") is not None
            try:
                edge = max(-_masks.MAX_MORPH_PX,
                           min(_masks.MAX_MORPH_PX, int(round(float(lay.get("edge") or 0)))))
            except (TypeError, ValueError):
                edge = 0
            grown = {"png": png, "kind": kind,
                     "grow": num("grow", _masks.MAX_MORPH_PX),
                     "shrink": num("shrink", _masks.MAX_MORPH_PX),
                     "feather": num("feather", _masks.MAX_MORPH_PX),
                     "erase": bool(lay.get("erase"))}
            if has_edge:
                grown["edge"] = edge
                # Keep the legacy pair consistent with the single axis, so a server-side
                # consumer that still reads grow/shrink sees the same geometry rather than
                # the zeros a one-axis client necessarily leaves behind.
                grown["grow"] = edge if edge > 0 else 0
                grown["shrink"] = -edge if edge < 0 else 0
            out.append(grown)
        if not out:
            raise ValueError("no usable mask layers were submitted")
        return out

    def _source_bytes(self, email, body, query) -> bytes:
        """Resolve the photo being edited. `image_id`/`source_ref` are opaque strings we
        handed the editor at launch — there is deliberately no URL field, for the reason
        documented all over imagegen/tools.py (a caller-supplied image reference is a
        footgun that has already fired once in this codebase)."""
        if self._source is None:
            raise ValueError("no source-image resolver is configured")
        ref = body.get("image_id") or body.get("source_ref") or query.get("image_id") or ""
        data = self._source(email, str(ref))
        if not data:
            raise ValueError("the source image could not be found for this user")
        return data

    def _dims(self, spec, source_bytes):
        """Working dims for the mask resample. The client's numbers are a REQUEST, never
        truth: masks.normalize() resamples to whatever the job actually submits to
        ComfyUI, so disagreement here costs quality, not correctness."""
        try:
            w, h = int(spec.get("width") or 0), int(spec.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        if w > 0 and h > 0:
            def q(v):   # workflows._round16: the latent grid the mask eventually feeds
                return max(64, min(4096, int(round(v / 16.0)) * 16))
            return q(w), q(h)
        if _masks.HAS_PIL:
            try:
                return _masks.image_size(source_bytes)
            except Exception:  # noqa: BLE001 — fall through to the safe default
                pass
        return 1024, 1024

    def _engine_up(self):
        if self._image_engine_up is None:
            return True
        try:
            return bool(self._image_engine_up())
        except Exception:  # noqa: BLE001
            return False

    # ---------------- dispatch ----------------
    def dispatch(self, http) -> bool:
        """True if this was a /toolbox/* route (and therefore handled). Never raises: a
        traceback escaping here would take down the daemon's SHARED HTTP front, not just
        this surface."""
        path = (getattr(http, "path", "") or "").split("?", 1)[0]
        if not path.startswith("/toolbox/"):
            return False
        query = _parse_query(getattr(http, "path", ""))
        try:
            self._route(http, path, query)
        except _tokens.TokenError as e:
            http._send_json(403, {"error": f"not authorised: {e}"}, self._cors())
        except ValueError as e:
            http._send_json(400, {"error": str(e)}, self._cors())
        except Exception as e:  # noqa: BLE001
            self._note(f"{path} failed: {e!r}")
            http._send_json(500, {"error": f"toolbox error: {e.__class__.__name__}: {e}"},
                            self._cors())
        return True

    POST_ONLY = ("/toolbox/echo", "/toolbox/mask/preview", "/toolbox/mask/auto",
                 "/toolbox/jobs", "/toolbox/jobs/poll", "/toolbox/jobs/cancel")
    GET_ONLY = ("/toolbox/health", "/toolbox/spike", "/toolbox/embed", "/toolbox/source.png",
                "/toolbox/launch")

    def _route(self, http, path, query):
        cmd = (getattr(http, "command", "GET") or "GET").upper()
        if cmd == "OPTIONS":
            http._send_json(200, {"ok": True}, self._cors())
        elif cmd == "GET":
            if path == "/toolbox/health":
                self.h_health(http)
            elif path == "/toolbox/spike":
                self.h_spike(http)
            elif path == "/toolbox/launch":
                self.h_launch(http, query)
            elif path == "/toolbox/embed":
                self.h_embed(http, query)
            elif path == "/toolbox/source.png":
                self.h_source(http, query)
            # A known route reached with the wrong verb is 405, not 404: "the endpoint
            # exists, you called it wrong" is the message that saves an hour when the
            # editor is misbehaving and you are reading server logs.
            elif path in self.POST_ONLY:
                http._send_json(405, {"error": f"{path} is POST-only"}, self._cors())
            else:
                http._send_json(404, {"error": f"no toolbox route {path}"}, self._cors())
        elif cmd == "POST":
            if path == "/toolbox/echo":
                self.h_echo(http)
            elif path == "/toolbox/mask/preview":
                self.h_mask_preview(http)
            elif path == "/toolbox/mask/auto":
                self.h_mask_auto(http)
            elif path == "/toolbox/jobs":
                self.h_job_create(http)
            elif path == "/toolbox/jobs/poll":
                self.h_job_poll(http)
            elif path == "/toolbox/jobs/cancel":
                self.h_job_cancel(http)
            elif path in self.GET_ONLY:
                http._send_json(405, {"error": f"{path} is GET-only"}, self._cors())
            else:
                http._send_json(404, {"error": f"no toolbox route {path}"}, self._cors())
        else:
            http._send_json(405, {"error": f"{path} does not serve {cmd}"}, self._cors())


    # ---------------- read handlers ----------------
    def _base_url(self, http) -> str:
        """Absolute base for the editor's fetch()es. Behind Traefik the Host/X-Forwarded-*
        headers are authoritative; on the LAN spike the raw Host header is what we have.
        Never taken from a query param — a UI that lets a caller redirect its own
        subsequent POSTs is a token-leak primitive."""
        host = (http.headers.get("x-forwarded-host") or http.headers.get("host")
                or "127.0.0.1:8191").strip()
        proto = (http.headers.get("x-forwarded-proto") or "http").split(",")[0].strip()
        if not self.spike_enabled and proto not in ("https",):
            proto = "https"          # production mount is TLS-terminated at Traefik
        return f"{proto}://{host}"

    def h_health(self, http):
        # No token: this route exists so the harness can tell "server down" apart from
        # "the sandbox blocked me", and it carries no data beyond feature flags.
        http._send_json(200, {
            "ok": True, "version": VERSION, "milestone": MILESTONE,
            "pillow": bool(_masks.HAS_PIL),
            "tokens": bool(self.secret),
            "spike": self.spike_enabled,
            "segmenter": self._segmenter is not None,
            "image_engine": self._engine_up(),
            "redeem_on_create": REDEEM_ON_CREATE,
            # M1: is a real render queue wired (vs. the M0 validate-and-echo stub)? The
            # spike runs without one; the daemon mounts it. Surfaced so a health poll can
            # tell "the daemon's GPU path is live" from "this is a stub server."
            "queue": self._worker is not None,
            "endpoints": ["/toolbox/embed", "/toolbox/source.png", "/toolbox/echo",
                          "/toolbox/mask/preview", "/toolbox/mask/auto",
                          "/toolbox/jobs", "/toolbox/jobs/poll", "/toolbox/jobs/cancel"],
        }, self._cors())

    def h_spike(self, http):
        if not self.spike_enabled:
            http._send_json(404, {"error": "the spike harness is disabled"}, self._cors())
            return
        email = self.dev_email or "spike@local"
        token = _tokens.mint(self.secret or "spike-dev-secret", scope="launch",
                             email=email, ttl_s=4 * 3600)
        html = _web.harness_document("/toolbox/embed?token=" + token,
                                     api=self._base_url(http), email=email,
                                     token_state="minted, 4h (spike mode)")
        http._send_bytes(200, html.encode("utf-8"), "text/html", cache_s=0)

    def h_launch(self, http, query):
        """The standalone `lab.<domain>` page's entry point: it is human-gated by Traefik's
        forward-auth chain, which sets a trusted email header on the request that reaches
        us. From that header we mint the one launch token this session is allowed — the
        browser NEVER sees the HMAC secret, only the minted token, and it can never choose
        whose email it is bound to. Deny-by-default: with no trusted header there is no
        identity to bind to, so we refuse rather than mint an unattributable token (the
        whole point of the token is that a job is owed to a real user for the OWU save).

        `/toolbox/embed` is then redirected to with the token + the image reference the
        launcher resolved, so the embed document itself always loads behind a token."""
        email = self._launch_email(http)
        if not email:
            # A launch reached without the oauth chain in front of it (direct :11444, a
            # router chain typo). Refuse loudly — minting on an arbitrary header here would
            # let anyone edit as anyone.
            http._send_json(403, {"error": "launch requires a verified identity from the "
                                           "forward-auth chain; the toolbox router must sit "
                                           "behind oauth",
                                   "reason": "no_trusted_identity"}, self._cors())
            return
        image_id = str(query.get("image_id") or query.get("source_ref") or "")
        # Bound to the caller's identity + the photo they were handed; nothing the browser
        # can later send widens this binding (see _identity's job_id cross-check).
        token = _tokens.mint(self.secret, scope="launch", email=email,
                             image_id=image_id or None)
        loc = "/toolbox/embed?token=" + urllib.parse.quote(token, safe="")
        if image_id:
            loc += "&image_id=" + urllib.parse.quote(image_id, safe="")
        http.send_response(302)
        http.send_header("location", loc)
        http.send_header("content-length", "0")
        http.send_header("cache-control", "no-store")   # a minted token is never cacheable
        http.end_headers()

    def _launch_email(self, http) -> str:
        """The trusted email for a launch: whichever forward-auth header is configured
        (Traefik's oauth plugin header by default). Empty == no verified identity. Kept in
        one place so the trust boundary — WHICH header is authoritative — is greppable."""
        for hdr in self.launch_email_headers:
            v = (http.headers.get(hdr) or "").strip()
            if v:
                return v
        return ""

    def h_embed(self, http, query):
        email = self._identity(http, query, single_use=False)
        # Warm the elastic image tier toward an edit-capable model the moment an editor
        # opens, so the (possibly first) render does not pay the model-swap cold start on
        # the user's clock. Fired on a daemon thread: request_capability takes the Manager
        # lock and a swap can run tens of seconds — doing it inline would stall this shared
        # HTTP handler (and with it the whole daemon front) for the duration. Best-effort;
        # a failed pre-warm is not a failed editor (the render itself re-checks the engine).
        if self._prewarm is not None:
            import threading
            threading.Thread(target=self._safe_prewarm, name="toolbox-prewarm",
                             daemon=True).start()
        # The M0 probe is reachable ONLY while the spike is explicitly enabled. The real
        # mounts (the OWU Function, the lab page) run against a daemon where
        # spike_enabled is False, so instrumentation cannot ship to users by someone
        # remembering `?probe=1` in a URL.
        probe = self.spike_enabled and (query.get("probe") or "") in ("1", "true", "yes")
        spec = {"width": 0, "height": 0}
        try:
            source = self._source_bytes(email, {}, query)
        except ValueError:
            # A mount without a resolvable photo is still a valid document to load: the
            # editor itself reports "no source image was handed to the editor", which is
            # the honest state rather than a 400 the harness would misread as CORS.
            source = b""
        w, h = self._dims(spec, source) if source else (1024, 1024)
        cfg = {
            "api": self._base_url(http),
            "token": self._token_from(http, query),
            "image": ("data:image/png;base64," + base64.b64encode(source).decode()) if source else None,
            "image_id": query.get("image_id") or query.get("source_ref") or "",
            "max_side": MAX_SIDE,
        }
        http._send_bytes(200, _web.embed_document(cfg, title="Comfy Toolbox",
                                                  probe=probe).encode("utf-8"),
                         "text/html", cache_s=0)

    def h_source(self, http, query):
        """The photo by URL, for the harness's same-origin panel A. The real in-chat mount
        never uses this route (its document carries a data: URI), which is exactly why the
        route is auth-checked the same way anyway."""
        email = self._identity(http, query, single_use=False)
        data = self._source_bytes(email, {}, query)
        http._send_bytes(200, data, "image/png", cache_s=60)

    # ---------------- transport probe ----------------
    def h_echo(self, http):
        """Proves a body got from the browser to us intact — the second M0 pass criterion,
        separated from mask *semantics* on purpose. If echo is green and preview is red,
        the failure is in the mask logic, not the transport."""
        email = self._identity(http, {}, single_use=False)
        body = self._json_body(http)
        data = self._mask_from(body)
        info = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16],
                "email": email}
        if _masks.HAS_PIL:
            try:
                info["size"] = list(_masks.image_size(data))
                c = _masks.coverage(data)
                info["coverage"] = round(c, 4) if c is not None else None
            except Exception as e:  # noqa: BLE001 — echo must not fail on a bad mask
                info["decode_error"] = str(e)
        http._send_json(200, {"ok": True, **info}, self._cors())

    # ---------------- mask handlers ----------------
    def h_mask_preview(self, http):
        """The 1-second answer to "did my mask land where I meant?". No GPU, no engine,
        no cost — which is the entire point: a bad brushstroke must never cost a
        13-170 s render to discover. Deliberately calls the SAME masks.normalize() the
        render will call, so what the user approves and what gets painted cannot
        disagree."""
        email = self._identity(http, {}, single_use=False)
        body = self._json_body(http)
        spec = body.get("spec") or body.get("params") or {}
        if not _masks.HAS_PIL:
            http._send_json(503, {"error": "pillow unavailable: the mask cannot be "
                                           "normalised or verified, refusing to submit "
                                           "blind"}, self._cors())
            return
        layers = self._layers_from(body)
        mask = None if layers else self._mask_from(body)
        try:
            source = self._source_bytes(email, body, {})
        except ValueError as e:
            source = None
            self._note(f"preview without source for {email}: {e}")
        w, h = self._dims(spec, source or mask or b"")
        if layers:
            # Per-object geometry: each object's own grow/shrink/feather, brush edges left
            # alone because hardness already chose them (see masks.KIND_RULES).
            canon, info = _masks.normalize_layers(layers, w, h, invert=bool(spec.get("invert")))
        else:
            canon, info = _masks.normalize(
                mask, w, h, expand=spec.get("mask_expand", 0),
                shrink=spec.get("mask_shrink", 0), feather=spec.get("mask_feather", 8),
                invert=bool(spec.get("invert")))
        # Judge the PAINT, not the blurred result: feather inflates the >threshold pixel
        # count, so a spot-heal must not read as "not tiny" just because the user set a
        # wide blend (see masks.normalize for the measurement).
        paint = info["coverage_paint"]
        empty = paint < _masks.USER_MASK_EMPTY_FLOOR
        tiny = (not empty) and paint < _masks.COVERAGE_FLOOR
        out = {"ok": True, "coverage": info["coverage_after"], "info": info,
               "empty": empty, "tiny": tiny, "email": email}
        if source:
            # canon, not the raw upload: canon is already resampled+feathered, so the
            # overlay is literally what the graph's mask will be.
            out["overlay_png"] = base64.b64encode(
                _masks.overlay(source, canon, width=w, height=h, feather=0)).decode()
        if empty:
            # Wording matters. For a USER-painted mask this almost always means the mask
            # did not line up (orientation, painted on a thumbnail, polarity) — NOT that
            # the description was vague, which is what the CLIPSeg path tells you.
            out["error"] = ("that mask selects nothing at all; the render would leave the "
                            "photo untouched. Check the mask has the same orientation and "
                            "roughly the same size as the photo, and that you painted the "
                            "area to CHANGE.")
        elif tiny:
            # Not an error — a pimple is small. Say so explicitly, so a 0.2% figure in
            # the status line is not mistaken for a failure by the user OR by a model.
            out["note"] = ("that is a small selection "
                           f"({paint*100:.2f}% of the frame painted) — fine for a "
                           "blemish or a spot, too small if you meant a whole object.")
        http._send_json(200, out, self._cors())

    def h_mask_auto(self, http):
        """Text -> mask via the graph's existing CLIPSegMask node. A convenience layer,
        never a trusted one: the guess comes back as an ordinary editable mask, and it
        fails LOUDLY when the engine is down or the segmentation found nothing — the two
        conditions imagegen already treats as hard errors rather than silent no-ops."""
        email = self._identity(http, {}, single_use=False)
        body = self._json_body(http)
        text = str(body.get("text") or "").strip()
        if not text:
            raise ValueError("no description was given to auto-mask on")
        if len(text) > 200:
            raise ValueError("auto-mask description is too long (200 chars max)")
        if self._segmenter is None:
            http._send_json(503, {"error": "no auto-masking engine is wired on this "
                                           "install — paint the mask instead (it needs no "
                                           "GPU)", "reason": "no_segmenter"}, self._cors())
            return
        if not self._engine_up():
            http._send_json(503, {"error": "the image engine is not resident right now, so "
                                           "auto-masking cannot run — paint the mask "
                                           "instead", "reason": "no_image_engine"},
                            self._cors())
            return
        source = self._source_bytes(email, body, {})
        res = self._segmenter(source, text, float(body.get("threshold") or 0.4))
        mask_png, info = res if isinstance(res, tuple) else (None, res)
        if not mask_png:
            msg = info.get("error") if isinstance(info, dict) else str(info)
            http._send_json(502, {"error": msg or "auto-masking failed",
                                  "reason": "segmentation_failed"}, self._cors())
            return
        cov = _masks.coverage(mask_png)
        out = {"ok": True, "mask_png": base64.b64encode(mask_png).decode(),
               "coverage": cov, "email": email}
        if cov is not None and cov < _masks.COVERAGE_FLOOR:
            out["empty"] = True
            out["warning"] = ('nothing matched "' + text + '" — try a shorter, concrete '
                              "visual description (one distinctive appearance feature, not "
                              "a pose), or just paint it")
        http._send_json(200, out, self._cors())


    # ---------------- job handlers ----------------
    def _safe_prewarm(self):
        """Never let an editor-open warm-up raise on its daemon thread (an unhandled
        exception there prints a traceback and, worse, is silent to the user)."""
        try:
            r = self._prewarm() if self._prewarm is not None else None
            if isinstance(r, dict) and r.get("ok") is False and self.log:
                self._note(f"prewarm reported failure: {r.get('error') or r}")
        except Exception as e:  # noqa: BLE001
            self._note(f"prewarm raised: {e!r}")

    def h_job_create(self, http):
        """Validates everything it can WITHOUT a GPU, then either enqueues a real render
        (worker mounted — the daemon) or echoes the round trip (no worker — the M0 spike).
        The response shape is identical either way, so the editor does not change when the
        queue lands; only `stub` flips. Validation runs BEFORE the token is redeemed, so a
        400 for a bad mask never burns the caller's single-use launch token."""
        email = self._identity(http, {}, single_use=False)
        body = self._json_body(http)
        spec = body.get("spec") or {}
        if not _masks.HAS_PIL:
            raise ValueError("pillow unavailable — cannot verify the mask before "
                             "submitting; refusing to render blind")
        # Same decoder, same normalizer as h_mask_preview: the mask the user approved in the
        # live preview and the mask submitted to ComfyUI are produced by one code path, so
        # they cannot drift apart. If this ever disagrees with the preview, fix it there.
        layers = self._layers_from(body)
        mask = None if layers else self._mask_from(body)
        source = self._source_bytes(email, body, {})
        w, h = self._dims(spec, source)
        if layers:
            canon, info = _masks.normalize_layers(layers, w, h,
                                                  invert=bool(spec.get("invert")))
        else:
            canon, info = _masks.normalize(
                mask, w, h, expand=spec.get("mask_expand", 0),
                shrink=spec.get("mask_shrink", 0), feather=spec.get("mask_feather", 8),
                invert=bool(spec.get("invert")))
        if info["coverage_paint"] < _masks.USER_MASK_EMPTY_FLOOR:
            # Empty, not merely small: a blemish brush is legitimately ~0.2%, and the
            # paint number ignores feather so a wide blend can't mask an empty stroke.
            raise ValueError("the mask selects nothing at all, so nothing would be "
                             "painted — paint the area to change, then Preview")
        job_id = hashlib.sha256(f"{email}|{time.time_ns()}|{w}|{h}".encode()).hexdigest()[:16]
        seed = _as_int(spec.get("seed"), -1)
        kind = str(spec.get("kind") or "edit")

        if self._worker is not None:
            # PAID THE DEBT: redeem the launch token single-use now that we KNOW we will
            # spend GPU time (every cheap validation above passed). A replayed launch token
            # gets 403 here, not a second render. Persist the row first (survives a crash
            # mid-enqueue), then hand the blobs to the worker.
            self._identity(http, {}, single_use=REDEEM_ON_CREATE)
            self._worker.store.create(email=email, kind=kind, w=w, h=h, spec=spec,
                                      mask_info=info, job_id=job_id)
            self._worker.enqueue(job_id, source=source, mask=canon)
            stub = False
            note = ""
        else:
            # The spike has no queue/GPU; validate-and-echo the contract unchanged.
            stub = True
            note = ("M0 stub: the mask was received, normalized and verified; the ComfyUI "
                    "render runs when a job queue is mounted (stackd serve).")
        http._send_json(200, {
            "ok": True, "job_id": job_id, "state": "queued", "seed": seed,
            "email": email, "mask": info, "working_size": [w, h],
            # Echoed whole. kind/prompt/strength/opacity/blend_mode/color_match/
            # preserve_detail/variants are the graph knobs; returning them verbatim means a
            # refactor that silently drops one shows up as a diff here (test_contract).
            "spec": spec,
            # The editor polls with THIS, not the launch token: the launch token is now
            # spent, and polling is a read that must not be able to redeem anything.
            "token": _tokens.mint(self.secret or "spike-dev-secret", scope="job",
                                  email=email, job_id=job_id),
            "stub": stub,
            "note": note,
        }, self._cors())

    def h_job_poll(self, http):
        """Read-only status from the JobStore. `state` is queued|running|done|error|
        cancelled; on done, artifacts = [{png, type}]. Polling is a pure read — it never
        redeems anything. Without a mounted queue (the spike) it stays the honest M0 stub
        rather than fabricating a render that never ran."""
        body = self._json_body(http)          # read once; see _json_body's note
        # Authenticate before validating: an unauthenticated caller gets 403, not a free
        # 400 that confirms which parameters exist.
        email = self._identity(http, {}, scope="job",
                               job_id=str(body.get("job_id") or ""))
        job_id = str(body.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        if self._worker is None:
            http._send_json(200, {"ok": True, "job_id": job_id, "email": email,
                                  "state": "error", "artifacts": [], "stub": True,
                                  "error_message": "M0 stub: no render queue is mounted"},
                            self._cors())
            return
        row = self._worker.store.get(job_id)
        if row is None or (row.get("email") or "") != email:
            # Two distinct failures collapsed to one deliberate lie: a job that does not
            # exist and one owned by someone else BOTH read as 404, so an attacker cannot
            # probe which job ids are live by watching 403-vs-404. The token is already
            # job_id-bound (see _identity), so reaching here at all for another user's job
            # would itself be a token bug.
            http._send_json(404, {"error": "no such job", "job_id": job_id}, self._cors())
            return
        out = {"ok": True, "job_id": job_id, "email": email, "state": row["state"]}
        try:
            seed = _as_int(json.loads(row.get("spec_json") or "{}").get("seed"), -1)
        except (ValueError, TypeError):
            seed = -1
        if seed >= 0:
            out["seed"] = seed
        if row["state"] == "done" and row.get("artifact_b64"):
            out["artifacts"] = [{"png": row["artifact_b64"],
                                 "type": row.get("artifact_type") or "image/png"}]
        else:
            out["artifacts"] = []
        if row["state"] == "error":
            out["error_message"] = row.get("error") or "render failed"
        # elapsed_s from the row timestamps, so the status line can say how long it took.
        try:
            if row.get("created_at") and row.get("updated_at"):
                out["elapsed_s"] = round(float(row["updated_at"]) - float(row["created_at"]), 1)
        except (TypeError, ValueError):
            pass
        http._send_json(200, out, self._cors())

    def h_job_cancel(self, http):
        """Interrupt a running (or drop a queued) render. Job-scope and job_id-bound, like
        poll — the editor cancels with the same token it polls with, never the (spent)
        launch token. Idempotent: cancelling a finished job is a no-op that reports the
        terminal state, not an error."""
        body = self._json_body(http)
        email = self._identity(http, {}, scope="job",
                               job_id=str(body.get("job_id") or ""))
        job_id = str(body.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        if self._worker is None:
            http._send_json(409, {"error": "no render queue is mounted on this server",
                                  "job_id": job_id}, self._cors())
            return
        row = self._worker.store.get(job_id)
        if row is None or (row.get("email") or "") != email:
            http._send_json(404, {"error": "no such job", "job_id": job_id}, self._cors())
            return
        state = self._worker.cancel(job_id)
        http._send_json(200, {"ok": True, "job_id": job_id, "email": email,
                              "state": state}, self._cors())



