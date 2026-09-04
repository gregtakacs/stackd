"""OpenAI-compatible HTTP front + background supervisor. `stackd serve` is the
long-running daemon: one `Manager`, one lock, HTTP handler threads for `/v1/*`
and a control plane, plus a tick thread for health / crash-restart / idle-evict.

Only stdlib — `http.server` + `urllib`. Streaming (SSE) works by reading the
upstream response incrementally and flushing each chunk.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from stackd.engines.base import EngineState
from stackd.manager import Manager
from stackd.store import Store, _utc_day

_PROXY_READ_TIMEOUT_S = 600.0
_KEEPALIVE_EVERY_S = 15.0

_REGISTER_PAGE = """<!doctype html><meta charset=utf-8><title>stackd — register your key</title>
<style>body{font:15px system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem}
input,button{font:inherit;padding:.5rem;width:100%;box-sizing:border-box;margin:.3rem 0}
#m{margin-top:1rem;padding:.6rem;border-radius:6px;white-space:pre-wrap}</style>
<h2>Register your Open WebUI API key</h2>
<p>Settings → Account → API Keys in Open WebUI, then paste it here. This lets image
generations you request be saved back to <em>your</em> account.</p>
<input id=k type=password placeholder="sk-..." autocomplete=off>
<button onclick=go()>Register</button><div id=m></div>
<script>async function go(){let m=document.getElementById('m');m.textContent='…';
try{let r=await fetch('/register',{method:'POST',headers:{'content-type':'application/json'},
body:JSON.stringify({api_key:document.getElementById('k').value.trim()})});
let j=await r.json();m.style.background=r.ok?'#e6f4ea':'#fce8e6';
m.textContent=r.ok?('Registered as '+j.email):(j.error&&j.error.message||'failed');}
catch(e){m.style.background='#fce8e6';m.textContent=String(e);}}</script>"""


def _sample_gpu_watts() -> float:
    # via runner._nvidia_smi -> wrapped in coreutils `timeout` so a wedged driver
    # (D-state nvidia-smi that ignores SIGKILL) can't stall the ticker thread.
    from stackd.runner import _nvidia_smi
    out = _nvidia_smi(["--query-gpu=power.draw", "--format=csv,noheader,nounits"]) or ""
    try:
        return sum(float(x) for x in out.split() if x.replace(".", "").isdigit())
    except ValueError:
        return 0.0


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _apply_preset(body: dict, preset: dict) -> None:
    """Force-merge the served-entry preset into the request (client values lose —
    that is the point of a pinned variant). `chat_template_kwargs` deep-merges."""
    for key, val in preset.items():
        if key == "chat_template_kwargs" and isinstance(val, dict):
            body[key] = _deep_merge(dict(body.get(key) or {}), val)
        else:
            body[key] = val


class _Handler(BaseHTTPRequestHandler):
    server_version = "stackd/0.2"
    protocol_version = "HTTP/1.1"

    # injected by make_server
    mgr: Manager = None  # type: ignore[assignment]
    lock: threading.Lock = None  # type: ignore[assignment]
    api_key: str | None = None
    warm_wait_s: float = 120.0
    store: Store | None = None
    owu_base_url: str | None = None
    pricing_path: str | None = None
    cleaner = None  # stackd.cleaner.Cleaner | None

    def log_message(self, fmt, *args):  # quieter than the default stderr spew
        pass

    # -- helpers ------------------------------------------------------------------
    def _send_json(self, code: int, payload: dict, extra_headers: dict | None = None):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        if not self.api_key:
            return True
        return self.headers.get("authorization", "") == f"Bearer {self.api_key}"

    def _read_body(self) -> bytes:
        n = int(self.headers.get("content-length") or 0)
        return self.rfile.read(n) if n else b""

    def _admin(self) -> bool:
        return not self.api_key or self.headers.get("authorization", "") == f"Bearer {self.api_key}"

    # -- GET --------------------------------------------------------------------
    def do_GET(self):
        if self.path == "/health":
            return self._send_json(200, {"status": "ok"})
        if self.path == "/register":  # self-service bootstrap — no auth
            raw = _REGISTER_PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if self.path.startswith("/register/users"):
            return self._users_get()
        if not self._authed():
            return self._send_json(401, {"error": {"message": "unauthorized"}})
        if self.path.split("?")[0] == "/savings":
            return self._savings()
        if self.path == "/status":
            with self.lock:
                return self._send_json(200, self.mgr.status())
        if self.path in ("/v1/models", "/models"):
            with self.lock:
                cat = self.mgr.models_catalog()
            return self._send_json(200, {
                "object": "list",
                "data": [
                    {
                        "id": m["id"],
                        "object": "model",
                        "owned_by": "stackd",
                        "context_length": m["context_length"],
                        "stackd": {"owner_profile": m["owner_profile"], "ready": m["ready"]},
                    }
                    for m in cat
                ],
            })
        if self.path == "/capabilities":
            with self.lock:
                return self._send_json(200, self.mgr.capabilities())
        if self.path == "/image":
            with self.lock:
                return self._send_json(200, self.mgr.image_status())
        if self.path == "/engines":
            with self.lock:
                return self._send_json(200, {"engines": self.mgr.engines()})
        if self.path == "/cleaner":
            if self.cleaner is None:
                return self._send_json(503, {"error": {"message": "cleaner not running"}})
            return self._send_json(200, self.cleaner.status())
        if self.path == "/comfyui" or self.path.startswith("/comfyui/") or self.path.startswith("/comfyui?"):
            return self._do_comfyui()
        return self._send_json(404, {"error": {"message": f"no route for {self.path}"}})

    # -- POST -----------------------------------------------------------------------
    def do_POST(self):
        if self.path == "/register":  # self-service bootstrap — no auth
            return self._register_submit()
        if not self._authed():
            return self._send_json(401, {"error": {"message": "unauthorized"}})

        if self.path.startswith("/profiles/"):
            return self._control()

        if self.path == "/reload":
            return self._reload()

        if self.path in ("/image/model", "/image/capability"):
            return self._image_swap()

        if self.path in ("/cleaner/on", "/cleaner/off"):
            if self.cleaner is None:
                return self._send_json(503, {"error": {"message": "cleaner not running"}})
            self.cleaner.set_enabled(self.path.endswith("/on"))
            return self._send_json(200, self.cleaner.status())

        if self.path in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings"):
            return self._completions()

        if self.path == "/comfyui" or self.path.startswith("/comfyui/") or self.path.startswith("/comfyui?"):
            return self._do_comfyui()

        return self._send_json(404, {"error": {"message": f"no route for {self.path}"}})

    def do_DELETE(self):
        if self.path.startswith("/register/users/"):
            if not self._admin():
                return self._send_json(401, {"error": {"message": "unauthorized"}})
            email = self.path[len("/register/users/"):].split("/")[0]
            with self.lock:
                gone = self.store.delete_user(email) if self.store else False
            return self._send_json(200 if gone else 404, {"deleted": gone, "email": email})
        return self._send_json(404, {"error": {"message": f"no route for {self.path}"}})

    # -- user key registration ----------------------------------------------------
    def _register_submit(self):
        if self.store is None:
            return self._send_json(503, {"error": {"message": "no datastore"}})
        try:
            body = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"error": {"message": "invalid JSON"}})
        key = (body.get("api_key") or "").strip()
        if not key:
            return self._send_json(400, {"error": {"message": "missing 'api_key'"}})
        if not self.owu_base_url:
            return self._send_json(503, {"error": {"message": "OWU base URL not configured"}})
        # validate the key against Open WebUI and learn whose it is
        try:
            req = urllib.request.Request(
                self.owu_base_url.rstrip("/") + "/api/v1/auths/",
                headers={"authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                who = json.loads(r.read())
            email = (who.get("email") or "").strip().lower()
        except Exception as e:  # noqa: BLE001
            return self._send_json(400, {"error": {"message": f"key rejected by Open WebUI: {e}"}})
        if not email:
            return self._send_json(400, {"error": {"message": "Open WebUI returned no email"}})
        with self.lock:
            self.store.register_key(email, key)
        return self._send_json(200, {"email": email, "status": "registered"})

    def _users_get(self):
        if not self._admin():
            return self._send_json(401, {"error": {"message": "unauthorized"}})
        if self.store is None:
            return self._send_json(503, {"error": {"message": "no datastore"}})
        rest = self.path[len("/register/users"):].strip("/")
        with self.lock:
            if rest.endswith("/key"):
                email = rest[: -len("/key")]
                k = self.store.resolve_key(email)
                return self._send_json(200 if k else 404, {"email": email, "api_key": k})
            return self._send_json(200, {"users": self.store.list_users()})

    # -- savings ---------------------------------------------------------------------
    def _savings(self):
        if self.store is None:
            return self._send_json(503, {"error": {"message": "no datastore"}})
        from urllib.parse import parse_qs, urlparse
        from stackd.pricing import load_pricing, savings
        q = parse_qs(urlparse(self.path).query)
        with self.lock:
            out = savings(self.store, load_pricing(self.pricing_path),
                          from_day=(q.get("from") or [None])[0], to_day=(q.get("to") or [None])[0])
        return self._send_json(200, out)

    # -- ComfyUI passthrough ---------------------------------------------------------
    def _do_comfyui(self):
        tail = self.path[len("/comfyui"):] or "/"
        if not tail.startswith(("/", "?")):
            tail = "/" + tail
        if tail.startswith("?"):
            tail = "/" + tail
        with self.lock:
            endpoint, note = self.mgr.comfyui_target()
        if not endpoint:
            return self._send_json(
                503, {"error": {"message": note, "type": "no_image_engine"}},
                {"retry-after": "5"},
            )
        body = self._read_body() if self.command == "POST" else None
        self._relay(endpoint.rstrip("/") + tail, self.command, body, streaming=False)

    def _control(self):
        parts = self.path.strip("/").split("/")  # profiles/<name>/<verb>
        if len(parts) != 3:
            return self._send_json(404, {"error": {"message": "bad control path"}})
        _, name, verb = parts
        try:
            with self.lock:
                events = []
                if verb == "activate":
                    events = self.mgr.use(name)
                elif verb == "pin":
                    events = self.mgr.use(name)
                    self.mgr.pin()
                elif verb == "unpin":
                    self.mgr.unpin()
                elif verb == "evict":
                    events = self.mgr.evict()
                else:
                    return self._send_json(404, {"error": {"message": f"unknown verb {verb}"}})
                for e in events or []:
                    detail = f" — {e.detail}" if getattr(e, "detail", "") else ""
                    print(f"[{time.strftime('%H:%M:%S')}] {e.action} {e.stack}{detail} ({name})")
                return self._send_json(200, self.mgr.status())
        except Exception as e:  # noqa: BLE001
            return self._send_json(400, {"error": {"message": str(e)}})

    def _reload(self):
        """Re-read config/*.yaml + the bind-mounted .env, then re-converge the
        active profile. A bad edit -> 400, live config untouched."""
        with self.lock:
            try:
                changed = self.mgr.reload_config()
            except Exception as e:  # noqa: BLE001 — bad YAML / missing ${VAR}
                return self._send_json(400, {"error": {"message": f"reload rejected: {e}"}})
            try:
                events = self.mgr.use(self.mgr.state.active_profile, manual=True)
            except Exception as e:  # noqa: BLE001
                return self._send_json(
                    500, {"error": {"message": f"config reloaded but converge failed: {e}"},
                          "reloaded": changed})
        for ev in events or []:
            d = f" — {ev.detail}" if getattr(ev, "detail", "") else ""
            print(f"[{time.strftime('%H:%M:%S')}] {ev.action} {ev.stack}{d} (reload)")
        print(f"[{time.strftime('%H:%M:%S')}] config reload: {'; '.join(changed)}")
        return self._send_json(200, {
            "reloaded": changed,
            "events": [{"action": e.action, "stack": e.stack, "detail": getattr(e, "detail", "")}
                       for e in events or []],
            "status": self.mgr.status(),
        })

    def _image_swap(self):
        """POST /image/model {name} | /image/capability {need} — bring a different
        image model resident. 200 with the swap result (incl. `note` on a
        downgrade); 409 when nothing capable fits the headroom."""
        try:
            body = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"error": {"message": "invalid JSON body"}})
        with self.lock:
            if self.path.endswith("/model"):
                name = (body.get("name") or body.get("model") or "").strip()
                if not name:
                    return self._send_json(400, {"error": {"message": "body needs {\"name\": ...}"}})
                res = self.mgr.set_image(model=name)
            else:
                need = (body.get("need") or body.get("capability") or "").strip()
                if not need:
                    return self._send_json(400, {"error": {"message": "body needs {\"need\": ...}"}})
                res = self.mgr.set_image(need_capability=need)
        for line in res.get("events") or []:
            print(f"[{time.strftime('%H:%M:%S')}] {line} (image swap)")
        return self._send_json(200 if res.get("ok") else 409, res)

    def _completions(self):
        raw = self._read_body()
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"error": {"message": "invalid JSON body"}})
        model = body.get("model")
        if not model:
            return self._send_json(400, {"error": {"message": "missing 'model'"}})
        streaming = bool(body.get("stream"))

        with self.lock:
            rr = self.mgr.route(model)

        if rr.status == "unknown":
            return self._send_json(404, {"error": {"message": f"unknown model {model!r}"}})
        if rr.status == "outranked":
            return self._send_json(
                503, {"error": {"message": rr.note, "type": "outranked"}},
                {"retry-after": "10"},
            )

        rr = self._await_ready(rr, model, streaming)
        if rr is None:  # keepalive stream already closed
            return
        if rr.status != "ok" or not rr.endpoint:
            return self._send_json(
                503, {"error": {"message": rr.note or "model warming", "type": "warming"}},
                {"retry-after": "5"},
            )

        _apply_preset(body, rr.preset)
        if rr.served_model_name:
            body["model"] = rr.served_model_name

        def _on_body(buf: bytes, status: int) -> None:
            self._record_usage(model, rr, buf, status, embeddings=self.path.endswith("embeddings"))

        self._relay(rr.endpoint.rstrip("/") + self.path, "POST",
                    json.dumps(body).encode(), streaming=streaming, on_body=_on_body)

    # -- ledger ------------------------------------------------------------------------
    def _record_usage(self, requested_model, rr, buf: bytes, status: int, *, embeddings: bool):
        if self.store is None or embeddings:
            return
        usage = _extract_usage(buf)
        email = (self.headers.get("X-OpenWebUI-User-Email")
                 or self.headers.get("X-OpenWebUI-User-Id") or "direct")
        try:
            with self.lock:
                self.store.record_usage(
                    user_email=email, requested_model=requested_model,
                    served_stack=rr.stack, served_profile=rr.profile,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                    off_home=(rr.route_kind != "native"),
                    ok=(200 <= status < 300),
                )
        except Exception:  # noqa: BLE001 — ledger must never break a response
            pass

    # -- warm wait ----------------------------------------------------------------
    def _await_ready(self, rr, model, streaming):
        if rr.status == "ok":
            return rr
        deadline = time.time() + self.warm_wait_s
        started_sse = False
        last_ka = 0.0
        while time.time() < deadline:
            if streaming and not started_sse:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "keep-alive")
                self.end_headers()
                started_sse = True
            if streaming and time.time() - last_ka > _KEEPALIVE_EVERY_S:
                try:
                    self.wfile.write(b": stackd warming\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return None
                last_ka = time.time()
            time.sleep(1.0)
            with self.lock:
                rr = self.mgr.route(model)
            if rr.status == "ok" and rr.endpoint:
                if started_sse:
                    # can't reverse-proxy after committing our own 200; tell the
                    # client to resend (it will, immediately, and hit a warm stack)
                    self._sse_finish_retry()
                    return None
                return rr
        return rr

    def _sse_finish_retry(self):
        for line in (
            'data: {"error":{"message":"stack now ready — resend","type":"warmed"}}\n\n',
            "data: [DONE]\n\n",
        ):
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
            except OSError:
                return

    # -- reverse proxy ----------------------------------------------------------------
    def _relay(self, url: str, method: str, body: bytes | None, *, streaming: bool,
               on_body=None):
        """Forward one request upstream and relay the response. `streaming` chunk-
        relays a response with no content-length (SSE); otherwise buffers and sends
        with a real content-length. `on_body(bytes, status)` gets the full response
        for the ledger (bounded — only the first 256 KiB is kept)."""
        fwd_ct = self.headers.get("content-type", "application/json")
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"content-type": fwd_ct, "accept": self.headers.get("accept", "*/*")},
        )
        try:
            up = urllib.request.urlopen(req, timeout=_PROXY_READ_TIMEOUT_S)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("content-type", e.headers.get("content-type", "application/json"))
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            if on_body:
                on_body(payload, e.code)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return self._send_json(502, {"error": {"message": f"upstream unreachable: {e}"}})

        keep = bytearray()

        def _tee(chunk: bytes) -> None:
            if on_body and len(keep) < 262144:
                keep.extend(chunk[: 262144 - len(keep)])

        with up:
            ctype = up.headers.get("content-type", "application/octet-stream")
            clen = up.headers.get("content-length")
            self.send_response(up.status)
            self.send_header("content-type", ctype)
            if streaming and not clen:
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = up.read(8192)
                    if not chunk:
                        self.wfile.write(b"0\r\n\r\n")
                        break
                    _tee(chunk)
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
            else:
                data = up.read()
                _tee(data)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        if on_body:
            on_body(bytes(keep), up.status)


