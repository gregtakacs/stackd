"""
Prompt rewriting via an external OpenAI-compatible chat endpoint.

This replaces the "magic prompt" / "JSON prompt builder" nodes that ship inside the
community ComfyUI workflows (a Gemma/Qwen LLM loaded through a CLIPLoader and run with
a `TextGenerate` node, permanently resident in VRAM just to turn a plain idea into the
structured caption Ideogram 4 wants). Instead we POST to the same endpoint the rest of
this stack already runs -- the stack's own /v1 (a resident chat model) --
so nothing extra has to be loaded.

Contract: `rewrite()` never raises and never blocks a generation. On any failure
(endpoint unreachable, timeout, HTTP error, empty/garbage response, and -- when
want_json -- unparseable JSON that can't be repaired) it logs a warning and returns the
caller's original `user_idea` unchanged. The image tools treat "rewrite returned the
input verbatim" as a normal, supported outcome.
"""

import json
import logging
import re

import httpx

from stackd.imagegen import config

logger = logging.getLogger("stackd.imagegen.prompt_llm")

# Models sometimes wrap output in ```json ... ``` despite being told not to; strip it
# before parsing/returning rather than failing the whole rewrite over a fence.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _unfence(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _first_json_object(text: str) -> str | None:
    """Best-effort: return the substring from the first '{' to its matching '}', so a
    model that emitted a sentence before/after the JSON despite instructions still
    yields a parseable object. None if there's no balanced object at all."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


async def rewrite(
    system_prompt: str,
    user_idea: str,
    *,
    api_key: str | None = None,
    aspect_ratio: str | None = None,
    width: int | None = None,
    height: int | None = None,
    want_json: bool = False,
) -> str:
    """Ask config.LLM_MODEL to turn `user_idea` into what the target image model wants,
    guided by `system_prompt` (loaded from prompts/*.txt by the caller).

    api_key   -- Bearer token for the call; defaults to config.LLM_API_KEY. server.py
                 passes the calling user's own registered key so the rewrite's tokens
                 land in that user's savings-ledger bucket instead of 'shared'.
    aspect_ratio / width / height -- appended to the user turn as context when set, so
                 the model can size bboxes / compositions to the real target frame.
    want_json -- request response_format=json_object and validate/repair the result to
                 a single JSON object string; on failure, fall back to the raw idea.

    Returns the rewritten string, or `user_idea` verbatim on any failure.
    """
    if not config.LLM_REWRITE_ENABLE or not system_prompt or not user_idea:
        return user_idea

    frame_bits = []
    if aspect_ratio and aspect_ratio.lower() not in ("", "auto"):
        frame_bits.append(f"target aspect ratio: {aspect_ratio}")
    if width and height:
        frame_bits.append(f"target resolution: {width}x{height}")
    user_turn = user_idea if not frame_bits else f"{user_idea}\n\n(" + "; ".join(frame_bits) + ")"

    body: dict = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_turn},
        ],
        "temperature": 0.4,
        "stream": False,
    }
    if want_json:
        # llama.cpp / vLLM behind the proxy both honor this; harmless if ignored.
        body["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json"}
    key = api_key or config.LLM_API_KEY
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S) as client:
            resp = await client.post(
                f"{config.LLM_BASE_URL}/chat/completions", json=body, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        logger.warning("prompt rewrite failed; using the prompt verbatim", exc_info=True)
        return user_idea

    text = _unfence(text)
    if not text:
        logger.warning("prompt rewrite returned empty; using the prompt verbatim")
        return user_idea

    if not want_json:
        return text

    # want_json: return a compact, valid single JSON object or bail to the raw idea.
    candidate = text if text.startswith("{") else (_first_json_object(text) or text)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        logger.warning("prompt rewrite (json) was not parseable; using the prompt verbatim")
        return user_idea
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
