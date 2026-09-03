"""
Env-var configuration for stackd's embedded image-generation MCP tools
(``stackd/imagegen``). There's no per-tool settings UI for an MCP server, so this is
the whole configuration surface.

Every setting can be given directly as an env var (e.g. ``MCP_API_KEY``) or, matching
this stack's Docker-secrets pattern, via a ``_FILE``-suffixed env var pointing at a file
whose contents are the value. ``_FILE`` wins if both are set.

What used to live here and no longer does: ``PROXY_BASE_URL`` / ``EGPU_COMFY_BASE_URL``
(the ComfyUI endpoint now comes from ``Manager.comfyui_target()`` in-process) and
``LLAMA_PROXY_BASE_URL`` (per-user keys are read straight from stackd's ``Store``). See
``stackd/imagegen/runtime.py``.
"""

import os


def _env(name: str, default: str | None = None) -> str | None:
    """Reads name, or the contents of the file at {name}_FILE if that's set instead."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        with open(file_path) as f:
            return f.read().strip()
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


# Open WebUI's own REST API -- both directions of image I/O (see openwebui_client.py).
# Every call uses the CALLING user's own API key, resolved per-request from stackd's
# Store (runtime.RT.store) -- there is no shared/fallback key.
OPENWEBUI_BASE_URL = _env("OPENWEBUI_BASE_URL", "http://open-webui:8080")

# Flux.2 Klein 9B's own practical bounds for width/height.
MIN_SIDE = _env_int("MIN_SIDE", 256)
MAX_SIDE = _env_int("MAX_SIDE", 2048)

TIMEOUT_S = _env_int("TIMEOUT_S", 300)
POLL_INTERVAL_S = _env_int("POLL_INTERVAL_S", 2)

# The MCP Streamable-HTTP listener (a uvicorn/starlette ASGI app on its own daemon
# thread inside `stackd serve`, separate from stackd's :11444 stdlib front). Reached
# only on the Docker network as http://stackd:{MCP_PORT}/mcp.
MCP_HOST = _env("MCP_HOST", "0.0.0.0")
MCP_PORT = _env_int("MCP_PORT", 8000)

# Shared secret required as `Authorization: Bearer <token>` on every MCP request (see
# BearerAuthMiddleware in tools.py). Empty disables auth -- only for local testing.
MCP_API_KEY = _env("MCP_API_KEY", "")

# Shown in the UserNotRegisteredError message so the calling model can hand the user a
# real clickable link. Optional -- blank omits the link.
LLAMA_PROXY_REGISTER_URL = _env("LLAMA_PROXY_REGISTER_URL", "")

# -----------------------------------------------------------------------------
# Prompt-rewrite LLM (see prompt_llm.py). Turns a plain user idea into the input a given
# image model wants -- replacing the ComfyUI-side "magic prompt" nodes with a call to
# the same OpenAI-compatible endpoint the rest of the stack runs. Default target is
# stackd's own /v1 on localhost (the resident everyday 27B). Any failure falls back to
# the prompt verbatim -- a rewrite must never block a generation.
LLM_BASE_URL = _env("LLM_BASE_URL", "http://localhost:11444/v1")
LLM_API_KEY = _env("LLM_API_KEY", "")
# 'assistant-nothink' = the 27B with reasoning disabled -- fast, reliable single-shot JSON.
LLM_MODEL = _env("LLM_MODEL", "assistant-nothink")
LLM_TIMEOUT_S = _env_int("LLM_TIMEOUT_S", 90)
# Master switch. "0"/"false"/"no"/"off" disables all rewriting (prompt used verbatim).
LLM_REWRITE_ENABLE = (_env("LLM_REWRITE_ENABLE", "true") or "").strip().lower() not in ("0", "false", "no", "off")
