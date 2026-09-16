"""
Comfy Toolbox — the Open WebUI-facing photo-editing suite (mask editor + edit engine).

Design contract, decided in the planning round and load-bearing for every file here:

* ONE serializable **EditSpec** (see `spec.py`) is the only way an edit is described.
  A chat-asked edit, an uploaded mask, the in-chat embed widget and the standalone lab
  page all produce the same EditSpec and hit the same code path and the same ComfyUI
  graphs. Nothing gets a private side channel.
* ONE job API (`api.py`, mounted at `/toolbox/*`) is the only thing the UI talks to.
  The two mounts (Open WebUI rich-UI embed, `lab.<domain>` full page) differ only in
  carrier: the editor bundle in `web/` is written to be mounted both ways and must use
  nothing that needs a same-origin context (no cookies, no localStorage, no
  same-origin-only APIs) because the embed mount is an opaque-origin sandboxed
  `srcdoc` iframe — see `web.py`'s `embed_document()` for why the JS/CSS are inlined.
* Identity: the *page* may be human-gated (Traefik `chain-oauth`), but every
  `/toolbox/*` call additionally carries a **single-use HMAC token** (`tokens.py`)
  minted at launch time and bound to an email. Reason, in the same spirit as
  `imagegen/openwebui_client.py`'s deny-by-default per-user keys: stackd's Traefik
  routers are Host-based on one container, so app-level verification means a router
  rules typo cannot expose job creation or another user's artifacts.
* Attribution still goes through the calling user's OWN Open WebUI API key, resolved
  from stackd's Store (see `imagegen/openwebui_client.resolve_user_api_key`). The
  token's email is cross-checked against it — a token minted for A must not save
  files as B.

Round-1 (M0) scope is the pieces needed to answer "can a mask be authored at all":
`tokens` (who is allowed to submit), `masks` (what a painted mask MEANS once it is
server-side — the browser is only a crude painter, the server owns the semantics),
`web` (the mount-agnostic editor document), and `spike` (a standalone LAN server that
reuses exactly these, so the editor can be tested on a phone before anything in the
live daemon changes).
"""
