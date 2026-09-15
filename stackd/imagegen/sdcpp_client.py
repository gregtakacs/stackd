"""Async HTTP client for a resident stable-diffusion.cpp ``sd-server``.

The sd.cpp analog of :mod:`stackd.imagegen.comfyui_client`. Where that one POSTs a
ComfyUI API-format graph to ``/prompt`` and polls ``/history``, this one POSTs a flat
body to ``POST /sdcpp/v1/img_gen`` and polls ``GET /sdcpp/v1/jobs/{id}``, then decodes
``result.images[].b64_json`` (the documented shape, api.md:868 -- a LIST of
{index, b64_json}; reading ``result.b64_json`` instead is the bogus "completed with no
image" trap the spike hit and this client guards against explicitly).

``base`` (the sd-server base URL) is a required argument on every network call, threaded
once per generation from runtime.sdcpp_endpoint() (Manager.comfyui_target()/capabilities()
for the resident engine), same contract as comfyui_client.base. The container sits on the
internal Docker network with no auth, so no bearer token.

Stdlib + httpx only (same deps comfyui_client already pulls).
"""

from __future__ import annotations

import asyncio
import base64
import time

try:
    import httpx
except ImportError:  # the [imagegen] extra; only the async funcs need it, so the pure
    httpx = None       # image-decoding helper stays importable for offline unit checks

from stackd.imagegen import config



def _headers() -> dict:
    return {"Content-Type": "application/json"}


async def _post_json(url: str, body: dict, timeout: int = 30) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _get_json(url: str, timeout: int = 30) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def submit(params: dict, base: str) -> str:
    """POST /sdcpp/v1/img_gen -> job id (accepts id or job_id, as the spike did --
    the field name has moved across sd.cpp versions and guessing wrong reads as a
    silent empty submission)."""
    r = await _post_json(f"{base.rstrip('/')}/sdcpp/v1/img_gen", params, timeout=30)
    return r.get("id") or r.get("job_id") or ""


def _extract_images(result: dict) -> list[bytes]:
    """Decode result.images[] -> raw PNG bytes. result.images is a LIST of
    {index, b64_json} (documented) but tolerate plain strings and a top-level
    b64_json too, so an sd.cpp shape change degrades to a clear error, not a
    'completed with no image' false-negative while the sampler actually ran."""
    out: list[bytes] = []
    for it in (result.get("images") or []):
        b64 = it.get("b64_json") if isinstance(it, dict) else (it if isinstance(it, str) else None)
        if b64:
            if "," in b64[:64]:          # tolerate a data: URL prefix
                b64 = b64.split(",", 1)[1]
            out.append(base64.b64decode(b64))
    if not out and result.get("b64_json"):
        out.append(base64.b64decode(result["b64_json"]))
    return out


async def wait_and_fetch(job_id: str, base: str, *, timeout: int | None = None) -> list[bytes]:
    """Poll GET /sdcpp/v1/jobs/{id} until it reaches a terminal state, then return the
    decoded image bytes. Raises TimeoutError past config.TIMEOUT_S, RuntimeError on a
    server-reported failure (carrying the engine's own error text)."""
    base = base.rstrip("/")
    timeout = timeout or config.TIMEOUT_S
    deadline = time.monotonic() + timeout
    path = f"{base}/sdcpp/v1/jobs/{job_id}"
    while True:
        j = await _get_json(path, timeout=30)
        status = j.get("status")
        if status in ("completed", "failed", "cancelled"):
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out after {timeout}s waiting for sd-server job {job_id}.")
        await asyncio.sleep(config.POLL_INTERVAL_S)
    if status != "completed":
        raise RuntimeError(f"sd-server job {job_id} {status}: {j.get('error') or status}")
    images = _extract_images(j.get("result") or {})
    if not images:
        # Keep the keys, not a guess -- this is the exact false-negative the spike
        # documented (a full 7/7 run that looked like a failure because the reader
        # looked in result.b64_json instead of result.images[]).
        raise RuntimeError(
            f"sd-server job {job_id} completed but returned no image "
            f"(result keys: {sorted((j.get('result') or {}).keys())})"
        )
    return images
