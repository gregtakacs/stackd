"""
Image I/O against Open WebUI's own REST API.

This server runs as a standalone process, separate from Open WebUI's own Python
interpreter -- it can't reach Open WebUI's internal storage/file-handling code directly
(that's part of a whole FastAPI app, not a published library), so both directions of
image I/O go over Open WebUI's public REST API instead, authenticated with the CALLING
user's own Open WebUI API key (resolved per-request by resolve_user_api_key -- there is
no shared/fallback key, see its docstring). Arguably a cleaner boundary than in-process
access would have been anyway.
"""

import re

import httpx

from stackd.imagegen import config, runtime


class UserNotRegisteredError(Exception):
    """Raised by resolve_user_api_key whenever a per-user key can't be resolved, for
    ANY reason -- there is no shared-key fallback to fall back to instead any more.
    Confirmed in practice that silent fallback is a real, non-hypothetical problem, not
    a theoretical one: a real user's generated image ended up saved under the shared
    key's account (invisible to them, since Open WebUI's ENABLE_ADMIN_CHAT_ACCESS is
    off) with no indication anything had gone wrong. Deliberately deny-by-default now:
    if we can't be sure who's asking and that they've registered their own key, the
    call fails instead of guessing. Callers should catch this immediately after
    resolving the key and return a clear, actionable error WITHOUT proceeding to submit
    a (comparatively expensive) ComfyUI generation that would just get discarded."""

    def __init__(self, email: str, reason: str):
        self.email = email
        self.reason = reason
        super().__init__(f"{email} is not registered ({reason})")


async def resolve_user_api_key(ctx) -> str:
    """Resolves the Open WebUI API key to use for this call: the calling user's OWN key,
    read from stackd's Store (the `/register` self-service registry) keyed by their
    forwarded X-OpenWebUI-User-Email header. There is no shared-key fallback -- each of
    the following raises UserNotRegisteredError instead of guessing whose account to use:
    - No email header forwarded (Open WebUI's ENABLE_FORWARD_USER_INFO_HEADERS=false, or
      the call didn't come from Open WebUI).
    - The datastore is disabled (`stackd serve --no-db`).
    - The forwarded email hasn't registered a key.
    Confirmed in practice that silently falling back to a shared key here misattributes a
    real generated file to whoever the shared key belongs to -- invisible to the actual
    requesting user -- so every one of these is a hard deny, full stop.
    """
    request = ctx.request_context.request if ctx else None
    email = request.headers.get("X-OpenWebUI-User-Email") if request else None
    if not email:
        raise UserNotRegisteredError(
            "(unknown user)",
            "no X-OpenWebUI-User-Email header was forwarded -- check Open WebUI's "
            "ENABLE_FORWARD_USER_INFO_HEADERS setting",
        )
    email = email.strip().lower()

    key = runtime.resolve_user_key(email)
    if not key:
        raise UserNotRegisteredError(email, "no registration found")
    return key


async def fetch_image(image_url: str, api_key: str) -> bytes:
    """Fetches a /api/v1/files/{id}/content relative path with the given API key --
    exactly, and only, the shape openwebui_client.lookup_recent_image returns. There is
    no model-suppliable image reference anywhere in this server any more (edit_image/
    stylize_image always auto-detect -- see server.py's module docstring for why), so
    image_url here is always our own server-generated string, never arbitrary input --
    this used to also handle bare file ids, data: URIs, fabricated file:// paths, and
    genuine external URLs, all in service of sanitizing a model-supplied value that no
    longer exists as an input surface at all."""
    url = f"{config.OPENWEBUI_BASE_URL.rstrip('/')}/{image_url.lstrip('/')}"
    return await _authenticated_get(url, api_key)


async def _authenticated_get(url: str, api_key: str) -> bytes:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content


_GENERATED_FILE_ID_RE = re.compile(r"/api/v1/files/([^/\s)\"']+)/content")


def _branch_messages(history: dict, message_id: str | None) -> list[dict]:
    """Walks parentId links from message_id back to the root, mirroring Open WebUI's own
    get_message_list() (open_webui/utils/misc.py) exactly -- history.messages is a flat
    dict of EVERY message ever created in this chat object, including abandoned branches
    (Open WebUI never deletes a branch when you edit/regenerate an earlier message, it just
    adds a new parentId chain alongside the old one) -- so scanning all of it sorted by
    timestamp, as an earlier version of this function did, can return an image from a
    different branch than the one actually being viewed/continued. Returns messages ordered
    newest-last (i.e. the caller should scan the reversed list for "most recent")."""
    if not message_id or message_id not in history:
        return []
    chain = []
    seen = set()
    current = history.get(message_id)
    while current:
        mid = current.get("id")
        if mid in seen:
            break
        seen.add(mid)
        chain.append(current)
        current = history.get(current.get("parentId"))
    chain.reverse()
    return chain


