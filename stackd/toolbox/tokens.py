"""
Single-use HMAC tokens for the `/toolbox/*` surface.

Why this exists: the editor has to submit jobs from a browser context that cannot hold
a stackd bearer secret (the in-chat mount is an opaque-origin sandboxed iframe, and the
lab mount is a user-controlled page), and the endpoints must still know *who* is
asking, because the whole stackd image pipeline attributes saved files to the calling
user's own Open WebUI key (see `imagegen/openwebui_client.resolve_user_api_key`, which
is deliberately deny-by-default after a confirmed silent misattribution). So the launch
hand-off mints a short-lived token carrying that identity, and the UI presents it on
every call.

The signing key is derived from stackd's existing shared secret with a domain-separator
HMAC rather than using the raw key, so the bearer token that gates `/comfyui` and the
MCP endpoint is never itself the thing that travels inside a URL/query param.

Scopes, and why single-use is per-scope rather than global:
  launch — minted when an editor session is opened, redeemed once for the first job.
           Single-use: after the first submit it is worthless, so a leaked URL or a
           bookmarked page cannot keep spawning jobs.
  job    — minted at job creation, bound to {job_id, email}. NOT single-use, because
           the editor legitimately polls GET /toolbox/jobs/{id} many times and then
           cancels. Its power is already bounded to one job id + one user.

Replay protection for single-use scopes is an in-process seen-jti table, NOT the Store.
That is a deliberate trade-off, stated plainly: a daemon restart frees an un-redeemed
launch token until its `exp`. `exp` is minutes, the token only lets you run an image
edit as the user who already holds the page, and M1 (jobs in the Store, surviving a
restart anyway) is the place where cross-restart single-use becomes worth the extra
schema. Making it look stronger than it is would be the worse mistake here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

_VERSION = "v1"
# Domain separator: makes the signature key for tokens differ from every other use of
# the shared secret (proxy bearer, MCP bearer), so a token can never be replayed as an
# API key against another stackd route.
_KEY_LABEL = b"stackd-toolbox-v1"

DEFAULT_TTL_S = {"launch": 900, "job": 3600}
_DEFAULT_SINGLE_USE = {"launch": True, "job": False}

_MAX_SEEN = 4096  # bound the replay table's memory; oldest exp pruned past this


class TokenError(Exception):
    """Base for every rejection. Callers turn this into a 403 with `reason` — they must
    NOT distinguish "signature wrong" from "expired" in the response body beyond a short
    reason string, but the distinction matters for the server log."""


class BadToken(TokenError):
    pass


class Expired(TokenError):
    pass


class WrongScope(TokenError):
    pass


class Replay(TokenError):
    pass


class Anonymous(TokenError):
    """A token that verifies but carries no email. Never acceptable for an endpoint
    that saves/reads a user's files — identity is the whole point of the token."""


