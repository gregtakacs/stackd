"""OpenAI-compatible HTTP front + background supervisor. `stackd serve` is the
long-running daemon: one `Manager`, one lock, HTTP handler threads for `/v1/*`
and a control plane, plus a tick thread for health / crash-restart / idle-evict.

Only stdlib — `http.server` + `urllib`. Streaming (SSE) works by reading the
upstream response incrementally and flushing each chunk.
"""

from __future__ import annotations

import datetime
import functools
import json
import os
import pathlib
import signal
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from stackd import events
from stackd.engines.base import EngineState
from stackd.manager import Manager
from stackd.promptcache import PromptCacheModel, pc_units
from stackd.store import Store, _utc_day

_WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"
_STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}


@functools.lru_cache(maxsize=16)
def _web_file(name: str) -> tuple[bytes, str] | None:
    """Read a bundled web asset once. `name` is a bare filename (no path
    separators); dashboard.html sits in web/, vendored libs in web/vendor/."""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    for cand in (_WEB_DIR / name, _WEB_DIR / "vendor" / name):
        if cand.is_file():
            ctype = _STATIC_TYPES.get(cand.suffix, "application/octet-stream")
            return cand.read_bytes(), ctype
    return None


def _gpu_stats() -> dict:
    try:
        from stackd.telemetry import gpu_stats
        return gpu_stats()
    except Exception as e:  # noqa: BLE001 — telemetry is best-effort
        return {"cuda0": None, "igpu0": None, "error": str(e)}


def _host_stats() -> dict:
    try:
        from stackd.telemetry import host_stats
        return host_stats()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}

_PROXY_READ_TIMEOUT_S = 600.0
_KEEPALIVE_EVERY_S = 15.0