def _find_image_ref(message: dict) -> str | None:
    for file in message.get("files") or []:
        content_type = file.get("content_type") or file.get("file", {}).get("meta", {}).get("content_type", "")
        if file.get("type") == "image" or content_type.startswith("image/"):
            file_id = file.get("id") or file.get("url")
            if file_id:
                return f"/api/v1/files/{file_id}/content"

    text_parts = []
    content = message.get("content")
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        text_parts.extend(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")

    # Confirmed in practice this is a real, high-impact bug: a tool-calling assistant
    # message (Open WebUI's "output" array format -- reasoning/function_call/
    # function_call_output/message blocks) always has content == '' -- the actual
    # image reference lives inside output[], which the check above never looked at.
    # That made every image an assistant's OWN prior tool call produced invisible to
    # auto-detect, so a chained "leave image_url blank" edit always fell all the way
    # back to the original upload (the only message type with `files` set) instead of
    # the immediately-preceding result -- silently discarding every edit in a multi-step
    # chain but the first. Scan every text fragment in output[] too: a
    # function_call_output's text is our own tool's {"images": [{"url": ...}]} JSON
    # response, and a message block's text is the final rendered `![Image](url)`
    # markdown -- both contain the same /api/v1/files/{id}/content pattern
    # _GENERATED_FILE_ID_RE already looks for below.
    for item in message.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            out = item.get("output")
            if isinstance(out, list):
                text_parts.extend(o.get("text", "") for o in out if isinstance(o, dict))
            elif isinstance(out, str):
                text_parts.append(out)
        elif item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text_parts.append(c.get("text", ""))

    found = _GENERATED_FILE_ID_RE.findall(" ".join(text_parts))
    return f"/api/v1/files/{found[-1]}/content" if found else None


async def lookup_recent_image(chat_id: str, message_id: str | None, api_key: str) -> str | None:
    """Finds the most recent image in the ACTIVE BRANCH of a chat. MCP tools get no
    __messages__/chat-history context at all (confirmed by tracing
    open_webui/utils/middleware.py's MCP call site), so this walks Open WebUI's own REST
    API instead, using the chat id and current message id forwarded in the
    X-OpenWebUI-Chat-Id/-Message-Id headers (see server.py) when
    ENABLE_FORWARD_USER_INFO_HEADERS is on.

    Branch-aware by design: if message_id is given, walks parentId links from it back to the
    root (_branch_messages) and scans only that path, newest first -- so editing an earlier
    message to start a new branch (e.g. deliberately branching off right after a specific
    generated photo specifically so "the last image in this branch" is unambiguous) correctly
    picks up that branch's own last image, not whatever the most-recently-touched OTHER
    branch in the same chat happens to contain. Falls back to scanning every message in the
    chat by raw timestamp, ignoring branches, only if message_id wasn't forwarded or isn't
    found (older Open WebUI versions, or a client that doesn't forward it).

    Scans uploaded-file attachments (message['files'], image content types) and
    previously-generated images referenced in message text (the /api/v1/files/{id}/content
    URLs this module's own save_image returns). Returns a /api/v1/files/{id}/content path, or
    None if nothing was found.

    NOTE: only resolves chats owned by whichever account api_key belongs to -- Open WebUI's
    ENABLE_ADMIN_CHAT_ACCESS is off in this deployment (a deliberate per-user privacy
    boundary, not an oversight). api_key must be the calling user's OWN key (see
    resolve_user_api_key) so this actually resolves chats belonging to whoever is asking --
    there is no shared key to fall back to any more.
    """
    url = f"{config.OPENWEBUI_BASE_URL.rstrip('/')}/api/v1/chats/{chat_id}"
    try:
        data = await _authenticated_get_json(url, api_key)
    except httpx.HTTPStatusError:
        return None

    history = data.get("chat", {}).get("history", {}).get("messages", {})

    branch = _branch_messages(history, message_id)
    messages = reversed(branch) if branch else sorted(history.values(), key=lambda m: m.get("timestamp", 0), reverse=True)

    for message in messages:
        ref = _find_image_ref(message)
        if ref:
            return ref

    return None


async def _authenticated_get_json(url: str, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def save_image(image_bytes: bytes, filename: str, api_key: str) -> str:
    """Uploads a result image via POST /api/v1/files/ (process=False -- skips Open
    WebUI's RAG/content-extraction pipeline, irrelevant for an image result) and
    returns a relative /api/v1/files/{id}/content URL -- the same shape
    lookup_recent_image itself resolves, so this result becomes exactly what a later
    edit_image/stylize_image call in this chat auto-detects and chains onto (there's no
    parameter for a caller to pass this back into any more -- see fetch_image's own
    docstring). api_key must be the calling user's own key (see resolve_user_api_key)
    so the saved file is actually owned by them -- there is no shared key to fall back
    to any more, that's the entire point of per-user keys existing."""
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{config.OPENWEBUI_BASE_URL.rstrip('/')}/api/v1/files/",
            params={"process": "false"},
            files={"file": (filename, image_bytes, "image/png")},
            headers=headers,
        )
        resp.raise_for_status()
        file_id = resp.json()["id"]
        return f"/api/v1/files/{file_id}/content"