def _extract_usage(buf: bytes) -> dict:
    """Pull the `usage` block from a chat/completions response — JSON body, or the
    last `data:` line of an SSE stream (present when stream_options.include_usage)."""
    if not buf:
        return {}
    try:
        return json.loads(buf).get("usage") or {}
    except (json.JSONDecodeError, AttributeError):
        pass
    last = {}
    for line in buf.split(b"\n"):
        line = line.strip()
        if line.startswith(b"data:") and b'"usage"' in line:
            try:
                u = json.loads(line[5:].strip()).get("usage")
                if u:
                    last = u
            except json.JSONDecodeError:
                continue
    return last


def make_server(mgr: Manager, host: str, port: int, api_key: str | None,
                warm_wait_s: float = 120.0, *, store: Store | None = None,
                owu_base_url: str | None = None,
                pricing_path: str | None = None, cleaner=None) -> ThreadingHTTPServer:
    handler = type("_BoundHandler", (_Handler,), {
        "mgr": mgr, "lock": threading.Lock(), "api_key": api_key, "warm_wait_s": warm_wait_s,
        "store": store, "owu_base_url": owu_base_url, "pricing_path": pricing_path,
        "cleaner": cleaner,
    })
    return ThreadingHTTPServer((host, port), handler)


def serve(mgr: Manager, host: str, port: int, api_key: str | None,
          tick_interval: float = 20.0, warm_wait_s: float = 120.0, *,
          store: Store | None = None, owu_base_url: str | None = None,
          pricing_path: str | None = None, host_baseline_w: float = 90.0) -> None:
    from stackd.cleaner import Cleaner
    cc = mgr.cfg.runtime.comfyui_cleaner
    cleaner = Cleaner(cc.scratch_dir, file_ttl_min=cc.file_ttl_min,
                      interval_s=cc.interval_s, enabled=cc.enabled)

    httpd = make_server(mgr, host, port, api_key, warm_wait_s, store=store,
                        owu_base_url=owu_base_url, pricing_path=pricing_path,
                        cleaner=cleaner)
    lock = httpd.RequestHandlerClass.lock  # type: ignore[attr-defined]
    stop = threading.Event()
    last_energy = [time.time()]

    def _resident_comfy() -> str | None:
        with lock:
            img = mgr.capabilities().get("image")
        return img.get("endpoint") if img and img.get("serveable") else None

    def _ticker():
        while not stop.wait(tick_interval):
            try:
                with lock:
                    for e in mgr.tick():
                        detail = f" — {e.detail}" if getattr(e, "detail", "") else ""
                        print(f"[{time.strftime('%H:%M:%S')}] {e.action} {e.stack}{detail}")
                if store is not None:
                    now = time.time()
                    hrs = (now - last_energy[0]) / 3600.0
                    last_energy[0] = now
                    watts = _sample_gpu_watts()
                    with lock:
                        store.add_energy(_utc_day(now), gpu_wh=watts * hrs,
                                         host_wh=host_baseline_w * hrs)
                        store.rollup()
            except Exception as e:  # noqa: BLE001
                print(f"[tick] error: {e}")

    for _n in mgr.state.boot_reset():
        print(f"[boot] dropped stale/crashed stack {_n} — will respawn fresh")

    # converge the active (default on fresh state) profile before accepting traffic
    try:
        with lock:
            for e in mgr.use(mgr.state.active_profile, manual=True):
                print(f"[boot] {e.action} {e.stack}"
                      + (f" — {e.detail}" if getattr(e, "detail", "") else ""))
    except Exception as e:  # noqa: BLE001
        print(f"[boot] converge failed: {e}")

    t = threading.Thread(target=_ticker, daemon=True)
    t.start()

    # ComfyUI scratch janitor (was the comfyui-cleaner container). Always runs;
    # POST /cleaner/off parks it. Stdlib — no extra deps.
    def _clean_evt(swept, pruned):
        print(f"[{time.strftime('%H:%M:%S')}] cleaner swept {swept} file(s), pruned {pruned} history entr(y/ies)")
    threading.Thread(
        target=cleaner.run_forever, args=(_resident_comfy, stop),
        kwargs={"on_event": _clean_evt}, daemon=True,
    ).start()
    print(f"comfyui cleaner {'on' if cleaner.enabled else 'off'} "
          f"(ttl {cleaner.file_ttl_min}m, every {cleaner.interval_s}s, {cleaner.scratch_dir})")

    # Embedded image-generation MCP tools (was the standalone comfyui-mcp container).
    # Optional: only runs if stackd was installed with the `imagegen` extra. Lazy import
    # keeps `import stackd.serve` — and the stdlib-only smoke suite — free of mcp/httpx.
    try:
        from stackd.imagegen import config as _ig_config
        from stackd.imagegen.tools import start_mcp_server
        start_mcp_server(mgr, store, lock)
        print(f"stackd MCP image tools on http://{host}:{_ig_config.MCP_PORT}/mcp")
    except ImportError as e:
        print(f"[mcp] image tools unavailable ({e}) — install stackd[imagegen]")
    except Exception as e:  # noqa: BLE001 — never let the image server sink the daemon
        print(f"[mcp] image tools failed to start: {e}")

    led = "on" if store is not None else "off"
    print(f"stackd serving on http://{host}:{port}  (auth {'on' if api_key else 'off'}, ledger {led}), "
          f"active profile {mgr.state.active_profile}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.shutdown()
        print("\nstopped")
