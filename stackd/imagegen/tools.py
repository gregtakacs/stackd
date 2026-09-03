"""
ComfyUI Flux.2 Klein MCP server -- generate_image / edit_image / stylize_image, exposed
over MCP's Streamable HTTP transport for Open WebUI (or any other MCP client) to connect
to once and never touch again.

MCP tool calls receive only their explicit arguments -- no __messages__/__request__/
__user__ hidden context the way native Open WebUI Tools get (confirmed by tracing
open_webui/utils/middleware.py's MCP call site: `tool_function(**kwargs)`, nothing
else). Two consequences that shape this file's design:

1. edit_image/stylize_image take no image parameter of any kind, always acting on the
   most recent image in the current chat branch. With no __messages__ to scan
   in-process, this instead calls back into Open WebUI's own REST API (GET
   /api/v1/chats/{id}, via the chat id Open WebUI
   forwards in a request header when ENABLE_FORWARD_USER_INFO_HEADERS=true) to find the
   most recent image itself -- see _resolve_image_url and
   openwebui_client.lookup_recent_image. Deliberately not model-suppliable at all (an
   earlier version accepted an optional explicit image_url override) -- confirmed in
   practice that giving the model an escape hatch here is a real, not just theoretical,
   footgun: on a retry/refinement, a model reached for the literal original upload
   instead of a generate_image REIMAGINE's own (pixel-unrelated) output earlier in the
   same branch, silently discarding it. Branch-walking the chat tree already resolves
   the correct image deterministically every time; removing the override removes the
   entire bug class instead of just documenting around it. If a user wants to act on a
   DIFFERENT image than the branch's current leaf, the correct mechanism is branching
   the conversation from that point (Open WebUI's own edit/regenerate), not a parameter.
   Every Open WebUI call in this file uses the CALLING user's own API key -- see
   openwebui_client.resolve_user_api_key, which looks it up from
   stackd's self-service /register registry via the forwarded
   X-OpenWebUI-User-Email header -- which is what makes this actually resolve each
   user's OWN chats/files despite Open WebUI's ENABLE_ADMIN_CHAT_ACCESS being
   intentionally off in this deployment. There is no shared-key fallback: if we don't
   know who's asking, there's no registry configured, or the caller hasn't
   self-registered, this raises openwebui_client.UserNotRegisteredError -- confirmed in
   practice that silently falling back to a shared key misattributes a real generated
   file to whoever that key belongs to, invisible to the actual requesting user. All
   three tools catch this immediately and return a clear "please register" error before
   ever submitting anything to ComfyUI.
2. generate_image's `prompt` is required -- there's no chat-history-derived fallback to
   generate one automatically without __messages__, and a capable tool-calling model
   already reasons over the full conversation itself.
"""

import copy
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time

import anyio
import httpx
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from starlette.responses import JSONResponse

from stackd.imagegen import (
    comfyui_client,
    config,
    openwebui_client,
    prompt_llm,
    runtime,
    workflows,
)

logger = logging.getLogger("stackd.imagegen")

# mcp 2.0.0 renamed FastMCP -> MCPServer and dropped host/port from its constructor
# entirely (confirmed against the installed 2.x SDK) -- they're now passed per-call to
# streamable_http_app()/run_streamable_http_async() instead, see _run() below.
# streamable_http_path defaults to "/mcp", so the full URL to register in Open WebUI's
# Admin Settings -> Tool Servers is http://<this-container>:{MCP_PORT}/mcp.
mcp = MCPServer("comfyui-flux2-klein")


def _comfy_base() -> tuple[str | None, str]:
    """(endpoint, error_note) for the ComfyUI the active profile has resident right now
    -- comfyui-cuda in 'everyday', comfyui-rocm in 'coding' -- via stackd's
    Manager.comfyui_target(). endpoint is None (with a human note) when nothing is
    serveable (e.g. the image engine is still warming after a profile switch)."""
    return runtime.comfyui_endpoint()

# The LLM-facing text for each @mcp.tool() below (prompting guidance: routing rules,
# parameter tuning, failure modes confirmed in practice, etc.) lives in tool_docs/*.md
# instead of each function's own docstring -- these are prompt content, not code
# documentation, and had grown to ~600 combined lines (over a third of this file) of prose
# sitting between the actual request-handling logic. Passing description= explicitly here
# means fn.__doc__ is never consulted at all (see mcp.server.mcpserver.tools.base.Tool.
# from_function: `func_doc = description or fn.__doc__ or ""`) -- each function keeps a
# short docstring purely for a human reading the code, unrelated to what the model sees.
_TOOL_DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_docs")


def _load_tool_doc(filename: str) -> str:
    with open(os.path.join(_TOOL_DOCS_DIR, filename)) as f:
        return f.read()


# -----------------------------------------------------------------------------
# Prompt-rewrite plumbing (see prompt_llm.py + workflow_graphs/models.json).
# Turns a plain user idea into what the chosen image model wants, by calling the
# shared OpenAI-compatible endpoint -- replacing the LLM that community ComfyUI
# workflows bake into the graph as a resident CLIP-loaded model.
# -----------------------------------------------------------------------------

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_prompt_file_cache: dict[str, str] = {}


def _load_prompt_file(name: str) -> str:
    if name not in _prompt_file_cache:
        with open(os.path.join(_PROMPTS_DIR, name)) as f:
            _prompt_file_cache[name] = f.read()
    return _prompt_file_cache[name]


# -----------------------------------------------------------------------------
# Model selection is NOT exposed. The MCP picks the pipeline purely from which
# stackd profile is live -- read from Manager.capabilities()["image"], which knows
# the resident image engine AND whether it is serveable *right now* (not mid-swap):
#
#   coding mode   (vLLM owns the RTX): flux2-klein on comfyui-rocm / the iGPU for
#                 generate + stylize + edit.
#   everyday mode (RTX has room): flux2-dev-turbo on comfyui-cuda for generate +
#                 stylize; flux2-klein on the iGPU for edit.
#
# No `model=` argument, no sticky state, no per-chat memory -- callers/users have
# no say and no visibility into which pipeline ran.
# -----------------------------------------------------------------------------


async def _current_mode() -> str:
    """'everyday' if stackd's resident, serveable image engine is a flux2-dev* pipeline,
    else 'coding' (klein). Reads Manager.capabilities()["image"] in-process."""
    cap = runtime.image_capability()
    if cap and cap.get("serveable"):
        model = (cap.get("active_model") or "").lower()
        return "everyday" if model.startswith("flux2-dev") else "coding"
    return "coding"


def _profile_for(tool: str, mode: str) -> str:
    """models.json profile for a tool call. edit is always klein-on-iGPU;
    generate/stylize follow the mode."""
    if tool == "edit":
        return "flux2-klein"
    return "flux2-dev-turbo" if mode == "everyday" else "flux2-klein"


async def _maybe_rewrite(
    rw_cfg: dict,
    user_prompt: str,
    *,
    api_key: str = "",  # accepted for call-site symmetry; NOT forwarded (see below)
    mode: str = "auto",
    aspect_ratio: str = "",
    width: int = 0,
    height: int = 0,
) -> tuple[str, str]:
    """Returns (prompt_to_use, note). `mode` is 'auto' (follow the model's manifest
    default), 'on' (force), or 'off' (force verbatim -- what raw_prompt=true maps to).
    A rewrite failure inside prompt_llm.rewrite() returns the input verbatim, which
    this surfaces as a note but never treats as an error."""
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "on", "off"):
        mode = "auto"
    if mode == "off" or not rw_cfg:
        return user_prompt, ""
    if mode == "auto" and not rw_cfg.get("default"):
        return user_prompt, ""
    sysfile = rw_cfg.get("system")
    if not sysfile:
        return user_prompt, ""
    try:
        system_prompt = _load_prompt_file(sysfile)
    except OSError:
        logger.warning("prompt file '%s' not found -- skipping rewrite", sysfile)
        return user_prompt, ""

    # Use the shared LLM_API_KEY (prompt_llm's default), NOT the calling user's key:
    # the per-user key (from the proxy /register registry) 401s against the proxy's
    # /v1 endpoint for anyone not registered there, which silently downgraded every
    # rewrite to "verbatim" -- fine for Flux.2, but that makes ideogram4 emit its
    # trained "blocked by safety filter" placeholder (raw text is out-of-distribution
    # for it). The shared token is accepted by /v1 and a ~500-token rewrite isn't
    # worth per-user ledger attribution.
    rewritten = await prompt_llm.rewrite(
        system_prompt,
        user_prompt,
        aspect_ratio=aspect_ratio or None,
        width=width or None,
        height=height or None,
        want_json=bool(rw_cfg.get("json")),
    )
    if rewritten == user_prompt:
        return user_prompt, f"prompt rewrite via {config.LLM_MODEL} unavailable; used prompt verbatim"
    return rewritten, f"prompt expanded via {config.LLM_MODEL}"


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------


