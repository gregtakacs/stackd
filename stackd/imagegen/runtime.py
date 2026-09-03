"""Process-wide handles the image tools use *instead of* HTTP calls back to stackd.

`comfyui-mcp` used to be its own container and reached stackd over HTTP for three
things: which ComfyUI is live (`/capabilities`), where to submit
(`/comfyui` passthrough), and the caller's per-user Open WebUI key (`/register`
registry). Merged into the daemon, those become direct `Manager` / `Store` calls.

`bind()` is called once by `tools.start_mcp_server()` from inside `stackd serve`.
`lock` is stackd's single request lock (`serve.serve()` passes
`httpd.RequestHandlerClass.lock`) -- the MCP tools run on their own uvicorn thread, so
every `mgr`/`store` touch must hold it, same as the stdlib handlers do.
"""

from __future__ import annotations

import threading


class _Runtime:
    mgr = None            # stackd.manager.Manager
    store = None          # stackd.store.Store | None
    lock: threading.Lock = threading.Lock()


RT = _Runtime()


def bind(mgr, store, lock: threading.Lock | None = None) -> None:
    RT.mgr = mgr
    RT.store = store
    if lock is not None:
        RT.lock = lock


def comfyui_endpoint() -> tuple[str | None, str]:
    """(endpoint, note) for the serveable image ComfyUI right now -- cuda or rocm,
    whichever the active profile has resident. `endpoint` is None (with a human note)
    when nothing is serveable."""
    with RT.lock:
        return RT.mgr.comfyui_target()


def image_capability() -> dict | None:
    """The `image` entry of `Manager.capabilities()` (or None): {stack, active_model,
    endpoint, serveable, state, workflow_templates, ...}."""
    with RT.lock:
        return RT.mgr.capabilities().get("image")


def resolve_user_key(email: str) -> str | None:
    with RT.lock:
        return RT.store.resolve_key(email) if RT.store is not None else None