def _signing_key(secret: str) -> bytes:
    return hmac.new(_KEY_LABEL, secret.encode("utf-8"), hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# jti -> exp. In-process; see the module docstring for exactly what that costs.
_SEEN: dict[str, int] = {}

# code -> (token, exp). The transcription-safe face of a launch token; see mint_link.
_LINKS: dict[str, tuple[str, int]] = {}
_MAX_LINKS = 2048
# Alphabet with the visually ambiguous characters removed (no 0/o, 1/i/l): an operator
# may have to read a code off a screen and a model may have to re-type it verbatim.
_CODE_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
_CODE_LEN = 10


def _try_claims(body: str):
    """Decode a token body without ever raising — a probe hands us arbitrary text."""
    try:
        return json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None


def _looks_minted(payload) -> bool:
    """Does this decoded body have the shape of something we would have signed? Used
    only to make a rejection more truthful — it grants nothing, and the payload text is
    attacker-supplied so it is never echoed back to the client.

    `scope` and `email` and NOT `exp`: the live incident this serves was a transcription
    that damaged the expiry key itself (exp -> ex), so requiring exp would make the
    truthful message fire on every case except the one it was written for."""
    return (isinstance(payload, dict)
            and "scope" in payload and "email" in payload)


def mint_link(token: str) -> str:
    """Register a short opaque code that stands in for a full launch token, and return
    the code.

    WHY THIS EXISTS. A launch token is ~250 characters of base64, and the assistant-
    driven mount hands the editor link to the MODEL as chat text — models re-type long
    base64 the way a human copies a serial number off a screen. That produced a live 403
    on a token the daemon had minted 14 seconds earlier: the payload's `exp` key had
    quietly become `ex` while the signature was carried over untouched, and re-signing
    the repaired payload reproduced the presented signature byte for byte. Anything an
    LLM must reproduce verbatim has to be short, opaque and human-checkable, so the URL
    now carries ~10 characters and the credential stays in this process.

    Posture, stated rather than implied:
      * NOT single-use, by design — it may serve the editor document repeatedly until
        the token expires, which is exactly the token's own read posture (a page refresh
        before the first Render must keep working). The single-use gate stays the first
        job submit; see api.REDEEM_ON_CREATE.
      * In-process only: a daemon restart un-mints outstanding links, whereas a
        bookmarked `/toolbox/embed?token=` URL used to survive one. Accepted because
        the link is a 15-minute affordance whose failure mode is "ask for the editor
        again" — which is what the 404 says.
    """
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != _VERSION:
        raise BadToken("malformed token")
    try:
        claims = json.loads(_unb64(parts[1]))
    except (ValueError, json.JSONDecodeError):
        raise BadToken("unreadable token payload")
    if not _looks_minted(claims):
        raise BadToken("mint_link wants a token we minted (scope/email)")
    now = int(time.time())
    try:
        exp = int(claims.get("exp", 0))
    except (TypeError, ValueError):
        raise BadToken("unreadable token expiry")
    if not exp or exp <= now:
        raise Expired("token expired")
    _prune_links(now)
    while True:
        code = "".join(_CODE_ALPHABET[b % len(_CODE_ALPHABET)]
                       for b in os.urandom(_CODE_LEN))
        if code not in _LINKS:
            break
    _LINKS[code] = (token, exp)
    return code


def resolve_link(code: str) -> str | None:
    """The token behind `code`, or None. Unknown and expired answer identically: a
    probe must not learn which half it got wrong, and neither grants anything — the
    caller still verifies the returned token (signature, scope, identity, single-use)."""
    hit = _LINKS.get((code or "").strip())
    if not hit:
        return None
    token, exp = hit
    if exp <= int(time.time()):
        _LINKS.pop(code.strip(), None)
        return None
    return token


def _prune_links(now: int) -> None:
    for code, (_tok, exp) in list(_LINKS.items()):
        if exp <= now:
            _LINKS.pop(code, None)
    if len(_LINKS) > _MAX_LINKS:      # flood: keep the newest, same stance as _SEEN
        for code in sorted(_LINKS, key=lambda c: _LINKS[c][1])[: len(_LINKS) - _MAX_LINKS]:
            _LINKS.pop(code, None)


def _prune(now: int) -> None:
    expired = [j for j, exp in _SEEN.items() if exp <= now]
    for j in expired:
        _SEEN.pop(j, None)
    if len(_SEEN) > _MAX_SEEN:  # pathological flood: keep the newest
        for j in sorted(_SEEN, key=lambda k: _SEEN[k])[: len(_SEEN) - _MAX_SEEN]:
            _SEEN.pop(j, None)


def mint(secret: str, *, scope: str, email: str, ttl_s: int | None = None,
         **claims) -> str:
    """`v1.<payload>.<sig>` — payload is base64url JSON with exp/jti/scope/email plus
    whatever job/chat binding the caller passes (job_id, chat_id, message_id, kind)."""
    if not secret:
        raise ValueError("tokens.mint needs a non-empty secret")
    if scope not in DEFAULT_TTL_S:
        raise ValueError(f"unknown token scope {scope!r}")
    payload = {
        "scope": scope,
        "email": (email or "").strip().lower(),
        "exp": int(time.time()) + int(ttl_s or DEFAULT_TTL_S[scope]),
        "jti": _b64(os.urandom(9)),
    }
    payload.update({k: v for k, v in claims.items() if v is not None})
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_signing_key(secret), body.encode(), hashlib.sha256).digest())
    return f"{_VERSION}.{body}.{sig}"


def verify(secret: str, token: str, *, scope: str, now: int | None = None,
           single_use: bool | None = None) -> dict:
    """Returns the payload dict, or raises a TokenError subclass. `single_use=None`
    takes the per-scope default table above."""
    if not secret:
        raise BadToken("toolbox tokens are not configured (no shared secret)")
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != _VERSION:
        raise BadToken("malformed token")
    _, body, sig = parts
    want = _b64(hmac.new(_signing_key(secret), body.encode(), hashlib.sha256).digest())
    # compare_digest, not ==: the signature is attacker-controlled text.
    if not hmac.compare_digest(want, sig):
        # Two very different stories look identical here — a damaged link and a token
        # signed by another secret — and the old flat "bad signature" message sent an
        # operator hunting for a broken secret when the real cause was a model that had
        # re-typed the URL and dropped a character out of the payload. Say what the
        # evidence supports and name the way out; never echo the (attacker-supplied)
        # payload back to the client.
        if _looks_minted(_try_claims(body)):
            raise BadToken("signature does not match this token: the link text was "
                           "damaged in transit (a re-typed or truncated URL) or it was "
                           "minted under a different toolbox secret — ask for a fresh "
                           "editor link")
        raise BadToken("bad signature")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        raise BadToken("unreadable token payload")
    if not isinstance(payload, dict):
        raise BadToken("unreadable token payload")
    if payload.get("scope") != scope:
        raise WrongScope(f"token is for {payload.get('scope')!r}, not {scope!r}")
    now = int(time.time() if now is None else now)
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        raise BadToken("unreadable token expiry")
    if exp <= now:
        raise Expired("token expired")
    if not (payload.get("email") or "").strip():
        raise Anonymous("token carries no user identity")

    if single_use is None:
        single_use = _DEFAULT_SINGLE_USE.get(scope, True)
    if single_use:
        jti = payload.get("jti") or ""
        _prune(now)
        if not jti or jti in _SEEN:
            raise Replay("token already redeemed")
        _SEEN[jti] = exp
    return payload

