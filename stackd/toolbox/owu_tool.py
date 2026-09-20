"""
title: Comfy Toolbox (mask editor)
author: stackd
version: 0.1
description: Opens the Comfy mask editor on this chat's most recent image. Call it when the
    user wants to pick/paint the masked area themselves ("let me choose the area", "open the
    editor/toolbox on this image", or a previous text-described edit was mis-segmented). It
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
# Configuration (Admin -> Functions -> import this file, then):
#   VALVE stackd_base_url  — where the daemon answers FROM THIS CONTAINER
#                            (default http://stackd:11444 on the shared compose network).
#   VALVE mint_key         — the stackd admin bearer; leave empty to inherit the
#                            container's OPENAI_API_KEY env (already set by compose).
# stackd's OWN side must have STACKD_TOOLBOX_PUBLIC_URL set (browser-reachable base);
# mint answers a clear reason string if it is not — surface that text to the operator.

import os

import requests
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


class Tools:
    class Valves(BaseModel):
        stackd_base_url: str = "http://stackd:11444"
        mint_key: str = ""
        timeout_s: int = 20

    def __init__(self):
        self.valves = self.Valves()

    async def open_mask_editor(self, __user__=None, __request__=None,
                               __chat_id__=None, __message_id__=None) -> HTMLResponse:
        """Open the Comfy mask editor for the most recent image in this chat."""
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
        key = self.valves.mint_key or os.environ.get("OPENAI_API_KEY", "")
        try:
            r = requests.post(
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
            return (f"Comfy Toolbox could not open: {reason}. If the answer is an "
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
            "saves to their own Open WebUI files. The link is single-use (~15 min). Do "
            "not call edit_image for this same request now the user has the controls.",
        )