_REGISTER_FALLBACK = (
    b"<!doctype html><meta charset=utf-8><title>stackd \xe2\x80\x94 register</title>"
    b"<p>register page asset missing</p>"
)


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

    def _send_bytes(self, code: int, raw: bytes, ctype: str, cache_s: int = 0):
        self.send_response(code)
        self.send_header("content-type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("content-length", str(len(raw)))
        if cache_s:
            self.send_header("cache-control", f"max-age={cache_s}")
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
            hit = _web_file("register.html")
            raw = hit[0] if hit else _REGISTER_FALLBACK
            return self._send_bytes(200, raw, "text/html")
        if self.path.startswith("/register/users"):
            return self._users_get()
        # --- web UI: the shell + vendored assets are unauthenticated (no secrets;
        #     every data endpoint below still checks the bearer). ---
        if self.path in ("/", "/dashboard", "/ui"):
            hit = _web_file("dashboard.html")
            if not hit:
                return self._send_json(404, {"error": {"message": "dashboard not bundled"}})
            return self._send_bytes(200, hit[0], hit[1])
        if self.path.startswith("/static/"):
            hit = _web_file(self.path[len("/static/"):].split("?")[0])
            if not hit:
                return self._send_json(404, {"error": {"message": "no such asset"}})
            return self._send_bytes(200, hit[0], hit[1], cache_s=86400)
        if not self._authed():
            return self._send_json(401, {"error": {"message": "unauthorized"}})
        if self.path.split("?")[0] == "/savings":
            return self._savings()
        if self.path.split("?")[0] == "/savings/facts":
            return self._savings_facts()
        if self.path.split("?")[0] == "/events":
            return self._events()
        if self.path == "/gpu":
            return self._send_json(200, _gpu_stats())
        if self.path == "/host":
            return self._send_json(200, _host_stats())
        if self.path.split("?")[0] == "/history":
            return self._history()
        if self.path == "/profiles":
            return self._profiles_list()
        if self.path.startswith("/engine/"):
            return self._engine(self.path[len("/engine/"):].split("?")[0])
        if self.path.startswith("/slots/"):
            return self._slots(self.path[len("/slots/"):].split("?")[0])
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
                        "stackd": {"owner_profile": m["owner_profile"], "ready": m["ready"],
                                   "native": m.get("native", True),
                                   "standin": m.get("standin", False)},
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

        if self.path == "/shutdown":
            return self._shutdown()

        if self.path == "/savings/refresh":
            return self._savings_refresh()

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

    def _savings_facts(self):
        """Finest-grain priced fact table for the dashboard's drill-down explorer.
        `?from=YYYY-MM-DD&to=YYYY-MM-DD` — same range params as `/savings`.

        Each fact carries both `served_stack` (the stackd unit, e.g. `coding`) and
        `served_model` (the checkpoint on disk, e.g. `Qwen3.8-Flash-Next-NVFP4`),
        straight from the ledger — no config-time remap."""
        if self.store is None:
            return self._send_json(503, {"error": {"message": "no datastore"}})
        from urllib.parse import parse_qs, urlparse
        from stackd.pricing import load_pricing, savings_facts
        q = parse_qs(urlparse(self.path).query)
        with self.lock:
            out = savings_facts(self.store, load_pricing(self.pricing_path),
                                from_day=(q.get("from") or [None])[0],
                                to_day=(q.get("to") or [None])[0])
        return self._send_json(200, out)

    def _savings_refresh(self):
        """Pull current tier-ref prices from OpenRouter (writes a new effective-
        dated point only on change). Mirrors `stackctl prices`."""
        if self.store is None:
            return self._send_json(503, {"error": {"message": "no datastore"}})
        from stackd.pricing import load_pricing, refresh_openrouter
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        try:
            with self.lock:
                n = refresh_openrouter(self.store, load_pricing(self.pricing_path), today=today)
        except Exception as e:  # noqa: BLE001 — network / parse
            return self._send_json(502, {"error": {"message": f"OpenRouter refresh failed: {e}"}})
        return self._send_json(200, {"updated": n, "last_openrouter_fetch": today})

    # -- web UI data endpoints -----------------------------------------------------
    def _events(self):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        try:
            since = int((q.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        return self._send_json(200, events.snapshot(since_seq=since or None))

    def _history(self):
        """Per-day arrays for the dashboard charts: requests + tokens by profile,
        energy, and net savings. `?from=YYYY-MM-DD&to=YYYY-MM-DD` for an explicit
        range, else `?days=N` (default 14). Empty (not an error) with no ledger."""
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        today = datetime.datetime.now(datetime.timezone.utc).date()  # match store._utc_day
        _iso = lambda s: datetime.date.fromisoformat(s)
        try:
            d_from = _iso((q.get("from") or [""])[0]) if q.get("from") else None
            d_to = _iso((q.get("to") or [""])[0]) if q.get("to") else None
        except ValueError:
            d_from = d_to = None
        if d_from:
            d_to = d_to or today
            if d_to < d_from:
                d_from, d_to = d_to, d_from
            n = min(400, (d_to - d_from).days + 1)
            span = [(d_from + datetime.timedelta(days=i)).isoformat() for i in range(n)]
        else:
            try:
                days = max(1, min(120, int((q.get("days") or ["14"])[0])))
            except ValueError:
                days = 14
            span = [(today - datetime.timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]
        days = len(span)
        out = {"days": span, "requests": {}, "tokens_in": {}, "tokens_out": {},
               "tokens_cached": {}, "energy": {"gpu_kwh": [0.0] * days, "host_kwh": [0.0] * days},
               "savings": {"net_usd": [0.0] * days},
               # per-day token-weighted throughput + context size
               "throughput": {"decode_tps": [0.0] * days, "prefill_tps": [0.0] * days,
                              "ctx_avg": [0] * days, "ctx_max": [0] * days}}
        if self.store is None:
            return self._send_json(200, out)
        idx = {d: i for i, d in enumerate(span)}
        with self.lock:
            rows = self.store.usage_rows(span[0], span[-1])
            erows = self.store.energy_rows(span[0], span[-1])
            from stackd.pricing import load_pricing, savings
            pricing = load_pricing(self.pricing_path)
            per_day = {d: savings(self.store, pricing, from_day=d, to_day=d) for d in span}
        acc = [dict(pfm=0, pft=0, dcm=0, dct=0, cxs=0, cxm=0, cxn=0) for _ in span]
        for r in rows:
            i = idx.get(r["day"])
            if i is None:
                continue
            prof = r.get("served_profile") or "?"
            for key, col in (("reqs", "requests"), ("prompt_tokens", "tokens_in"),
                             ("completion_tokens", "tokens_out"), ("cached_tokens", "tokens_cached")):
                series = out[col].setdefault(prof, [0] * days)
                series[i] += r.get(key) or 0
            a = acc[i]
            a["pfm"] += r.get("prefill_ms") or 0;  a["pft"] += r.get("prefill_tok") or 0
            a["dcm"] += r.get("decode_ms") or 0;   a["dct"] += r.get("decode_tok") or 0
            a["cxs"] += r.get("ctx_tokens_sum") or 0
            a["cxm"] = max(a["cxm"], r.get("ctx_tokens_max") or 0)
            a["cxn"] += r.get("ctx_n") or 0
        tp = out["throughput"]
        for i, a in enumerate(acc):
            tp["decode_tps"][i] = round(a["dct"] * 1000 / a["dcm"], 1) if a["dcm"] else 0.0
            tp["prefill_tps"][i] = round(a["pft"] * 1000 / a["pfm"], 1) if a["pfm"] else 0.0
            tp["ctx_avg"][i] = round(a["cxs"] / a["cxn"]) if a["cxn"] else 0
            tp["ctx_max"][i] = a["cxm"]
        for r in erows:
            i = idx.get(r["day"])
            if i is not None:
                out["energy"]["gpu_kwh"][i] = round((r["gpu_wh"] or 0) / 1000.0, 4)
                out["energy"]["host_kwh"][i] = round((r["host_wh"] or 0) / 1000.0, 4)
        # cumulative-savings line = the `payback_tier` from pricing.json (the tier
        # the payback % is measured against), else the last-configured tier.
        sample = per_day.get(span[-1]) or per_day.get(span[0]) or {}
        pay_label = sample.get("payback_tier")
        out["savings_tier"] = pay_label or ((sample.get("tiers") or [{}])[-1].get("label", ""))
        for d, i in idx.items():
            tiers = per_day[d].get("tiers") or []
            blk = next((x for x in tiers if x["label"] == pay_label), None) or (tiers[-1] if tiers else None)
            out["savings"]["net_usd"][i] = round((blk["net"] if blk else 0.0), 4)
        return self._send_json(200, out)

    def _profiles_list(self):
        from stackd.planner import plan_transition
        from stackd.validator import validate_profile
        with self.lock:
            cfg, cat = self.mgr.cfg, self.mgr.catalog
            active = self.mgr.state.active_profile
            pinned = self.mgr.state.pinned
            run_state = {n: rt.state.value for n, rt in self.mgr.state.stacks.items()}
            running = set(run_state)
            img = self.mgr.state.image
            if img is not None:
                run_state[f"image:{img.active_model}"] = img.state.value
            out = []
            for name, pr in sorted(cfg.profiles.items(), key=lambda kv: -kv[1].priority):
                try:
                    rep = validate_profile(cfg, name, cat)
                except Exception as e:  # noqa: BLE001
                    out.append({"name": name, "error": str(e)})
                    continue
                would_evict = []
                if name != active:
                    try:
                        would_evict = plan_transition(cfg, active, name, cat).teardown
                    except Exception:  # noqa: BLE001
                        would_evict = []
                out.append({
                    "name": name, "priority": pr.priority, "default": pr.default,
                    "manual_only": pr.manual_only,
                    "idle_evict": pr.idle_evict, "active": name == active,
                    "pinned": pinned and name == active, "models": list(pr.models),
                    "fits": rep.ok, "unplaced": rep.unplaced, "flags": rep.flags,
                    "placement": rep.placement, "resident": [m for m in rep.resident if m in running],
                    "states": {m: run_state.get(m) for m in pr.models},  # ready|warming|... per model
                    "would_evict": would_evict,
                })
        converging = any(s == "warming" for s in run_state.values())
        return self._send_json(200, {"active": active, "pinned": pinned,
                                     "converging": converging, "profiles": out})

    def _engine(self, stack: str):
        """Normalised live telemetry for one running engine (llama.cpp /slots +
        /metrics, vLLM + SGLang /metrics) — tok/s, KV %, in-flight, MTP acceptance."""
        with self.lock:
            rt = self.mgr.state.stacks.get(stack)
            endpoint = rt.endpoint if rt else None
            m = self.mgr.cfg.models.get(stack)
            tmpl = m.engine.template if m else ""
            params = dict(m.engine.params) if m else {}
            cmd_extra = list(getattr(getattr(m.engine, "container", None), "cmd_extra", []) or []) if m else []

        def _cli_val(flag):  # pull "--flag N" out of a raw cmd_extra list
            try:
                return cmd_extra[cmd_extra.index(flag) + 1]
            except (ValueError, IndexError):
                return None
        if rt is None:
            return self._send_json(404, {"error": {"message": f"no running stack {stack!r}"}})
        if not endpoint:
            return self._send_json(503, {"error": {"message": "no endpoint yet"}})
        from stackd.telemetry import engine_telemetry
        tel = engine_telemetry(endpoint, tmpl)
        if tel.get("slots") is None:   # vLLM/SGLang have no /slots — take the configured seq cap
            cap = (params.get("max_num_seqs") or params.get("parallel")
                   or _cli_val("--max-num-seqs") or _cli_val("--max-running-requests"))
            tel["slots"] = int(cap) if cap else None
        if tel.get("ctx_max") is None:  # no ctx ceiling in /metrics — use the configured one
            ml = (params.get("max_model_len") or params.get("ctx")
                  or _cli_val("--max-model-len") or _cli_val("--context-length"))
            tel["ctx_max"] = int(ml) if ml else None
        hint = _LAST_GEN.get(stack)
        if hint:
            tel["last_request"] = hint
            if not tel.get("active"):
                # idle -> the last request's own llama.cpp `timings` are the
                # authoritative "last session" numbers (the poll-based tracking
                # is only a fallback, e.g. for vLLM).
                for k in ("gen_tok_s", "prompt_tok_s", "mtp_accept_pct"):
                    if hint.get(k) is not None:
                        tel[k] = hint[k]
                        tel[k + "_session"] = hint[k]
                hc = hint.get("ctx_tokens") or 0
                if hc and hc > (tel.get("ctx_tokens") or 0):
                    tel["ctx_tokens"] = hc
                    if tel.get("ctx_max"):
                        tel["kv_pct"] = round(100.0 * hc / tel["ctx_max"], 1)
        return self._send_json(200, tel)

    def _slots(self, stack: str):
        """Reverse-proxy a llama.cpp engine's /slots for the live engine card.
        vLLM has no equivalent -> {"unsupported": true}."""
        with self.lock:
            rt = self.mgr.state.stacks.get(stack)
            endpoint = rt.endpoint if rt else None
            tmpl = ""
            m = self.mgr.cfg.models.get(stack)
            if m:
                tmpl = m.engine.template
        if rt is None:
            return self._send_json(404, {"error": {"message": f"no running stack {stack!r}"}})
        if not tmpl.startswith("llamacpp"):
            return self._send_json(200, {"unsupported": True, "template": tmpl})
        if not endpoint:
            return self._send_json(503, {"error": {"message": "no endpoint yet"}})
        try:
            req = urllib.request.Request(endpoint.rstrip("/") + "/slots")
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read()
            self._send_bytes(200, body, "application/json")
        except Exception as e:  # noqa: BLE001
            self._send_json(502, {"error": {"message": f"slots unreachable: {e}"}})

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
                evs = []
                if verb == "activate":
                    evs = self.mgr.use(name)
                elif verb == "pin":
                    evs = self.mgr.use(name)
                    self.mgr.pin()
                elif verb == "unpin":
                    self.mgr.unpin()
                elif verb == "evict":
                    evs = self.mgr.evict()
                else:
                    return self._send_json(404, {"error": {"message": f"unknown verb {verb}"}})
                for e in evs or []:
                    detail = f" — {e.detail}" if getattr(e, "detail", "") else ""
                    print(f"[{time.strftime('%H:%M:%S')}] {e.action} {e.stack}{detail} ({name})")
                    events.record_evt(e, source=f"control:{verb}")
                if not evs:
                    events.record(verb, name, source="control")
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
                evs = self.mgr.use(self.mgr.state.active_profile, manual=True)
            except Exception as e:  # noqa: BLE001
                return self._send_json(
                    500, {"error": {"message": f"config reloaded but converge failed: {e}"},
                          "reloaded": changed})
        for ev in evs or []:
            d = f" — {ev.detail}" if getattr(ev, "detail", "") else ""
            print(f"[{time.strftime('%H:%M:%S')}] {ev.action} {ev.stack}{d} (reload)")
            events.record_evt(ev, source="reload")
        print(f"[{time.strftime('%H:%M:%S')}] config reload: {'; '.join(changed)}")
        events.record("reload", "", "; ".join(changed), source="reload")
        return self._send_json(200, {
            "reloaded": changed,
            "events": [{"action": e.action, "stack": e.stack, "detail": getattr(e, "detail", "")}
                       for e in evs or []],
            "status": self.mgr.status(),
        })

    def _shutdown(self):
        """POST /shutdown — tear down every engine stackd spawned (LLM stacks +
        the elastic image tier) through the Docker socket, then stop the daemon.
        For a clean whole-stack stop: nothing stackd created is left orphaned
        holding VRAM. `stackctl down` is the CLI for this."""
        with self.lock:
            removed = self.mgr.shutdown_engines()
        for n in removed:
            print(f"[{time.strftime('%H:%M:%S')}] teardown {n} (shutdown)")
        events.record("shutdown", "", f"removed {', '.join(removed) or 'nothing'}", source="shutdown")
        self._send_json(200, {"stopped": removed, "note": "daemon exiting"})
        # serve_forever() only returns from another thread; do it after the
        # response has flushed so `stackctl down` gets its 200.
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()

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
                backend = (body.get("backend") or "").strip() or None
                unsafe = bool(body.get("unsafe"))
                if unsafe and not backend:
                    return self._send_json(400, {"error": {
                        "message": "\"unsafe\": true requires a \"backend\" — an unsafe load must "
                                   "target a specific, deliberate device, never 'wherever fits'"}})
                res = self.mgr.set_image(model=name, backend=backend, unsafe=unsafe)
            else:
                need = (body.get("need") or body.get("capability") or "").strip()
                if not need:
                    return self._send_json(400, {"error": {"message": "body needs {\"need\": ...}"}})
                res = self.mgr.set_image(need_capability=need)
        for line in res.get("events") or []:
            print(f"[{time.strftime('%H:%M:%S')}] {line} (image swap)")
            events.record("image-swap", f"image:{res.get('active_model') or '?'}", str(line), source="image")
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

        is_embeddings = self.path.endswith("embeddings")

        def _on_body(buf: bytes, status: int, meta: dict | None = None) -> None:
            self._record_usage(model, rr, buf, status, embeddings=is_embeddings,
                               req_body=body, meta=meta)
            _capture_gen(rr.stack, buf)

        self._relay(rr.endpoint.rstrip("/") + self.path, "POST",
                    json.dumps(body).encode(), streaming=streaming, on_body=_on_body,
                    inject_timings=_INJECT_TIMINGS and not is_embeddings)

    # -- ledger ------------------------------------------------------------------------
    def _record_usage(self, requested_model, rr, buf: bytes, status: int, *,
                      embeddings: bool, req_body: dict | None = None,
                      meta: dict | None = None):
        if self.store is None or embeddings:
            return
        usage = _extract_usage(buf)
        email = (self.headers.get("X-OpenWebUI-User-Email")
                 or self.headers.get("X-OpenWebUI-User-Id") or "direct")
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        # prompt-processing / generation time: prefer the engine's own numbers
        # (llama.cpp `timings`, ms), else the proxy's wall-clock split (meta).
        tim = _extract_timings(buf)
        if tim and (tim.get("prompt_ms") or tim.get("predicted_ms")):
            prefill_ms = round(tim.get("prompt_ms") or 0)
            decode_ms = round(tim.get("predicted_ms") or 0)
        else:
            m = meta or {}
            prefill_ms = round((m.get("prefill_s") or 0) * 1000)
            decode_ms = round((m.get("decode_s") or 0) * 1000)
        try:
            with self.lock:
                # cached_tokens = a synthetic *commercial* prefix-cache estimate
                # (what a frontier API would have discounted for this prefix
                # pattern), NOT the local backend's own reuse — see promptcache.py.
                pcm = _prompt_cache_model(self.pricing_path)
                cached = pcm.measure(f"{email}\x00{rr.stack}", pc_units(req_body or {}), prompt)
                self.store.record_usage(
                    user_email=email, requested_model=requested_model,
                    served_stack=rr.stack, served_model=rr.artifact, served_profile=rr.profile,
                    prompt_tokens=prompt, completion_tokens=completion, cached_tokens=cached,
                    prefill_ms=prefill_ms, decode_ms=decode_ms, ctx_tokens=prompt + completion,
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
               on_body=None, inject_timings: bool = False):
        """Forward one request upstream and relay the response. `streaming` chunk-
        relays a response with no content-length (SSE); otherwise buffers and sends
        with a real content-length. `on_body(bytes, status)` gets the full response
        for the ledger (bounded — only the first 256 KiB is kept).

        `inject_timings` (chat/completions only): if the upstream response carries a
        `usage` block but no llama.cpp-style `timings` (i.e. vLLM / SGLang), add
        Ollama-style `eval_count`/`eval_duration`/`total_duration` (proxy-measured
        wall clock, nanoseconds) so Open WebUI can show a tokens/sec rate.

        `on_body` is also handed a small `meta` dict: {prefill_s, decode_s} —
        the proxy wall-clock split (first upstream byte ~= prefill done), for the
        ledger's tok/s when the engine reports no timings of its own."""
        fwd_ct = self.headers.get("content-type", "application/json")
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"content-type": fwd_ct, "accept": self.headers.get("accept", "*/*")},
        )
        t0 = time.monotonic()
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
                on_body(payload, e.code, {})
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return self._send_json(502, {"error": {"message": f"upstream unreachable: {e}"}})

        keep = bytearray()
        tail = bytearray()          # rolling last ~8 KiB — finds the usage line even
        _TAILCAP = 8192             # on a stream far past the 256 KiB `keep` cap

        def _tee(chunk: bytes) -> None:
            if on_body and len(keep) < 262144:
                keep.extend(chunk[: 262144 - len(keep)])
            if inject_timings:
                tail.extend(chunk)
                if len(tail) > _TAILCAP:
                    del tail[: len(tail) - _TAILCAP]

        def _wc(b: bytes) -> None:   # one chunked-transfer frame + flush
            self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
            self.wfile.flush()

        with up:
            ctype = up.headers.get("content-type", "application/octet-stream")
            clen = up.headers.get("content-length")
            self.send_response(up.status)
            self.send_header("content-type", ctype)
            t_first = None
            if streaming and not clen:
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                lb = b""            # line buffer — relay whole SSE events (\n\n-delimited)
                while True:
                    chunk = up.read(8192)
                    if not chunk:
                        break
                    if t_first is None:
                        t_first = time.monotonic()
                    _tee(chunk)
                    lb += chunk
                    while b"\n\n" in lb:
                        evt, lb = lb.split(b"\n\n", 1)
                        evt += b"\n\n"
                        if inject_timings and evt.strip() == b"data: [DONE]":
                            extra = _timing_sse_line(bytes(tail), t0, t_first, time.monotonic())
                            if extra:
                                _wc(extra)
                        _wc(evt)
                if lb:
                    _wc(lb)
                self.wfile.write(b"0\r\n\r\n")
            else:
                data = up.read()
                _tee(data)
                if inject_timings:
                    data = _inject_body_timings(data, t0, time.monotonic()) or data
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        t_end = time.monotonic()
        if on_body:
            prefill_s = (t_first - t0) if t_first else 0.0
            decode_s = (t_end - t_first) if t_first else (t_end - t0)
            on_body(bytes(keep), up.status, {"prefill_s": prefill_s, "decode_s": decode_s})


# One long-lived prompt-cache estimator per pricing.json path (it holds a rolling
# per-(user, stack) request history). Rebuilt only if the path changes.
_PCM: list = [object(), None]  # [pricing_path sentinel, PromptCacheModel|None]


def _prompt_cache_model(pricing_path) -> PromptCacheModel:
    if _PCM[0] != pricing_path or _PCM[1] is None:
        from stackd.pricing import load_pricing
        cfg = (load_pricing(pricing_path) or {}).get("prompt_cache")
        _PCM[0], _PCM[1] = pricing_path, PromptCacheModel(cfg)
    return _PCM[1]


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


# proxy-measured per-stack generation stats, keyed by stack — from llama.cpp's
# own `timings` block on every response, so a short request isn't missed the way
# a 3s /engine poll would miss it. `seq` lets a poller detect a NEW request and
# average over real generations only (never over idle gaps).
_LAST_GEN: dict = {}
_GEN_SEQ = [0]


def _extract_timings(buf: bytes) -> dict:
    """llama.cpp `timings` block — JSON body or the final SSE `data:` line."""
    if not buf:
        return {}
    try:
        t = json.loads(buf).get("timings")
        if t:
            return t
    except (json.JSONDecodeError, AttributeError):
        pass
    last = {}
    for line in buf.split(b"\n"):
        line = line.strip()
        if line.startswith(b"data:") and b'"timings"' in line:
            try:
                t = json.loads(line[5:].strip()).get("timings")
                if t:
                    last = t
            except json.JSONDecodeError:
                continue
    return last


def _capture_gen(stack: str | None, buf: bytes) -> None:
    if not stack:
        return
    tm = _extract_timings(buf)
    if not tm:
        return
    g = round(tm.get("predicted_per_second") or 0, 1) or None
    p = round(tm.get("prompt_per_second") or 0, 1) or None
    ctx = int((tm.get("prompt_n") or 0) + (tm.get("predicted_n") or 0))
    dn, da = tm.get("draft_n") or 0, tm.get("draft_n_accepted") or 0
    mtp = round(100.0 * da / dn, 1) if dn else None
    if g is None and p is None:
        return
    _GEN_SEQ[0] += 1
    _LAST_GEN[stack] = {"gen_tok_s": g, "prompt_tok_s": p, "mtp_accept_pct": mtp,
                        "ctx_tokens": ctx or None, "at": time.time(), "seq": _GEN_SEQ[0]}


# --- Ollama-style timing injection for OpenAI-shaped backends (vLLM / SGLang) --
# Open WebUI (0.11.x, utils/response.py) derives `response_token/s` ONLY from
# `eval_count / eval_duration` (Ollama) or a llama.cpp `timings` block — OpenAI's
# `usage` has token counts but no duration. vLLM / SGLang stream OpenAI usage, so
# OWU shows counts and no rate. Here the proxy measures wall clock and attaches
# the Ollama fields it's missing. Opt out with STACKD_INJECT_TIMINGS=0.
_INJECT_TIMINGS = os.getenv("STACKD_INJECT_TIMINGS", "1").lower() not in ("0", "false", "no", "")


def _ollama_timings(usage: dict, prefill_s, decode_s: float, total_s: float) -> dict:
    """Ollama-shaped timing fields (durations in nanoseconds) from proxy timing."""
    tm = {
        "eval_count": int(usage.get("completion_tokens") or 0),
        "eval_duration": max(int(decode_s * 1e9), 1),
        "total_duration": max(int(total_s * 1e9), 1),
    }
    if usage.get("prompt_tokens") and prefill_s and prefill_s > 0:
        tm["prompt_eval_count"] = int(usage["prompt_tokens"])
        tm["prompt_eval_duration"] = int(prefill_s * 1e9)
    return tm


def _timing_sse_line(tail: bytes, t0: float, t_first, t_end: float) -> bytes | None:
    """A synthetic `data:` chunk carrying only the Ollama timing fields, to emit
    just before `data: [DONE]`. None if there's nothing to add."""
    if _extract_timings(tail):                       # llama.cpp already has real timings
        return None
    usage = _extract_usage(tail)                     # needs stream_options.include_usage
    if not usage or not usage.get("completion_tokens"):
        return None
    ct = int(usage.get("completion_tokens") or 0)
    prefill_s = (t_first - t0) if t_first else None
    decode_s = (t_end - t_first) if t_first else (t_end - t0)
    # Degenerate split: a small/cached response can arrive in one read, so
    # t_first ~= t_end and decode_s collapses to ~0 -> an absurd tok/s. If the
    # implied rate is impossible for a single stream (>3000 tok/s), just put all
    # the elapsed time on decode and drop the prefill split.
    if ct > 4 and decode_s > 0 and ct / decode_s > 3000:
        decode_s, prefill_s = (t_end - t0), None
    tm = _ollama_timings(usage, prefill_s, decode_s, t_end - t0)
    chunk = {"id": "stackd-timing", "object": "chat.completion.chunk",
             "created": int(time.time()), "choices": [], "usage": tm}
    return b"data: " + json.dumps(chunk).encode() + b"\n\n"


def _inject_body_timings(data: bytes, t0: float, t_end: float) -> bytes | None:
    """Non-streamed chat/completions: fold Ollama timing fields into `usage`.
    None if not a JSON object with a `usage` and no existing `timings`."""
    try:
        doc = json.loads(data)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or doc.get("timings") or not isinstance(doc.get("usage"), dict):
        return None
    # non-streamed: can't separate prefill from decode — attribute it all to decode
    doc["usage"] = {**doc["usage"], **_ollama_timings(doc["usage"], None, t_end - t0, t_end - t0)}
    return json.dumps(doc).encode()


def make_server(mgr: Manager, host: str, port: int, api_key: str | None,
                warm_wait_s: float = 120.0, *, store: Store | None = None,
                owu_base_url: str | None = None,
                pricing_path: str | None = None, cleaner=None) -> ThreadingHTTPServer:
    handler = type("_BoundHandler", (_Handler,), {
        "mgr": mgr, "lock": threading.Lock(), "api_key": api_key, "warm_wait_s": warm_wait_s,
        "store": store, "owu_base_url": owu_base_url, "pricing_path": pricing_path,
        "cleaner": cleaner,
    })
    srv = ThreadingHTTPServer((host, port), handler)
    handler.httpd = srv          # so POST /shutdown can stop serve_forever()
    return srv


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

    # SIGTERM (docker stop / compose down) -> graceful exit: run the `finally`
    # below (save state, stop the server). Engine containers are LEFT RUNNING by
    # default so a `compose restart` / rebuild keeps them (boot_reset re-adopts
    # the healthy ones). Set STACKD_TEARDOWN_ON_SIGTERM=1 to also tear every
    # engine down here — for a host where `compose down` should leave nothing
    # holding VRAM and an in-place restart isn't used. Explicit `stackctl down`
    # always tears down regardless of the env var.
    _term_teardown = os.environ.get("STACKD_TEARDOWN_ON_SIGTERM", "").lower() in ("1", "true", "yes")

    def _on_sigterm(signum, _frame):
        name = signal.Signals(signum).name
        print(f"\n[{time.strftime('%H:%M:%S')}] {name} — shutting down"
              + (" (tearing down engines)" if _term_teardown else ""))
        def _do():
            if _term_teardown:
                try:
                    with lock:
                        for n in mgr.shutdown_engines():
                            print(f"[{time.strftime('%H:%M:%S')}] teardown {n} (shutdown)")
                except Exception as e:  # noqa: BLE001
                    print(f"[shutdown] engine teardown error: {e}")
            stop.set()
            httpd.shutdown()
        threading.Thread(target=_do, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_sigterm)

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
                        events.record_evt(e, source="tick")
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
        events.record("boot-reset", _n, "stale/crashed — respawning", source="boot")

    # converge the active (default on fresh state) profile before accepting traffic
    try:
        with lock:
            for e in mgr.use(mgr.state.active_profile, manual=True):
                print(f"[boot] {e.action} {e.stack}"
                      + (f" — {e.detail}" if getattr(e, "detail", "") else ""))
                events.record_evt(e, source="boot")
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
