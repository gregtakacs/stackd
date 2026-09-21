"""
title: Comfy Toolbox (mask editor)
author: stackd
version: 0.1
description: Opens the Comfy mask editor INLINE in this chat (embedded, no new window).
    THIS IS THE ENTRY POINT for painted mask control whenever the user wants to pick/paint
    the masked area themselves ("let me choose the area", "open the editor/toolbox on this
    image", or a previous text-described edit was mis-segmented) -- call THIS tool, not the
    MCP retouch_image, which merely returns a link that opens in a new browser window. It
    renders nothing — it hands back the editor; the user paints and presses Render. For an
    ordinary describable edit with no painted control wanted, use edit_image instead.
requires: requests
"""

# Thin by design: all identity, registration, resolution and render work stays in the
# stackd daemon. This file runs INSIDE Open WebUI's server, which is precisely why it
# may hold the mint caller-auth (a valve, or inherited from OWU's own OPENAI_API_KEY
# env) — the browser never sees it. The browser sees only the minted one-time launch
# token inside the returned HTML, which expires in ~15 min and dies on first submit
# (stackd/toolbox/tokens.py governs that lifecycle; nothing here duplicates it).
#
# The embed contract is verified against the deployed Open WebUI 0.11.3 bundle:
# utils/middleware.py process_tool_result() checks isinstance(result, HTMLResponse) +
# Content-Disposition 'inline', renders the body as the sandboxed srcdoc UI, and hands
# the model only a neutral ui_component status (result_context below) — the editor's
# own iframe:height postMessage (M0-verified protocol) sizes it.
#
# Configuration (Admin -> Tools -> import this file, then):
#   VALVE stackd_base_url  — where the daemon answers FROM THIS CONTAINER
#                            (default http://stackd:11444 on the shared compose network).
#   The caller-auth (the stackd admin bearer mint demands) resolves in this order,
#   first non-blank wins — see _mint_caller_auth for why the ORDER is the design:
#     1. VALVE mint_key          — explicit override, encrypted at rest when
#                                  ENABLE_VALVE_ENCRYPTION is on.
#     2. env STACKD_MINT_KEY     — a deployment-level override for hosts without a
#                                  mounted secret.
#     3. VALVE mint_key_file     — the mounted compose secret, by default
#                                  /run/secrets/ollama_token: the same file the daemon
#                                  itself reads (STACKD_API_KEY_FILE), so a stack that
#                                  already mounts the secret needs NO secret pasted
#                                  anywhere and no secret ever enters the DB.
#     4. env OPENAI_API_KEY      — legacy inheritance; kept last because in a real
#                                  Open WebUI container it is set to the EMPTY string
#                                  (chat credentials live in the DB, not the
#                                  environment), so treating it as a hit guarantees a
#                                  403 mint_requires_caller_auth. Empty/blank is never
#                                  a hit at any step.
# stackd's OWN side must have STACKD_TOOLBOX_PUBLIC_URL set (browser-reachable base);
# mint answers a clear reason string if it is not — surface that text to the operator.

import asyncio
import os

import requests
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


