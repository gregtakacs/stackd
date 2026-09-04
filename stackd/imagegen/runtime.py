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
    endpoint, serveable, state, capabilities, ...}."""
    with RT.lock:
        return RT.mgr.capabilities().get("image")


def request_capability(verb: str) -> dict:
    """Ask the elastic image tier to bring a model that provides `verb` resident
    (in-process — same daemon as the Manager). Returns Manager.set_image()'s dict:
    {ok, active_model, downgraded_from, note, ...} or {ok: False, error}."""
    with RT.lock:
        return RT.mgr.set_image(need_capability=verb)


def request_pipeline(active_model: str) -> dict:
    """Ask the tier to make a specific pipeline resident (auto-downgrades + says so
    if it doesn't fit). Same return shape as request_capability()."""
    with RT.lock:
        return RT.mgr.set_image(model=active_model)


def image_pipelines() -> list[dict]:
    """The tier's `prefer:` catalog: [{active_model, capabilities, backends,
    footprint_gib}, ...] — for resolving a user's loose pipeline name."""
    with RT.lock:
        return RT.mgr.image_status().get("prefer", [])


def resolve_user_key(email: str) -> str | None:
    with RT.lock:
        return RT.store.resolve_key(email) if RT.store is not None else None