def _round16(value: float) -> int:
    return max(16, round(value / 16) * 16)


def _resolve_dimensions_no_source(aspect_ratio: str, width: int, height: int):
    """generate_image's dimension resolution -- no source image, so aspect_ratio must be
    an explicit preset (or width/height given directly)."""
    notes = []
    if width and height:
        rw, rh = _round16(width), _round16(height)
        rw = max(config.MIN_SIDE, min(config.MAX_SIDE, rw))
        rh = max(config.MIN_SIDE, min(config.MAX_SIDE, rh))
        if (rw, rh) != (width, height):
            notes.append(
                f"requested {width}x{height} adjusted to {rw}x{rh} "
                f"(multiple of 16, {config.MIN_SIDE}-{config.MAX_SIDE}px per side)"
            )
        if rw * rh > workflows.MAX_PIXELS:
            raise ValueError(
                f"{rw}x{rh} is {rw * rh / 1_000_000:.1f}MP, over Flux.2 Klein 9B's 4MP limit. "
                "Pick smaller dimensions or use an aspect_ratio preset instead."
            )
        return rw, rh, notes
    if width or height:
        raise ValueError("Provide both width and height, or neither (to use aspect_ratio).")
    preset = workflows.ASPECT_PRESETS.get(aspect_ratio.lower().strip())
    if not preset:
        raise ValueError(
            f"Unknown aspect_ratio '{aspect_ratio}'. Choose one of: {', '.join(workflows.ASPECT_PRESETS)}."
        )
    return preset[0], preset[1], notes


def _dims_from_bytes(data: bytes):
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:
        pass
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


# Auto aspect-ratio-preserving path (aspect_ratio='auto') targets workflows.
# PRACTICAL_MAX_PIXELS, not the pipeline's raw MAX_PIXELS ceiling -- see that constant's
# comment: a source anywhere close to the full 4MP confirmed leaking into CPU/host RAM
# during edit_image. Previously hardcoded to 1024*1024 (~1MP) instead, an unrelated older
# "fast default" that undershot even the RAM-safe budget.
AUTO_BUDGET_PIXELS = workflows.PRACTICAL_MAX_PIXELS


def _resolve_dimensions_from_source(source_bytes: bytes, aspect_ratio: str, width: int, height: int):
    """edit_image's dimension resolution -- also supports aspect_ratio='auto', scaling to
    fit AUTO_BUDGET_PIXELS while preserving the source's own aspect ratio."""
    notes = []
    if width and height:
        rw, rh = _round16(width), _round16(height)
        rw = max(config.MIN_SIDE, min(config.MAX_SIDE, rw))
        rh = max(config.MIN_SIDE, min(config.MAX_SIDE, rh))
        if (rw, rh) != (width, height):
            notes.append(
                f"requested {width}x{height} adjusted to {rw}x{rh} "
                f"(multiple of 16, {config.MIN_SIDE}-{config.MAX_SIDE}px per side)"
            )
        if rw * rh > workflows.MAX_PIXELS:
            raise ValueError(f"{rw}x{rh} is {rw * rh / 1_000_000:.1f}MP, over Flux.2 Klein 9B's 4MP limit.")
        return rw, rh, notes
    if width or height:
        raise ValueError("Provide both width and height, or neither.")

    if aspect_ratio and aspect_ratio.lower() != "auto":
        preset = workflows.ASPECT_PRESETS.get(aspect_ratio.lower().strip())
        if not preset:
            raise ValueError(
                f"Unknown aspect_ratio '{aspect_ratio}'. Choose 'auto' or one of: {', '.join(workflows.ASPECT_PRESETS)}."
            )
        return preset[0], preset[1], notes

    dims = _dims_from_bytes(source_bytes)
    if not dims or dims[0] <= 0 or dims[1] <= 0:
        notes.append(
            f"could not detect source image size, defaulting to square "
            f"{workflows.ASPECT_PRESETS['square'][0]}x{workflows.ASPECT_PRESETS['square'][1]}"
        )
        return workflows.ASPECT_PRESETS["square"][0], workflows.ASPECT_PRESETS["square"][1], notes
    src_w, src_h = dims
    scale = min(1.0, (AUTO_BUDGET_PIXELS / (src_w * src_h)) ** 0.5)
    w = max(config.MIN_SIDE, min(config.MAX_SIDE, _round16(src_w * scale)))
    h = max(config.MIN_SIDE, min(config.MAX_SIDE, _round16(src_h * scale)))
    notes.append(f"preserving source aspect ratio: {src_w}x{src_h} -> {w}x{h}")
    return w, h, notes


# Deliberately broad, not a curated noun list -- an earlier curated-noun-list version
# missed unlisted multi-object phrases and let a bad multi-object segmentation through.
_CONJUNCTION_RE = re.compile(r"\s*,\s*|\s+and\s+|\s+or\s+|\s*&\s*|\s*/\s*", re.IGNORECASE)

_POSSESSION_VERB_RE = re.compile(r"\b(wearing|carrying|holding(?:\s+up)?|clutching)\b", re.IGNORECASE)


def _split_compound_regions(target_region: str) -> list[str]:
    """Returns [target_region] unchanged if it names one region, or 2+ parts if it looks
    ambiguous (see edit_image's caller for what happens then).

    One exemption to the plain conjunction split: a single subject named once, then
    wearing/carrying/holding a list of things, still names ONE region even though that
    list has conjunctions in it -- confirmed a real, not just theoretical, false positive:
    "the woman with her back to camera wearing a black backpack and patterned shorts" split
    into ('...backpack', 'patterned shorts') and got rejected as 2 objects, forcing a retry
    that dropped "patterned shorts" -- a real, useful disambiguating detail -- just to
    satisfy this check. That less-specific retry then failed to segment ANYTHING at all
    (see comfyui_client.mask_indicates_nothing_found) -- so this isn't just a cosmetic
    over-rejection, it can actively degrade the description into one segmentation can't
    localize. Only exempt when the FIRST part is the one naming the subject+verb and every
    later part is a short bare continuation of its item list (not itself introducing a new
    "X wearing/carrying Y" clause, which would mean a genuinely separate second subject --
    e.g. "the woman wearing a red hat and the man wearing a blue coat" must still split)."""
    parts = [p.strip() for p in _CONJUNCTION_RE.split(target_region.strip()) if p.strip()]
    if len(parts) <= 1:
        return [target_region.strip()]
    if _POSSESSION_VERB_RE.search(parts[0]):
        rest_are_bare_items = all(len(p.split()) <= 5 and not _POSSESSION_VERB_RE.search(p) for p in parts[1:])
        if rest_are_bare_items:
            return [target_region.strip()]
    return parts


async def _resolve_image_url(ctx: Context, api_key: str) -> str:
    """Always auto-detects the most recent image in this chat's active branch --
    edit_image/stylize_image have no image parameter of any kind (see this module's own
    docstring for why: a model-suppliable override was a real, confirmed bug source, not
    just a theoretical one). Walks Open WebUI's own REST API instead of relying on
    MCP's tool-call arguments, since MCP tools get no __messages__/hidden chat-history
    context at all -- see this module's own docstring. Requires Open WebUI's
    ENABLE_FORWARD_USER_INFO_HEADERS=true (forwards X-OpenWebUI-Chat-Id, and
    X-OpenWebUI-Message-Id for branch-aware lookup -- see
    openwebui_client.lookup_recent_image); without a chat id, or if nothing is found,
    raises so the caller gets a clear error instead of a confusing downstream fetch
    failure. api_key should be the caller's already-resolved key (see
    openwebui_client.resolve_user_api_key) -- using the calling user's own key here
    (rather than the shared default) is what lets this resolve THEIR chats at all, since
    Open WebUI's ENABLE_ADMIN_CHAT_ACCESS is off in this deployment.
    """
    headers = ctx.headers or {}
    chat_id = headers.get("X-OpenWebUI-Chat-Id")
    message_id = headers.get("X-OpenWebUI-Message-Id")
    if not chat_id:
        raise ValueError(
            "No chat id was forwarded to auto-detect the image to act on (requires Open "
            "WebUI's ENABLE_FORWARD_USER_INFO_HEADERS=true). This tool always acts on the "
            "most recent image in the current chat branch and has no way to reference a "
            "different one -- there's nothing else to try here."
        )

    found = await openwebui_client.lookup_recent_image(chat_id, message_id, api_key)
    if not found:
        raise ValueError(
            "No image could be found in this chat's active branch to act on (it may "
            "belong to a different user than the key in use here, or contain no image "
            "yet). Tell the user, in your reply, to attach or generate an image in this "
            "chat first."
        )
    return found


