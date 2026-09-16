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