class Tools:
    class Valves(BaseModel):
        stackd_base_url: str = "http://stackd:11444"
        mint_key: str = ""
        mint_key_file: str = "/run/secrets/ollama_token"
        timeout_s: int = 20

    def __init__(self):
        self.valves = self.Valves()

    def _mint_caller_auth(self):
        """The stackd admin bearer this Function must present to /toolbox/mint, resolved
        from SERVER-side sources only (valve -> STACKD_MINT_KEY -> mounted secret file ->
        OPENAI_API_KEY). Returns (key, source_label); the label rides into the failure
        text, because the whole class of bug this fixes was a silent empty bearer whose
        403 named neither the file nor the valve that was actually in force.

        Blank-at-every-step is enforced deliberately, not tidily: compose-set
        OPENAI_API_KEY= is a PRESENT, EMPTY variable in the deployed container, and an
        `or`-chain that treated presence as a hit shipped a bearer of nothing."""
        cands = (
            ("valve mint_key", self.valves.mint_key),
            ("env STACKD_MINT_KEY", os.environ.get("STACKD_MINT_KEY", "")),
        )
        for label, val in cands:
            val = (val or "").strip()
            if val:
                return val, label
        path = (self.valves.mint_key_file or "").strip()
        note = ""
        if path:
            try:
                with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                    val = f.read().strip()          # secret files carry trailing newlines
                if val:
                    return val, "secret file " + path
                note = "secret file %s is empty" % path
            except OSError as e:
                # Absent/unreadable is NOT terminal: a host that does not use compose
                # secrets (the standalone deploy/, a dev box) has no such file, and
                # aborting here would kill the OPENAI_API_KEY fallback for everyone who
                # never had the mount. Keep looking, but remember the complaint so a
                # dead-end chain reports the FILE as the suspect rather than blaming the
                # env it eventually fell through to.
                note = "unreadable secret file %s (%s)" % (path, e.__class__.__name__)
        val = (os.environ.get("OPENAI_API_KEY", "") or "").strip()
        if val:
            return val, "env OPENAI_API_KEY"
        return "", note or "no caller-auth source configured"

    async def open_mask_editor(self, __user__=None, __request__=None,
                               __chat_id__=None, __message_id__=None) -> HTMLResponse:
        """Open the Comfy mask editor INLINE in this chat, on its most recent image.
        Preferred over the MCP retouch_image link tool (which opens a new browser
        window): call this whenever the user wants to paint/choose the edit area."""
        headers = {}
        try:
            headers = {k.lower(): v for k, v in dict(__request__.headers).items()}
        except Exception:
            pass  # __request__ absent (older OWU?): fall back to the injected identity
        email = (headers.get("x-openwebui-user-email")
                 or (__user__ or {}).get("email") or "").strip().lower()
        if not email:
            return ("Comfy Toolbox: Open WebUI forwarded no user email for this call "
                    "(requires ENABLE_FORWARD_USER_INFO_HEADERS). Relay this to the user; "
                    "a describable edit can still proceed via edit_image.")
        body = {
            "email": email,
            "chat_id": headers.get("x-openwebui-chat-id") or __chat_id__ or "",
            "message_id": headers.get("x-openwebui-message-id") or __message_id__ or "",
        }
        key, key_src = self._mint_caller_auth()
        if not key:
            return ("Comfy Toolbox: no stackd caller-auth is available to this Function "
                    f"({key_src}). Set valve mint_key, export STACKD_MINT_KEY, or point "
                    "valve mint_key_file at the mounted secret the daemon itself uses "
                    "(default /run/secrets/ollama_token). Do not paste the key into chat.")
        # MUST NOT BLOCK THE EVENT LOOP. OWUI calls this coroutine directly on its own
        # loop (utils/tools.py wraps tool functions without a threadpool), and the mint
        # is NOT self-contained: stackd's h_mint resolves the chat's most recent image by
        # calling BACK into this same OWUI server (/api/v1/chats, /api/v1/files/.../content).
        # A blocking requests.post here froze the loop that those callbacks needed ->
        # circular wait -> this call's own timeout fired first (live 2026-09-20: "cannot
        # reach the stackd daemon ... Read timed out" at exactly timeout_s, while the
        # stackd log showed its open-webui:8080 file fetch hanging). to_thread keeps the
        # loop free to serve the mint's callbacks; the MCP retouch_image path never hit
        # this because it mints inside the stackd process, where nothing of OWUI's is blocked.
        try:
            r = await asyncio.to_thread(
                requests.post,
                self.valves.stackd_base_url.rstrip("/") + "/toolbox/mint",
                json=body, timeout=self.valves.timeout_s,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key},
            )
        except requests.RequestException as e:
            return f"Comfy Toolbox: cannot reach the stackd daemon at {self.valves.stackd_base_url} ({e})."
        try:
            data = r.json()
        except ValueError:
            return f"Comfy Toolbox: daemon answered {r.status_code} with a non-JSON body."
        if r.status_code != 200 or not data.get("ok"):
            reason = data.get("reason") or data.get("error") or f"HTTP {r.status_code}"
            caller = ""
            if reason in ("mint_requires_caller_auth", "mint_not_configured"):
                # The one failure whose cause is THIS file rather than the user's chat:
                # name the source that supplied the bearer (never the bearer itself).
                caller = f" (this Function sent a bearer from {key_src})"
            return (f"Comfy Toolbox could not open: {reason}{caller}. If the answer is an "
                    "unregistered user or a missing image, relay it; if the edit is "
                    "describable without painted control, edit_image can serve instead.")
        html = data.get("html") or ""
        if not html:
            return "Comfy Toolbox: the daemon minted a session but sent no editor document."
        # (HTMLResponse, result_context): the bundle's documented pair — the user gets
        # the editor, the model gets THIS instead of megabytes of inlined HTML.
        return (
            HTMLResponse(content=html, media_type="text/html",
                         headers={"Content-Disposition": "inline"}),
            "Comfy Toolbox mask editor is open for the user: they paint the area, tune "
            "edge/feather per object, pick Edit or Replace, and press Render; the result "
            "saves to their own Open WebUI files AND is posted back into this chat as a "
            "new assistant message automatically when the render finishes — the editor "
            "then collapses to a one-line summary and NEVER shows the finished image "
            "itself, so do not paste, re-attach or re-describe the result; just tell the "
            "user it is ready in the chat (the mint "
            "carried this chat's id — do not call edit_image for this same request now "
            "the user has the controls).",
        )
