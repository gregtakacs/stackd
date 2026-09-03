"""
Shared async HTTP client for talking to a ComfyUI instance -- one place for the
post_json/get_json/get_bytes/upload_to_comfy/wait_and_fetch calls generate_image/
edit_image/stylize_image all need.

`base` (the ComfyUI base URL) is a required argument on every network call. The caller
resolves it once per generation from `runtime.comfyui_endpoint()` (stackd's
`Manager.comfyui_target()` -- whichever of comfyui-cuda / comfyui-rocm the active
profile has resident) and threads the same value through submit -> poll -> fetch. The
ComfyUI containers sit on the internal Docker network with no auth, so no bearer token.
"""

import asyncio
import io
import time
import urllib.parse
import uuid

import httpx

from stackd.imagegen import config

try:
    from PIL import Image
except ImportError:
    Image = None


def _headers(extra: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    return headers


async def post_json(url: str, body: dict, timeout: int = 30) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_json(url: str, timeout: int = 30) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def get_bytes(url: str, timeout: int = 30) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        return resp.content


async def reachable(base: str, timeout: int = 5) -> bool:
    """True if GET {base}/system_stats answers 200."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base.rstrip('/')}/system_stats", headers=_headers())
            return resp.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


# -----------------------------------------------------------------------------
# Pre-upload downscale -- exists because ComfyUI's LoadImage decodes the
# full-resolution source into a tensor before any downstream resize node gets a
# chance to shrink it -- a large phone photo (e.g. 4000x6000) crashed generation
# from the VRAM/RAM spike of decoding and caching that oversized tensor, worse
# here since VRAM is shared with llama-cpp via the proxy's coexistence
# scheduling. Both variants below only downscale (never upscale a smaller
# source) and fall back to the original bytes untouched if PIL isn't importable.
# -----------------------------------------------------------------------------


def downscale_to_exact_size(image_bytes: bytes, width: int, height: int) -> bytes:
    """edit_image's variant: stretches directly to (width, height), matching ComfyUI's
    own ImageScale(crop="disabled") semantics exactly (confirmed against ComfyUI's
    comfy/utils.py -- crop="disabled" stretches with no aspect-ratio preservation), so
    this is a pure relocation of work the graph's own ImageScale node was always going
    to do anyway, not a behavior change."""
    if Image is None:
        return image_bytes
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.width <= width and img.height <= height:
            return image_bytes
        resized = img.convert("RGB").resize((width, height), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue()


def downscale_to_pixel_budget(image_bytes: bytes, max_pixels: int) -> bytes:
    """stylize_image's variant: uniform scale-both-dimensions-equally resize (aspect-
    ratio-preserving), matching ImageScaleBy's own behavior (it scales both dimensions
    by one factor, unlike edit_image's ImageScale which stretches to an explicit
    width/height). Rounds DOWN to the nearest 16 (Flux.2 Klein 9B requires multiples of
    16) -- rounding to nearest can push slightly back over budget."""
    if Image is None:
        return image_bytes
    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.width * img.height <= max_pixels:
            return image_bytes
        scale = (max_pixels / (img.width * img.height)) ** 0.5
        new_w = max(16, int(img.width * scale // 16) * 16)
        new_h = max(16, int(img.height * scale // 16) * 16)
        resized = img.convert("RGB").resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue()


# -----------------------------------------------------------------------------
# Post-edit sanity check -- catches a target_region edit_image call that ComfyUI reports
# as successfully completed even though the described subject was never actually
# localized. Root-caused against a real failure (chat b345f0bb...: asked to remove "the
# woman with her back to camera wearing a black backpack" -- the resulting image was,
# pixel for pixel, the same person still fully present) -- the referring-expression
# segmentation (Florence2Run) can fail to localize the described subject and produce a
# near-empty mask, and ComfyUI's workflow still completes "successfully" in that case.
#
# ORIGINAL implementation compared the final output image against the source (pixel-diff
# at a downsampled analysis size) and rejected below a fixed changed-pixel-fraction floor.
# Replaced after a second confirmed failure (chat 2cb5ddc1...) showed this was
# fundamentally unreliable for small/blurry/distant content: three reproductions of the
# same real photo and same-style target_region (a small, out-of-focus text sign on a
# building facade) produced mask coverage within a tight, consistent band (1.41%-2.80%)
# across DIFFERENT random KSampler seeds -- confirming segmentation itself was working
# correctly and consistently every time -- yet the resulting pixel-diff varied enough
# (0.187%-0.618%) that one of the three, despite a clearly legible, correct text swap
# confirmed by visual inspection, still landed under the pixel-diff floor and got wrongly
# rejected. Root cause: Florence2Run's own seed is a FIXED constant in the workflow graph
# (unlike the KSampler's, which is randomized per call) -- segmentation is deterministic
# and reliable, but pixel-diff conflates that stable signal with the KSampler's unrelated,
# highly variable rendering randomness, which is especially pronounced for thin/blurred/
# small content like distant signage text. Mask coverage isolates the actually-reliable
# signal instead: did Florence2Run localize SOMETHING non-trivial, independent of how much
# the sampler's random seed happened to visually change those pixels this particular run.
# -----------------------------------------------------------------------------

_MASK_COVERAGE_BRIGHTNESS_THRESHOLD = 50  # 0-255 grayscale value counted as "masked"
# Below this fraction of the mask canvas being "masked", treat as segmentation having
# failed to localize anything real. Confirmed real, correctly-located, successful masks
# measure 1.41%-2.80% (see module comment above) -- this floor sits well below that with
# large margin, since GrowMask's mandatory margin expansion (12-28px, see workflows.py)
# means even a small genuine detection ends up with non-trivial coverage after growth.
_MASK_COVERAGE_FLOOR = 0.003


def mask_indicates_nothing_found(mask_bytes: bytes) -> bool:
    """True if the Final Mask Preview (workflows.MASK_PREVIEW_NODE) looks empty/near-empty
    -- i.e. Florence2Run's segmentation likely failed to localize target_region at all. See
    the module comment above for why this replaced a pixel-diff-based check. Fails open
    (returns False, "assume it's fine") if PIL isn't available or the image fails to
    decode -- this is a best-effort safety net, not something that should ever mask a real
    error from a genuinely broken image."""
    if Image is None:
        return False
    try:
        with Image.open(io.BytesIO(mask_bytes)) as img:
            g = img.convert("L")
            masked = sum(1 for px in g.getdata() if px > _MASK_COVERAGE_BRIGHTNESS_THRESHOLD)
            total = g.size[0] * g.size[1]
    except Exception:
        return False
    return (masked / total) < _MASK_COVERAGE_FLOOR


# -----------------------------------------------------------------------------
# Upload -- unique filename per call: a fixed filename with overwrite=true meant
# two requests close together could overwrite each other's source file on disk
# before the earlier one's LoadImage node had actually read it, silently
# editing the wrong image (confirmed by direct observation). A unique filename
# per call makes every request's source image independent regardless of timing.
# -----------------------------------------------------------------------------


async def upload_to_comfy(image_bytes: bytes, filename_prefix: str, base: str) -> str:
    filename = f"{filename_prefix}_{uuid.uuid4().hex}.png"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base.rstrip('/')}/upload/image",
            files={"image": (filename, image_bytes, "image/png")},
            data={"overwrite": "true"},
        )
        resp.raise_for_status()
        return resp.json()["name"]


async def submit_workflow(workflow: dict, base: str) -> str:
    result = await post_json(
        f"{base.rstrip('/')}/prompt", {"prompt": workflow}, timeout=30
    )
    return result["prompt_id"]


async def wait_and_fetch(
    prompt_id: str, include_node_ids: set[str], base: str
) -> dict[str, list[bytes]]:
    """Polls /history until the prompt completes, then downloads every image any of
    include_node_ids produced. Returns images keyed by node id so callers can tell a
    real output apart from e.g. a debug mask preview with certainty, instead of relying
    on dict/list ordering ComfyUI doesn't guarantee."""
    base = base.rstrip("/")
    deadline = time.monotonic() + config.TIMEOUT_S
    while True:
        history = await get_json(f"{base}/history/{prompt_id}", timeout=30)
        if prompt_id in history:
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out after {config.TIMEOUT_S}s waiting for ComfyUI.")
        await asyncio.sleep(config.POLL_INTERVAL_S)

    images_by_node: dict[str, list[bytes]] = {node_id: [] for node_id in include_node_ids}
    outputs = history[prompt_id].get("outputs", {})
    for node_id, node_out in outputs.items():
        if node_id not in include_node_ids:
            continue
        for img in node_out.get("images", []):
            params = urllib.parse.urlencode(
                {
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                }
            )
            data = await get_bytes(f"{base}/view?{params}", timeout=30)
            images_by_node[node_id].append(data)

    # Result is in hand -- drop this history entry now, so the comfyui-cleaner
    # backstop never has to (and can never race a still-polling fetch by
    # blanket-clearing /history, which silently timed generations out on
    # 2026-09-02). Best-effort; ComfyUI's POST /history takes {"delete": [...]}.
    try:
        await post_json(f"{base}/history", {"delete": [prompt_id]}, timeout=10)
    except Exception:
        pass
    return images_by_node