def _not_registered_response(e: "openwebui_client.UserNotRegisteredError") -> str:
    """Builds the JSON returned when openwebui_client.resolve_user_api_key raises
    UserNotRegisteredError -- called immediately after resolving the key, before any
    ComfyUI submission, so an unregistered user's call fails cleanly and cheaply instead
    of silently burning a real generation on a result that would just get misattributed
    to the shared key's account (see that exception's own docstring)."""
    link = f" at {config.LLAMA_PROXY_REGISTER_URL}" if config.LLAMA_PROXY_REGISTER_URL else ""
    who = "The calling user" if e.email == "(unknown user)" else e.email
    return json.dumps(
        {
            "error": (
                f"{who} hasn't registered their own Open WebUI API key yet ({e.reason}). "
                f"Tell the user, in your reply, to register their key on the self-service "
                f"registration page{link} (paste the API key from Open WebUI's Settings -> "
                f"Account -> API Keys) before this tool can be used. Do not retry this call "
                f"until they've done that."
            )
        }
    )


def _max_size_for_ratio(src_w: int, src_h: int, max_pixels: int, max_side: int) -> tuple[int, int]:
    """Largest (w, h), both multiples of 16 and each <= max_side, preserving src_w/src_h as
    closely as multiple-of-16 rounding allows, within a max_pixels total budget. Used to
    match REIMAGINE's generated image to its source photo's aspect ratio -- deliberately
    not clamped to <=1.0 scale, since generate_image has no pixel conditioning on the
    source at all (matching its aspect ratio is purely a framing choice, not a resolution
    one), so upscaling past the source's own pixel count to use the full budget is fine.
    """
    k = min(max_side / src_w, max_side / src_h, (max_pixels / (src_w * src_h)) ** 0.5)
    w = max(16, int(src_w * k // 16) * 16)
    h = max(16, int(src_h * k // 16) * 16)
    return w, h


async def _autodetect_source_aspect(ctx: Context, api_key: str) -> tuple[int, int, str] | None:
    """For a REIMAGINE-style bare generate_image call, best-effort matches the most recent
    photo's aspect ratio in this chat, so the model doesn't have to guess it by eye (visually
    distinguishing e.g. 4:3 from 16:9 from a rendered image is unreliable -- this reads the
    real pixel dimensions instead). Reads only that image's dimensions, never its pixel
    content -- generate_image remains pure text-to-image with zero pixel conditioning, this
    is strictly a framing choice. Uses the same chat-lookup mechanism edit_image/stylize_image
    use to auto-detect their own source image (see _resolve_image_url) via the forwarded
    X-OpenWebUI-Chat-Id/-Message-Id headers.

    Returns None on any failure (no chat id forwarded, no image found, fetch failed,
    dimensions undetectable) -- this is a nice-to-have default, not a hard requirement, so
    the caller falls back to the plain "square" preset rather than erroring out.
    """
    try:
        headers = ctx.headers or {}
        chat_id = headers.get("X-OpenWebUI-Chat-Id")
        if not chat_id:
            return None
        message_id = headers.get("X-OpenWebUI-Message-Id")
        found = await openwebui_client.lookup_recent_image(chat_id, message_id, api_key)
        if not found:
            return None
        source_bytes = await openwebui_client.fetch_image(found, api_key)
        dims = _dims_from_bytes(source_bytes)
        if not dims or dims[0] <= 0 or dims[1] <= 0:
            return None
        src_w, src_h = dims
        w, h = _max_size_for_ratio(src_w, src_h, workflows.PRACTICAL_MAX_PIXELS, config.MAX_SIDE)
        return w, h, f"aspect_ratio auto-matched from the referenced photo: {src_w}x{src_h} -> {w}x{h}"
    except Exception:
        logger.exception("generate_image: aspect-ratio auto-detect failed, falling back to square")
        return None


def _success_response(detail: str, urls: list[str]) -> str:
    """Builds the JSON a tool call returns on success. There is no __event_emitter__
    available here to push a 'chat:message:files' event and make the image display
    automatically -- an MCP tool's return value is just text the calling model reads
    like any other tool result -- so this explicitly instructs the model to embed the
    URL as markdown itself; confirmed in practice that without this instruction the
    model just describes the result in prose ("here is a link to your photo") instead
    of rendering it.

    Also confirmed in practice: a reasoning model sometimes writes this exact reply inside
    its own reasoning/thinking content instead of ever emitting a real final assistant
    message -- Open WebUI collapses reasoning content by default, so the image is
    technically "shown" but invisible unless the user manually expands "Thought for Ns".
    This isn't something a tool result can fully control (it depends on the model/inference
    server correctly closing its own reasoning block), but the explicit "final answer, not
    reasoning" framing below is a low-cost nudge in that direction.
    """
    image_md = "\n".join(f"![Image]({u})" for u in urls)
    return json.dumps(
        {
            "status": "success",
            "detail": detail,
            "images": [{"url": u} for u in urls],
            "message": (
                f"{detail} Done reasoning -- write your final reply to the user now "
                f"(not further reasoning/thinking content). That reply must include this markdown "
                f"exactly as given (do not just link or describe it in prose -- only markdown "
                f"image syntax renders inline in the chat):\n{image_md}"
            ),
        },
        ensure_ascii=False,
    )


# -----------------------------------------------------------------------------
# stylize_image's fixed-menu prompt data -- pure prompt-text data with no Open WebUI
# dependency at all.
# -----------------------------------------------------------------------------

STYLES: dict[str, str] = {
    "cinematic": (
        "Give it a bold, dramatic cinematic look with strong SUBJECT ISOLATION: throw the "
        "background heavily out of focus with large, smooth, prominent bokeh (soft round "
        "highlights), keep the foreground subject tack-sharp with 85mm-lens depth "
        "compression. Dramatic cinematic lighting -- a defined key light plus a strong "
        "rim/edge light wrapping the subject for separation, giving surfaces a glossy, "
        "sculpted, almost 3D-rendered sheen. Rich filmic contrast and a moody, high-end "
        "colour grade. Keep the shadows deep and atmospheric but not fully crushed -- the "
        "subject must stay clearly readable, never lost in murk. Keep the original "
        "framing, distance, and zoom."
    ),
    "cartoon": (
        "Redraw as a polished modern Western/Pixar-style cartoon illustration: clean bold "
        "outlines, smooth semi-flat cel shading, simplified friendly shapes, warm "
        "approachable character design, bright even lighting, a vivid but natural colour "
        "palette. A cohesive animated-film look."
    ),
    "line_art": (
        "Redraw as a clean black-and-white line art illustration -- confident bold ink "
        "outlines, minimal to no shading, no color fill anywhere in the image, including "
        "the background (render the existing background in the same clean linework style "
        "rather than removing it). Crisp uniform line weight throughout. "
        "Technical/editorial illustration quality, precise and uncluttered, similar to a "
        "polished pen-and-ink drawing."
    ),
    "oil_painting": (
        "Repaint this as a genuine oil painting, NOT a photo with a painterly filter -- the "
        "result must look unmistakably hand-painted, not photorealistic. Thick, clearly "
        "visible impasto brushstrokes with distinct individual brush marks, especially "
        "through hair, fabric, and background areas. Abstract fine detail into confident "
        "color blocks and strokes rather than smooth photographic precision -- skin and "
        "surfaces should show visible paint texture and brush direction, not airbrushed "
        "smoothness or photographic sharpness. Rich, warm, slightly muted color palette "
        "with soft edges between color blocks rather than crisp photographic edges. "
        "Clearly visible canvas weave texture throughout the entire image. Traditional "
        "portrait-painting composition and lighting reminiscent of classical or "
        "impressionist portraiture."
    ),
    "polaroid": (
        "A sharp, fully in-focus photograph with crisp fine detail and clearly resolved "
        "edges, given only a vintage instant-Polaroid colour grade: faded warm-toned "
        "colour, lifted blacks, flat muted overall contrast, fine even grain, gentle "
        "corner vignette, and a plain white Polaroid border framing the image. Colour and "
        "tone treatment only -- the underlying photo stays crisp and detailed. Nostalgic "
        "instant-snapshot feel."
    ),
    "vintage_photo": (
        "A sharp, fully in-focus photograph with crisp detail, given only the colour of a "
        "genuinely old print: faded desaturated colour or light sepia, flat muted contrast, "
        "fine even film grain, faint scratches and dust specks, a slight warm/yellowed "
        "cast, gentle vignette. The image itself stays crisp and clearly resolved -- an "
        "aged colour grade on a sharp photo, nothing softened or blurred."
    ),
    "manga": (
        "Redraw as a black-and-white Japanese manga illustration -- expressive large "
        "manga-style eyes, clean dynamic ink linework, screentone (dot pattern) shading "
        "for midtones and shadows instead of blended gradients, high contrast between "
        "black linework and white/screentone areas, characteristic manga proportions and "
        "expressive facial styling. Classic printed-manga-panel aesthetic."
    ),
}

COLOR_TREATMENT_DETAILS: dict[str, str] = {
    "sepia": (
        "Convert this image to a strictly monochrome sepia-toned rendering -- rendered "
        "only in warm brown/tan tones, with no other hues present anywhere in the image, "
        "similar to a genuine 19th/early-20th-century sepia print. Preserve tonal detail "
        "and contrast through variation in the brown tone itself, not by introducing any "
        "other color."
    ),
    "black_and_white": (
        "Convert this image to a strictly monochrome black-and-white rendering -- pure "
        "grayscale tones with no color cast of any kind (not sepia, not blue-toned, just "
        "neutral gray), while preserving full tonal range and contrast."
    ),
    "colorize": (
        "Render this image in full, natural color. If the source is black-and-white, "
        "sepia-toned, or otherwise monochrome, add plausible, historically-natural color "
        "throughout -- realistic skin tones, clothing colors, and environmental colors, "
        "as if expertly colorizing an old photograph. If the source is already in color, "
        "ensure rich, natural, true-to-life color throughout. Colors should look "
        "realistic, not artificially oversaturated or garish."
    ),
    "vivid": (
        "Boost color saturation and vibrancy throughout the image -- rich, punchy, vivid "
        "colors with strong color intensity and contrast, eye-catching and dynamic, while "
        "keeping skin tones natural and believable rather than oversaturated or artificial."
    ),
}

SEASON_DETAILS: dict[str, str] = {
    "spring": (
        "soft diffused light, fresh cool-bright colour, occasional puddles from recent "
        "rain. Only where trees or planted areas ALREADY exist in the image: budding "
        "branches, new light-green leaves, a few blossoms"
    ),
    "summer": (
        "harsh direct sunlight with hard-edged shadows, clear blue sky, warm saturated "
        "colour, heat haze. Any trees or plantings already in the image read as full and "
        "deep green; dry or golden grass on existing lawns"
    ),
    "autumn": (
        "low warm golden light, slightly muted overall colour, a cooler sky. Only where "
        "trees or planted areas ALREADY exist: orange/red/yellow leaves, some bare "
        "branches, scattered fallen leaves on the ground directly beneath them"
    ),
    "winter": (
        "flat pale low-angle light, desaturated cold colour, overcast sky. Snow on "
        "upward-facing surfaces (ground, rooftops, ledges, any existing branches); any "
        "existing deciduous trees are bare; icicles only where water would drip"
    ),
    # NOTE: every entry deliberately leads with light + colour + sky (the parts that
    # read as a grade) and scopes foliage to vegetation ALREADY present. flux2-dev at
    # stylize guidance takes a bare "orange foliage" literally and grows trees/leaves
    # across an urban frame -- buildings, roads, sky. _build_combined_prompt appends a
    # global "don't invent objects" clause on top of this (see below).
}

TIME_OF_DAY_DETAILS: dict[str, str] = {
    "dawn": (
        "soft pre-sunrise light, cool blue-gray tones, a faint pink/orange glow low on the "
        "horizon, long soft shadows, low overall ambient light"
    ),
    "morning": (
        "clear bright morning light, soft warm tones, long shadows from a low sun angle, "
        "crisp clear sky"
    ),
    "midday": (
        "bright direct overhead sunlight, high contrast, short compact shadows, vivid "
        "saturated colors, clear sky"
    ),
    "golden_hour": (
        "warm golden low-angle sunlight, soft long shadows, warm amber and orange tones "
        "throughout the scene, a gentle glow along the edges of objects"
    ),
    "sunset": (
        "warm orange/pink/purple sky, low warm light, long dramatic shadows, "
        "silhouette-friendly backlighting where appropriate"
    ),
    "night": (
        "dark sky, illumination from ambient sources (streetlights, windows, moonlight) "
        "rather than sunlight, deep shadows, cooler blue-toned ambient light, visible "
        "stars if outdoors and unobstructed"
    ),
}

WEATHER_DETAILS: dict[str, str] = {
    "clear": ("clear blue sky with no clouds, bright natural sunlight, sharp well-defined shadows"),
    "overcast": (
        "uniform gray cloud cover, soft flat diffused light, minimal shadows, muted color saturation"
    ),
    "rainy": (
        "overcast rain clouds, wet reflective ground and surfaces, visible rain in the air, "
        "muted desaturated colors, puddles reflecting the sky"
    ),
    "downpour": (
        "heavy, intense rainfall actively pouring down -- dense visible sheets and streaks "
        "of falling rain, dark dramatic storm clouds, low visibility and a darker overall "
        "mood, rain splashing and rippling on wet surfaces, wind-blown rain at a slight "
        "angle, heavily saturated wet surfaces and reflections; this should read as being "
        "caught in an active, intense storm, not a calm aftermath"
    ),
    "foggy": (
        "dense atmospheric fog reducing visibility, soft hazy diffused light, muted "
        "low-contrast colors, distant background elements fading into the mist"
    ),
    "stormy": (
        "dark dramatic storm clouds, high contrast between bright and shadowed areas, wind-blown foliage"
    ),
    "snowy": (
        "falling snow, snow accumulation on horizontal surfaces (ground, roofs, branches), "
        "soft flat overcast light, muted cool color palette"
    ),
}


def _dev_stylize_guidance(style: str, color_treatment: str, season: str, time_of_day: str, weather: str) -> float:
    """FluxGuidance for the flux2-dev stylize graph, scaled to how much the requested
    adjustment has to overpower ReferenceLatent. flux2-klein ignores this (no FluxGuidance
    node) -- it's dev-only. Heavy form re-renders need ~8 to land; a relight/colour grade
    at 8 blows out (dark, moody, loses the scene), so those get ~3.5; weather/season sit
    between."""
    heavy = {"oil_painting", "manga", "line_art"}
    if style in heavy:
        return 8.0  # full render-style change -- needs to punch through ReferenceLatent
    if style == "cartoon":
        # cartoon IS a full medium change, but at 8 dev redesigns faces into generic
        # cartoon characters and loses the source likeness. ~6 keeps the cel-shaded look
        # while ReferenceLatent still holds each person's actual face/build/scene.
        return 6.0
    if style == "cinematic":
        # relight + bokeh applied ON TOP of the existing medium. At 8, dev repaints the
        # whole frame and de-cartoons a cartoon source back to photoreal; ~5 lands the
        # light/DoF while leaving the source rendering (incl. cel shading) intact.
        return 5.0
    if style in {"polaroid", "vintage_photo"} or color_treatment:
        return 3.5  # film look / colour grade -- prompt is already forceful, keep it gentle
    if season and not time_of_day and not weather:
        # season alone is a light seasonal grade over an unchanged scene. At 5.5 dev
        # over-reads "foliage" and grows trees/leaves across an urban frame; ~4 lands
        # the light/colour shift while ReferenceLatent holds the built environment.
        return 4.0
    # time_of_day / weather (alone or mixed with season)
    return 5.5


# flux2-dev re-renders harder than klein (denoise 1 + FluxGuidance vs klein's distilled
# 4-step + ReferenceLatent), so a style string klein applies as a light relight, dev
# applies as a full repaint -- which de-cartoons a cartoon/illustrated source back toward
# photorealism. These dev-only style texts add explicit "stay in the source medium"
# language for the styles where that bites. klein keeps the plain STYLES entries.
STYLE_OVERRIDES_DEV: dict[str, str] = {
    "cartoon": (
        # POSITIVE-ONLY. This graph has no working negative (BasicGuider + a dead
        # ConditioningZeroOut) and Flux ignores negation regardless, so every noun
        # reads as a target -- "no faces on cars" literally grows faces on cars.
        # Describe only the wanted result; state what surfaces ARE, never what
        # they must not be. Structure/likeness is held by ReferenceLatent.
        "Re-render the whole frame as one polished modern Western/Pixar-style cartoon: "
        "flat matte cel shading, clean bold outlines, simplified stylised shapes, flat "
        "colour blocking, smooth even surfaces. Buildings, streets, sky, vehicles, windows "
        "and props become clean cartoon shapes with plain flat-painted surfaces. The people "
        "stay true to their real proportions, features and likeness from the source, drawn "
        "in that same clean cel-shaded style. Vivid but natural colours, warm cohesive "
        "animated-film lighting; a faithful cartoon of this exact scene."
    ),
    "cinematic": (
        "Apply cinematic LIGHTING and DEPTH only -- do NOT change the rendering medium of "
        "the source. Strong SUBJECT ISOLATION: throw the background well out of focus with "
        "smooth, prominent bokeh (soft round highlights), keep the foreground subject "
        "tack-sharp, 85mm-lens depth compression. Clean soft key plus fill and a gentle "
        "rim light for separation; keep exposure and brightness NATURAL, balanced "
        "contrast, do NOT crush the blacks or make it dark, low-key, dim, or moody. "
        "Whatever medium the source already is, the result stays that same medium: a "
        "photograph stays a photograph (a cinematic photo), a cartoon or cel-shaded "
        "illustration stays a cartoon (a polished '3D cartoon' with cinematic light and "
        "bokeh). Keep the original framing, distance, and zoom."
    ),
}


def _style_text(style: str, model: str) -> str:
    if model.startswith("flux2-dev") and style in STYLE_OVERRIDES_DEV:
        return STYLE_OVERRIDES_DEV[style]
    return STYLES[style]


def _build_combined_prompt(
    style: str, color_treatment: str, season: str, time_of_day: str, weather: str, *, model: str = ""
) -> str:
    parts = [
        # Positive-only (this pipeline has no working negative; naming a thing to
        # forbid it just adds it). Say what to keep and what to restyle.
        "Keep the exact composition, camera angle, subject positions, structural "
        "shapes, and each person's real features, pose and identity. Restyle only "
        "the surface look -- materials, shading, linework and colour -- of what is "
        "already in the frame; every object keeps its existing plain form."
    ]
    if style:
        parts.append(_style_text(style, model))
    if season:
        parts.append(
            f"Shift the scene toward {season}: {SEASON_DETAILS[season]}. Do NOT add trees, "
            "plants, grass, leaves or any greenery where none exist in the source; do not "
            "turn walls, roads, roofs or sky into vegetation. Buildings, pavement, "
            "vehicles and hard surfaces keep their exact shape and only change in light "
            "and colour."
        )
    if time_of_day:
        label = time_of_day.replace("_", " ")
        parts.append(f"Change the time of day to {label}: {TIME_OF_DAY_DETAILS[time_of_day]}.")
    if weather:
        parts.append(f"Change the weather to {weather}: {WEATHER_DETAILS[weather]}.")
    if color_treatment:
        parts.append(COLOR_TREATMENT_DETAILS[color_treatment])
    return " ".join(parts)


# -----------------------------------------------------------------------------
# generate_image
# -----------------------------------------------------------------------------


@mcp.tool(description=_load_tool_doc("generate_image.md"))
async def generate_image(
    ctx: Context,
    prompt: str,
    aspect_ratio: str = "",
    width: int = 0,
    height: int = 0,
    seed: int = -1,
    rewrite_prompt: str = "auto",
) -> str:
    """Text-to-image. The pipeline is chosen automatically from the current mode
    (see _profile_for). LLM-facing description: tool_docs/generate_image.md (this
    docstring is never sent to the model -- see _load_tool_doc's own comment)."""
    if not prompt:
        return json.dumps({"error": "prompt is required (no chat-history fallback available over MCP)."})

    profile = _profile_for("generate", await _current_mode())
    try:
        model_name, graph, nodes, entry = workflows.load_model("generate", profile)
    except (workflows.UnknownModel, workflows.ToolUnsupported) as e:
        return json.dumps({"error": str(e)})

    try:
        api_key = await openwebui_client.resolve_user_api_key(ctx)
    except openwebui_client.UserNotRegisteredError as e:
        return _not_registered_response(e)

    # edit_image/stylize_image accept the literal string "auto" for this same
    # "figure it out yourself" behavior -- confirmed in practice a model passes "auto"
    # here too, by analogy, expecting the same thing, and got a real "Unknown
    # aspect_ratio 'auto'" error back instead (this tool's ASPECT_PRESETS has no "auto"
    # entry; blank is what actually triggers auto-detection here). Normalize it to
    # blank rather than just documenting the difference, since the docstring alone
    # didn't stop the model from trying it.
    if aspect_ratio and aspect_ratio.strip().lower() == "auto":
        aspect_ratio = ""

    notes = []
    if not aspect_ratio and not width and not height:
        detected = await _autodetect_source_aspect(ctx, api_key)
        if detected:
            width, height, note = detected
            notes.append(note)
        else:
            aspect_ratio = "square"

    try:
        w, h, more_notes = _resolve_dimensions_no_source(aspect_ratio, width, height)
        notes.extend(more_notes)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)

    final_prompt, rw_note = await _maybe_rewrite(
        entry.get("prompt_rewrite") or {},
        prompt,
        api_key=api_key,
        mode=rewrite_prompt,
        aspect_ratio=aspect_ratio,
        width=w,
        height=h,
    )
    if rw_note:
        notes.append(rw_note)

    save_node = nodes["save"]
    workflows.set_node(graph, nodes["positive"], "positive", final_prompt)
    workflows.set_node(graph, nodes.get("width"), "width", w)
    workflows.set_node(graph, nodes.get("height"), "height", h)
    workflows.set_node(graph, nodes.get("seed"), "seed", seed)

    comfy_base, base_note = _comfy_base()
    if not comfy_base:
        return json.dumps({"error": base_note})

    try:
        prompt_id = await comfyui_client.submit_workflow(graph, base=comfy_base)
        images_by_node = await comfyui_client.wait_and_fetch(prompt_id, {save_node}, base=comfy_base)
    except httpx.HTTPError as e:
        logger.exception("generate_image: ComfyUI proxy request failed")
        return json.dumps({"error": f"Could not reach ComfyUI proxy: {e!r}"})
    except TimeoutError as e:
        logger.exception("generate_image: timed out waiting for ComfyUI")
        return json.dumps({"error": str(e)})

    images_raw = images_by_node.get(save_node, [])
    if not images_raw:
        return json.dumps({"error": "ComfyUI finished but returned no images."})

    try:
        urls = [await openwebui_client.save_image(data, "generated-image.png", api_key) for data in images_raw]
    except Exception as e:
        logger.exception("generate_image: failed to save result to Open WebUI")
        return json.dumps({"error": f"Generated image but failed to save it to Open WebUI: {e!r}"})

    detail = f"{w}x{h}, seed {seed}."
    if not entry.get("validated", False):
        detail += " (this pipeline's graph is not yet live-validated on this install)"
    if notes:
        detail += " " + " ".join(notes)
    return _success_response(detail, urls)


# -----------------------------------------------------------------------------
# edit_image
# -----------------------------------------------------------------------------


_EDIT_INSTR_RE = re.compile(
    r"\b(?:replace|swap(?:\s+out)?|change|turn|convert|transform)\b.*?"
    r"\b(?:with|to|into|for)\b\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
_MAKE_IT_RE = re.compile(
    r"^\s*make\s+(?:it|the\s+\S+(?:\s+\S+)?)\s+(?:into\s+|look\s+like\s+)?(.+)",
    re.IGNORECASE | re.DOTALL,
)

# Guards a same-turn edit_image retry spiral: every edit_image call in one turn
# re-resolves to the SAME source (edits don't commit to chat history until the
# turn ends) and Open WebUI only keeps the last result, so a model that calls
# again after a success silently throws away the earlier image. Updated only on
# a SUCCESSFUL edit; a match on the next call within the window is refused. A
# genuine follow-up in a later turn sees the previous RESULT as its source (a
# different hash), so it's allowed. 2026-09-02, see edit_image.
_LAST_EDIT: dict = {}
_EDIT_REPEAT_WINDOW_S = 900


def _describe_edit_target(prompt: str) -> str:
    """Generic backstop for the masked path (which feeds `prompt` straight into
    the inpaint conditioning -- no _maybe_rewrite there). Flux.2 is a caption
    model, not an instruction model: an instruction-phrased prompt ('Replace the
    red Lamborghini with a blue Audi R8 ...') leaves BOTH subjects in the
    conditioning and the original-object words drag the result back toward the
    source. Reduce 'replace/change/turn X with/to/into Y ...' (and 'make it
    Y ...') to 'Y ...'. Not an enumeration -- just the instruction frame. Left
    unchanged if it doesn't match (already a description, or an add/remove
    edit). tool_docs/edit_image.md tells the model to phrase it this way in the
    first place; this only catches the cases where it doesn't. 2026-09-02."""
    p = (prompt or "").strip()
    m = _EDIT_INSTR_RE.search(p) or _MAKE_IT_RE.match(p)
    if m:
        cand = m.group(1).strip().rstrip(". ").strip()
        if len(cand) >= 8:
            return cand
    return p


def _submit_edit(
    model_name: str,
    comfy_filename: str,
    positive: str,
    width: int,
    height: int,
    seed: int,
    target_region: str,
    edit_strength: float,
    color_correct_strength: float,
    invert_mask: bool,
    preserve_scene_context: bool,
    edge_softness: int,
) -> dict:
    """Builds the workflow dict for one edit_image call. No debug_mask_preview parameter
    here, since there's no second-image display channel for an MCP tool response the way
    Open WebUI's chat:message:files event gives its native Tools feature one -- this can
    be added back if a client-side use for it materializes.

    For a masked edit, `model_name` selects the per-model inpaint graph -- every one keeps
    the klein MASK_* node-id layout, so the id-based patching below is model-agnostic; only
    the front-end (loaders / FluxGuidance) differs, which this function never touches."""
    if target_region:
        _n, workflow = workflows.load_inpaint_graph(model_name)
        workflow[workflows.MASK_LOAD_IMAGE_NODE]["inputs"]["image"] = comfy_filename
        workflow[workflows.MASK_POSITIVE_NODE]["inputs"]["value"] = positive
        workflow[workflows.MASK_WIDTH_NODE]["inputs"]["value"] = width
        workflow[workflows.MASK_HEIGHT_NODE]["inputs"]["value"] = height
        workflow[workflows.MASK_SEED_NODE]["inputs"]["value"] = seed
        workflow[workflows.MASK_REGION_NODE]["inputs"]["value"] = target_region
        workflow[workflows.MASK_COLOR_STRENGTH_NODE]["inputs"]["value"] = color_correct_strength
        # The segmentation node (MASK_SEGMENT_NODE) is CLIPSegMask -- deterministic,
        # no seed input. (It replaced Florence2Run, which had a stuck baked seed.) If a
        # future seg node reintroduces a seed, wire the pipeline seed into it here.
        if "seed" in workflow[workflows.MASK_SEGMENT_NODE]["inputs"]:
            workflow[workflows.MASK_SEGMENT_NODE]["inputs"]["seed"] = seed

        # CLIPSegMask now sizes the grow + feather to the segmented object itself
        # (grow_frac / feather_frac). So the downstream graph steps here are only
        # a light finish, NOT the main margin -- a fixed 28 px here is what made
        # small objects balloon to fill an oversized mask. edge_softness scales
        # this residual feather (default 28 -> ~8 px) rather than setting it
        # outright; pass edge_softness<=0 for a hard composite edge.
        if edge_softness <= 0:
            del workflow["31"]
            del workflow[workflows.MASK_BLUR_NODE]
            del workflow["33"]
            del workflow["34"]
            del workflow[workflows.MASK_CLAMP_MARGIN_NODE]
            workflow["28"]["inputs"]["mask"] = [workflows.MASK_GROW_NODE, 0]
            workflow[workflows.MASK_COMPOSITE_NODE]["inputs"]["mask"] = [workflows.MASK_GROW_NODE, 0]
        else:
            soft = min(31, edge_softness)
            blur_radius = max(3, round(soft * 0.3))          # 28 -> 8
            workflow[workflows.MASK_BLUR_NODE]["inputs"]["blur_radius"] = blur_radius
            workflow[workflows.MASK_BLUR_NODE]["inputs"]["sigma"] = max(0.1, min(10.0, blur_radius / 3))
            workflow[workflows.MASK_CLAMP_MARGIN_NODE]["inputs"]["expand"] = blur_radius
            workflow[workflows.MASK_GROW_NODE]["inputs"]["expand"] = max(4, round(soft * 0.15))  # 28 -> 4

        if invert_mask:
            workflow[workflows.MASK_INVERT_NODE] = {
                "inputs": {"mask": [workflows.MASK_SEGMENT_NODE, 0]},  # CLIPSegMask mask = output 0
                "class_type": "InvertMask",
                "_meta": {"title": "Invert Mask"},
            }
            workflow[workflows.MASK_GROW_NODE]["inputs"]["mask"] = [workflows.MASK_INVERT_NODE, 0]

        if not preserve_scene_context:
            workflow[workflows.MASK_KSAMPLER_NODE]["inputs"]["positive"] = [workflows.MASK_REGION_REFLATENT_NODE, 0]
            del workflow[workflows.MASK_FULL_REFLATENT_NODE]
            del workflow[workflows.MASK_FULL_ENCODE_NODE]
    else:
        workflow = copy.deepcopy(workflows.EDIT_WORKFLOW)
        workflow[workflows.EDIT_LOAD_IMAGE_NODE]["inputs"]["image"] = comfy_filename
        workflow[workflows.EDIT_POSITIVE_NODE]["inputs"]["value"] = positive
        workflow[workflows.EDIT_WIDTH_NODE]["inputs"]["value"] = width
        workflow[workflows.EDIT_HEIGHT_NODE]["inputs"]["value"] = height
        workflow[workflows.EDIT_SEED_NODE]["inputs"]["value"] = seed
        workflow[workflows.EDIT_KSAMPLER_NODE]["inputs"]["denoise"] = edit_strength
    return workflow


@mcp.tool(description=_load_tool_doc("edit_image.md"))
async def edit_image(
    ctx: Context,
    prompt: str = "",
    target_region: str = "",
    aspect_ratio: str = "auto",
    width: int = 0,
    height: int = 0,
    seed: int = -1,
    edit_strength: float = 0.35,
    color_correct_strength: float = 0.95,
    invert_mask: bool = False,
    preserve_scene_context: bool = True,
    edge_softness: int = 28,
    rewrite_prompt: str = "auto",
) -> str:
    """Targeted image edit (whole-image or masked). Routing (see _current_mode /
    the target_region branch below): a MASKED edit in everyday mode runs on
    flux2-dev-turbo on the eGPU (CLIPSeg auto-mask + inpaint, ~15-20s, tuned in
    edit/flux2-dev-turbo-inpaint.json); every other case -- whole-image edits, and
    anything in coding mode -- stays on flux2-klein on the iGPU. LLM-facing
    description: tool_docs/edit_image.md (never sent to the model -- see
    _load_tool_doc).

    Note: the dev-turbo inpaint graph skips ColorMatchV2 (it cast a colour tint
    + halo on real colour changes), so color_correct_strength is a no-op on that
    path -- still honoured by the klein graph. preserve_scene_context works
    normally on both."""
    if not prompt:
        return json.dumps({"error": "prompt is required (no chat-history fallback available over MCP)."})

    target_region = (target_region or "").strip()

    # TEMP diagnostic (2026-09-02): masked "convert to blue Audi R8" kept failing
    # observed via Open WebUI -- root-caused to instruction-phrased prompt +
    # branded target_region. Log raw vs resolved so a regression is visible.
    if target_region:
        logger.info(
            "edit_image (masked): prompt=%r -> %r | target_region=%r | "
            "preserve_scene_context=%r invert_mask=%r seed=%r edge_softness=%r",
            prompt, _describe_edit_target(prompt),
            target_region, preserve_scene_context, invert_mask, seed, edge_softness,
        )
    else:
        logger.info(
            "edit_image (whole): prompt=%r seed=%r rewrite_prompt=%r aspect=%r %dx%d",
            prompt, seed, rewrite_prompt, aspect_ratio, width, height,
        )

    # Masked (target_region) edits run on flux2-dev-turbo on the eGPU in everyday
    # mode -- much faster to iterate on than the emulated-fp8 iGPU. Whole-image
    # edits, and any edit in coding mode, stay on flux2-klein on the iGPU.
    if target_region and (await _current_mode()) == "everyday":
        model_name = "flux2-dev-turbo"
    else:
        model_name = "flux2-klein"
    _n, model_meta = workflows.model_entry(model_name)
    edit_spec = ((model_meta.get("tools") or {}).get("edit")) or {}

    if target_region and not invert_mask:
        # invert_mask edits select a broad region specifically so everything OUTSIDE it can
        # be changed instead (e.g. target_region="the people and the dog", invert_mask=True
        # to edit the background behind everyone) -- multiple named things combining into
        # one broad selection is the documented, intended use of that mode (see this
        # function's own docstring examples), not an ambiguous multi-object request the way
        # it would be without invert_mask. Precision of the selection boundary also matters
        # less here since the edit itself happens outside it.
        regions = _split_compound_regions(target_region)
        if len(regions) > 1:
            return json.dumps(
                {
                    "error": (
                        f"REJECTED: target_region must name exactly ONE object or area, and "
                        f"'{target_region}' contains a conjunction (\"and\" / \",\" / \"&\" / "
                        f"\"/\" / \"or\"), which this tool never accepts -- it reads as "
                        f"{' + '.join(repr(r) for r in regions)}, and the segmenter blends or "
                        f"misses when handed more than one target. There is no way to edit two "
                        f"regions in one call. Pick ONE of:\n"
                        f"  (a) if {regions[0]!r} and {regions[1]!r} are parts of ONE subject, "
                        f"name that subject instead -- 'the cap and gown' -> 'the graduate', "
                        f"'the people and the dog' -> 'the family' -- and call once with that.\n"
                        f"  (b) if they are unrelated things, do NOT call edit_image again this "
                        f"turn (a second call re-edits the original and only the last result "
                        f"survives). Instead, in your reply, make ONE of the two changes and "
                        f"tell the user the other needs a follow-up request.\n"
                        f"Do not keep retrying edit_image with reworded regions -- one clean "
                        f"single-object call, or hand it back to the user."
                    )
                }
            )

    edit_strength = max(0.05, min(1.0, edit_strength))
    color_correct_strength = max(0.0, min(1.0, color_correct_strength))
    invert_mask = bool(invert_mask) and bool(target_region)
    preserve_scene_context = bool(preserve_scene_context) or not bool(target_region)
    edge_softness = max(0, min(120, int(edge_softness)))

    try:
        api_key = await openwebui_client.resolve_user_api_key(ctx)
    except openwebui_client.UserNotRegisteredError as e:
        return _not_registered_response(e)

    try:
        image_url = await _resolve_image_url(ctx, api_key)
        source_bytes = await openwebui_client.fetch_image(image_url, api_key)
    except Exception as e:
        logger.exception("edit_image: failed to fetch source image")
        return json.dumps({"error": f"Could not fetch source image: {e!r}"})

    # Same-turn retry guard (see _LAST_EDIT). If this exact source was just
    # edited successfully, another call now would re-edit the ORIGINAL and the
    # user would only ever see this one -- refuse and make the model hand back
    # what it already has.
    src_sig = hashlib.sha256(source_bytes).hexdigest()
    _prev = _LAST_EDIT
    if (_prev.get("src") == src_sig
            and (time.monotonic() - _prev.get("ts", 0.0)) < _EDIT_REPEAT_WINDOW_S):
        return json.dumps({"error": (
            "You have ALREADY run edit_image on this exact image in this turn and "
            "have not shown that result to the user yet. Do NOT call edit_image "
            "again now. Every call in one turn edits the ORIGINAL image (edits do "
            "not chain until your turn ends) and Open WebUI keeps only the LAST "
            "result -- calling again silently discards the image you already made "
            f"(previous call: {_prev.get('prompt', '')!r}). Reply to the user with "
            "that image. If it is not right, say so in your reply and let them ask "
            "for another pass -- the next turn's edit will correctly build on it."
        )})

    try:
        w, h, notes = _resolve_dimensions_from_source(source_bytes, aspect_ratio, width, height)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)

    if target_region:
        flags = []
        if invert_mask:
            flags.append("inverted")
        if not preserve_scene_context:
            flags.append("identity swap, no scene context")
        flag_str = f", {', '.join(flags)}" if flags else ""
        pipeline_label = f"precise edit (target_region='{target_region}'{flag_str})"
    else:
        pipeline_label = f"whole-image edit [{model_name}]"
        if not model_meta.get("validated", False):
            pipeline_label += " (graph not yet live-validated)"

    # Whole-image edits run through the model registry; masked (inpaint) edits stay on
    # the bespoke Klein pipeline _submit_edit builds (registry-gated to flux2-klein above).
    rw_note = ""
    if target_region:
        # No _maybe_rewrite on the masked path -- but do reduce an instruction-
        # phrased prompt to a plain description of the wanted result, so the
        # original-object tokens don't fight the inpaint (see _describe_edit_target).
        final_prompt = _describe_edit_target(prompt)
    else:
        final_prompt, rw_note = await _maybe_rewrite(
            model_meta.get("prompt_rewrite") or {},
            prompt,
            api_key=api_key,
            mode=rewrite_prompt,
            aspect_ratio="" if (aspect_ratio or "").lower() == "auto" else aspect_ratio,
            width=w,
            height=h,
        )

    comfy_base, base_note = _comfy_base()
    if not comfy_base:
        return json.dumps({"error": base_note})

    try:
        source_bytes = (
            comfyui_client.downscale_to_exact_size(source_bytes, w, h)
        )
        comfy_filename = await comfyui_client.upload_to_comfy(source_bytes, "edit_src", base=comfy_base)
        if target_region:
            workflow = _submit_edit(
                model_name,
                comfy_filename,
                final_prompt,
                w,
                h,
                seed,
                target_region,
                edit_strength,
                color_correct_strength,
                invert_mask,
                preserve_scene_context,
                edge_softness,
            )
            save_node = workflows.MASK_SAVE_NODE
            fetch_nodes = {save_node, workflows.MASK_PREVIEW_NODE}
        else:
            _name, workflow, nodes, _entry = workflows.load_model("edit", model_name)
            save_node = nodes["save"]
            workflows.set_node(workflow, nodes["positive"], "positive", final_prompt)
            workflows.set_node(workflow, nodes.get("width"), "width", w)
            workflows.set_node(workflow, nodes.get("height"), "height", h)
            workflows.set_node(workflow, nodes.get("seed"), "seed", seed)
            workflows.set_node(workflow, nodes.get("image"), "image", comfy_filename)
            workflows.set_node(workflow, nodes.get("denoise"), "denoise", edit_strength)
            fetch_nodes = {save_node}
        prompt_id = await comfyui_client.submit_workflow(workflow, base=comfy_base)
        images_by_node = await comfyui_client.wait_and_fetch(prompt_id, fetch_nodes, base=comfy_base)
    except httpx.HTTPError as e:
        logger.exception("edit_image: ComfyUI proxy request failed")
        return json.dumps({"error": f"Could not reach ComfyUI proxy: {e!r}"})
    except TimeoutError as e:
        logger.exception("edit_image: timed out waiting for ComfyUI")
        return json.dumps({"error": str(e)})

    edit_images = images_by_node.get(save_node, [])
    if not edit_images:
        return json.dumps({"error": "ComfyUI finished but returned no images."})

    # Checks the actual segmentation mask, not the output image -- see
    # comfyui_client.mask_indicates_nothing_found's module comment for why: an earlier
    # pixel-diff-based version of this check was confirmed unreliable for small/blurry/
    # distant content (a correctly-located, correctly-edited small sign's text got
    # wrongly rejected because the KSampler's random seed that particular run happened to
    # produce a lower-contrast result, nothing to do with segmentation actually failing).
    mask_images = images_by_node.get(workflows.MASK_PREVIEW_NODE, [])
    if target_region and mask_images and all(comfyui_client.mask_indicates_nothing_found(m) for m in mask_images):
        return json.dumps(
            {
                "error": (
                    f"ComfyUI completed the edit but the segmentation mask for target_region "
                    f"'{target_region}' came back essentially empty -- CLIPSeg couldn't locate "
                    f"it, so nothing was actually painted (confirmed happening in practice: "
                    f"this is not a hypothetical). Do not treat this as success. CLIPSeg "
                    f"segmentation is deterministic, so an identical retry will fail "
                    f"identically -- you must improve target_region: a shorter, more concrete "
                    f"visual description of the SAME target, preferring a single distinctive "
                    f"appearance feature over an orientation/pose clause (e.g. 'the woman in "
                    f"the black backpack' rather than 'the woman with her back to camera "
                    f"wearing a black backpack') -- segmentation grounds on visual appearance, "
                    f"not pose or orientation. If you dropped a distinguishing detail on a prior retry "
                    f"specifically to satisfy this tool's multi-object check, that detail "
                    f"may have been exactly what made the description unique -- put it "
                    f"back if the shorter version doesn't work either, and mention the "
                    f"situation to the user rather than silently retrying indefinitely."
                )
            }
        )

    try:
        urls = [await openwebui_client.save_image(data, "edited-image.png", api_key) for data in edit_images]
    except Exception as e:
        logger.exception("edit_image: failed to save result to Open WebUI")
        return json.dumps({"error": f"Edited image but failed to save it to Open WebUI: {e!r}"})

    # Record this successful edit so an immediate repeat call on the same source
    # (a same-turn retry) is refused -- see _LAST_EDIT / the guard above.
    _LAST_EDIT.clear()
    _LAST_EDIT.update({"src": src_sig, "ts": time.monotonic(), "prompt": prompt})

    detail = f"{w}x{h}, seed {seed}, {pipeline_label}."
    if rw_note:
        notes.append(rw_note)
    if notes:
        detail += " " + " ".join(notes)
    return _success_response(detail, urls)


# -----------------------------------------------------------------------------
# stylize_image
# -----------------------------------------------------------------------------


@mcp.tool(description=_load_tool_doc("stylize_image.md"))
async def stylize_image(
    ctx: Context,
    style: str = "",
    color_treatment: str = "",
    season: str = "",
    time_of_day: str = "",
    weather: str = "",
    upscale_by: float = 1.0,
    seed: int = -1,
) -> str:
    """Curated stylize/mood presets. Pipeline chosen from the current mode
    (dev-turbo-on-eGPU in everyday mode, klein-on-iGPU in coding mode); both hold
    composition via denoise-1 + ReferenceLatent and change only the requested
    treatment. LLM-facing description: tool_docs/stylize_image.md."""
    profile = _profile_for("stylize", await _current_mode())
    try:
        model_name, graph, nodes, model_meta = workflows.load_model("stylize", profile)
    except (workflows.UnknownModel, workflows.ToolUnsupported) as e:
        return json.dumps({"error": str(e)})

    style = (style or "").strip().lower()
    color_treatment = (color_treatment or "").strip().lower()
    season = (season or "").strip().lower()
    time_of_day = (time_of_day or "").strip().lower()
    weather = (weather or "").strip().lower()

    if style and style not in STYLES:
        return json.dumps({"error": f"Unknown style '{style}'. Choose one of: {', '.join(STYLES)}, or leave empty."})
    if color_treatment and color_treatment not in COLOR_TREATMENT_DETAILS:
        return json.dumps(
            {"error": f"Unknown color_treatment '{color_treatment}'. Choose one of: {', '.join(COLOR_TREATMENT_DETAILS)}, or leave empty."}
        )
    if season and season not in SEASON_DETAILS:
        return json.dumps({"error": f"Unknown season '{season}'. Choose one of: {', '.join(SEASON_DETAILS)}."})
    if time_of_day and time_of_day not in TIME_OF_DAY_DETAILS:
        return json.dumps({"error": f"Unknown time_of_day '{time_of_day}'. Choose one of: {', '.join(TIME_OF_DAY_DETAILS)}."})
    if weather and weather not in WEATHER_DETAILS:
        return json.dumps({"error": f"Unknown weather '{weather}'. Choose one of: {', '.join(WEATHER_DETAILS)}."})
    if not style and not color_treatment and not season and not time_of_day and not weather:
        return json.dumps(
            {"error": "No adjustment specified -- set style='cinematic' and/or at least one of color_treatment/season/time_of_day/weather."}
        )

    upscale_by = max(0.1, min(4.0, upscale_by))
    style_prompt = _build_combined_prompt(style, color_treatment, season, time_of_day, weather, model=profile)
    seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)

    try:
        api_key = await openwebui_client.resolve_user_api_key(ctx)
    except openwebui_client.UserNotRegisteredError as e:
        return _not_registered_response(e)

    try:
        image_url = await _resolve_image_url(ctx, api_key)
        source_bytes = await openwebui_client.fetch_image(image_url, api_key)
    except Exception as e:
        logger.exception("stylize_image: failed to fetch source image")
        return json.dumps({"error": f"Could not fetch source image: {e!r}"})

    comfy_base, base_note = _comfy_base()
    if not comfy_base:
        return json.dumps({"error": base_note})

    try:
        source_bytes = comfyui_client.downscale_to_pixel_budget(
            source_bytes, workflows.PRACTICAL_MAX_PIXELS / (upscale_by**2)
        )
        comfy_filename = await comfyui_client.upload_to_comfy(source_bytes, "stylize_src", base=comfy_base)
        _n, workflow, nodes, _e = workflows.load_model("stylize", profile)
        save_node = nodes["save"]
        workflows.set_node(workflow, nodes["prompt"], "prompt", style_prompt)
        if nodes.get("guidance"):  # flux2-dev only -- scale FluxGuidance to the treatment weight
            workflows.set_node(
                workflow, nodes["guidance"], "guidance",
                _dev_stylize_guidance(style, color_treatment, season, time_of_day, weather),
            )
        workflows.set_node(workflow, nodes.get("image"), "image", comfy_filename)
        workflows.set_node(workflow, nodes.get("scale_by"), "scale_by", upscale_by)
        workflows.set_node(workflow, nodes.get("seed"), "seed", seed)
        prompt_id = await comfyui_client.submit_workflow(workflow, base=comfy_base)
        images_by_node = await comfyui_client.wait_and_fetch(prompt_id, {save_node}, base=comfy_base)
    except httpx.HTTPError as e:
        logger.exception("stylize_image: ComfyUI proxy request failed")
        return json.dumps({"error": f"Could not reach ComfyUI proxy: {e!r}"})
    except TimeoutError as e:
        logger.exception("stylize_image: timed out waiting for ComfyUI")
        return json.dumps({"error": str(e)})

    images_raw = images_by_node.get(save_node, [])
    if not images_raw:
        return json.dumps({"error": "ComfyUI finished but returned no images."})

    try:
        urls = [await openwebui_client.save_image(data, "stylized-image.png", api_key) for data in images_raw]
    except Exception as e:
        logger.exception("stylize_image: failed to save result to Open WebUI")
        return json.dumps({"error": f"Stylized image but failed to save it to Open WebUI: {e!r}"})

    applied = []
    if style:
        applied.append(style)
    if color_treatment:
        applied.append(f"color_treatment={color_treatment}")
    if season:
        applied.append(f"season={season}")
    if time_of_day:
        applied.append(f"time_of_day={time_of_day}")
    if weather:
        applied.append(f"weather={weather}")
    detail = f"applied: {', '.join(applied)}; upscale_by {upscale_by}, seed {seed}."
    if not model_meta.get("validated", False):
        detail += " (this pipeline's graph is not yet live-validated on this install)"
    return _success_response(detail, urls)


class BearerAuthMiddleware:
    """Raw ASGI middleware requiring `Authorization: Bearer <config.MCP_API_KEY>` on every
    request. Deliberately not using the mcp SDK's built-in TokenVerifier/AuthSettings --
    that machinery is for acting as a real OAuth resource server (issuer_url, protected
    resource metadata endpoints, etc.), which is more than a static shared secret needs
    and which Open WebUI's plain "bearer" tool-server auth_type (a static header, no OAuth
    handshake) wouldn't exercise anyway. Written as raw ASGI rather than
    Starlette's BaseHTTPMiddleware so it can't interfere with the streamable-http
    transport's long-lived SSE/chunked bodies -- it only inspects headers before the
    wrapped app ever sees the connection.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        if not hmac.compare_digest(auth_header, f"Bearer {self.token}"):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def _serve_async() -> None:
    # Mirrors MCPServer.run_streamable_http_async() -- host/port are no longer
    # constructor/settings state in mcp 2.0.0, just kwargs on this call -- but wraps the
    # app in BearerAuthMiddleware first -- see its docstring for why this isn't done via
    # the SDK's own auth_server_provider/token_verifier/AuthSettings.
    app = BearerAuthMiddleware(mcp.streamable_http_app(host=config.MCP_HOST), config.MCP_API_KEY)
    server = uvicorn.Server(uvicorn.Config(app, host=config.MCP_HOST, port=config.MCP_PORT, log_level="info"))
    await server.serve()


def start_mcp_server(mgr, store, lock=None) -> "threading.Thread":
    """Bind the in-process stackd handles and run the MCP Streamable-HTTP app on its own
    daemon thread (its own asyncio loop -- stackd's stdlib :11444 server stays on the
    main thread). Called once from `stackd.serve.serve()`. Returns the thread.

    `lock` is stackd's single request lock; the MCP tools touch `mgr`/`store` from this
    thread, so every touch goes through it (see stackd/imagegen/runtime.py).
    """
    import threading

    runtime.bind(mgr, store, lock)
    if not config.MCP_API_KEY:
        logger.warning("MCP_API_KEY not set -- the image MCP endpoint is unauthenticated")

    def _thread() -> None:
        try:
            anyio.run(_serve_async)
        except Exception:  # noqa: BLE001
            logger.exception("stackd image MCP server exited")

    t = threading.Thread(target=_thread, name="stackd-imagegen-mcp", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    raise SystemExit(
        "stackd.imagegen.tools is not a standalone server any more -- it runs inside "
        "`stackd serve` (which calls start_mcp_server())."
    )
