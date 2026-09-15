"""sd.cpp verb handlers for the embedded image MCP -- the sd.cpp sibling of the
ComfyUI code path in tools.py.

tools.py's generate_image / edit_image / stylize_image resolve the resident model,
then call into here when ImageSlot.engine == "sdcpp"; otherwise they run their own
ComfyUI graph-patch path unchanged. Everything engine-agnostic (dimension
resolution, prompt rewrite, the style menu, Open WebUI source fetch / result save,
timing + response shape) is REUSED from tools.py -- imported lazily inside each
handler so tools.py (which imports this module only inside its dispatch guard) and
this module don't form a module-load cycle. What differs from the ComfyUI path is
only the submit/fetch: a flat body to /sdcpp/v1/img_gen via sdcpp_client, with the
sampling recipe sourced from sdcpp_pipelines by active_model.
"""

from __future__ import annotations

import json
import logging
import random
import time

import httpx

from stackd.imagegen import (
    comfyui_client,
    openwebui_client,
    runtime,
    sdcpp_client,
    sdcpp_pipelines,
    workflows,
)

logger = logging.getLogger("stackd.imagegen")


async def generate(profile, ctx, prompt, aspect_ratio, width, height, seed,
                   rewrite_prompt, model_note, t0) -> str:
    """sd.cpp text-to-image, mirroring tools.generate_image's prep + response exactly
    but submitting an /sdcpp/v1/img_gen body instead of a ComfyUI graph."""
    from stackd.imagegen import tools  # lazy: avoids the module-load cycle
    try:
        api_key = await openwebui_client.resolve_user_api_key(ctx)
    except openwebui_client.UserNotRegisteredError as e:
        return tools._not_registered_response(e)
    if aspect_ratio and aspect_ratio.strip().lower() == "auto":
        aspect_ratio = ""
    notes: list[str] = []
    if not aspect_ratio and not width and not height:
        detected = await tools._autodetect_source_aspect(ctx, api_key)
        if detected:
            width, height, note = detected
            notes.append(note)
        else:
            aspect_ratio = "square"
    try:
        w, h, more_notes = tools._resolve_dimensions_no_source(aspect_ratio, width, height)
        notes.extend(more_notes)
    except ValueError as e:
        return _err(str(e))
    seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)
    _rw0 = time.monotonic()
    final_prompt, rw_note = await tools._maybe_rewrite(
        sdcpp_pipelines.rewrite_config(profile), prompt, mode=rewrite_prompt,
        aspect_ratio=aspect_ratio, width=w, height=h)
    rewrite_s = time.monotonic() - _rw0
    if rw_note:
        notes.append(rw_note)
    try:
        sp = sdcpp_pipelines.sample_params(profile, "generate")
    except sdcpp_pipelines.ToolUnsupported as e:
        return _err(str(e))
    body: dict = {"prompt": final_prompt, "width": w, "height": h, "seed": seed,
                  "output_format": "png", "sample_params": sp["sample_params"]}
    if sp["lora"]:
        body["lora"] = sp["lora"]
    base, base_note = runtime.sdcpp_endpoint()
    if not base:
        return _err(base_note)
    _g0 = time.monotonic()
    try:
        job = await sdcpp_client.submit(body, base=base)
        imgs = await sdcpp_client.wait_and_fetch(job, base=base)
    except httpx.HTTPError as e:
        logger.exception("generate_image: sd-server request failed")
        return _err(f"Could not reach sd-server: {e!r}")
    except (TimeoutError, RuntimeError) as e:
        logger.exception("generate_image: sd-server job did not complete")
        return _err(str(e))
    _g1 = time.monotonic()
    try:
        urls = [await openwebui_client.save_image(d, "generated-image.png", api_key) for d in imgs]
    except Exception as e:
        logger.exception("generate_image: failed to save result to Open WebUI")
        return _err(f"Generated image but failed to save it to Open WebUI: {e!r}")
    detail = f"{w}x{h}, seed {seed}."
    if not sp.get("validated", False):
        detail += " (this sdcpp pipeline is not yet live-validated on this install)"
    if notes:
        detail += " " + " ".join(notes)
    return tools._success_response(detail, urls, note=model_note,
                                   timing=tools._timing(t0, _g0, _g1, rewrite_s))


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


async def stylize(profile, ctx, style, color_treatment, season, time_of_day,
                  weather, upscale_by, seed, model_note, t0) -> str:
    """sd.cpp stylize: an img2img relight of the branch's recent image via
    init_image + strength. Reuses tools.py's style-menu validation + combined-prompt
    builder and comfyui_client's engine-agnostic PIL downscale. Submits the img2img
    body with NO custom_sigmas (sdcpp_pipelines omits them for stylize so sd.cpp
    derives its own strength schedule -- the trap-14 ghosting fix)."""
    from stackd.imagegen import tools  # lazy: avoids the module-load cycle
    style = (style or "").strip().lower()
    color_treatment = (color_treatment or "").strip().lower()
    season = (season or "").strip().lower()
    time_of_day = (time_of_day or "").strip().lower()
    weather = (weather or "").strip().lower()
    if style and style not in tools.STYLES:
        return _err(f"Unknown style '{style}'. Choose one of: {', '.join(tools.STYLES)}, or leave empty.")
    if not any((style, color_treatment, season, time_of_day, weather)):
        return _err("No adjustment specified -- set style='cinematic' and/or at least one of color_treatment/season/time_of_day/weather.")
    upscale_by = max(0.1, min(4.0, upscale_by))
    seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)
    style_prompt = tools._build_combined_prompt(
        style, color_treatment, season, time_of_day, weather, model=profile)
    try:
        sp = sdcpp_pipelines.sample_params(profile, "stylize")
    except sdcpp_pipelines.ToolUnsupported as e:
        return _err(str(e))
    try:
        api_key = await openwebui_client.resolve_user_api_key(ctx)
    except openwebui_client.UserNotRegisteredError as e:
        return tools._not_registered_response(e)
    try:
        image_url = await tools._resolve_image_url(ctx, api_key)
        source_bytes = await openwebui_client.fetch_image(image_url, api_key)
    except Exception as e:
        logger.exception("stylize_image: failed to fetch source image")
        return _err(f"Could not fetch source image: {e!r}")
    base, base_note = runtime.sdcpp_endpoint()
    if not base:
        return _err(base_note)
    source_bytes = comfyui_client.downscale_to_pixel_budget(
        source_bytes, workflows.PRACTICAL_MAX_PIXELS / (upscale_by ** 2))
    body: dict = {"prompt": style_prompt, "seed": seed, "output_format": "png",
                  "init_image": _img_to_b64(source_bytes), "sample_params": sp["sample_params"]}
    if sp["lora"]:
        body["lora"] = sp["lora"]
    if sp.get("strength") is not None:
        body["strength"] = sp["strength"]
    _g0 = time.monotonic()
    try:
        job = await sdcpp_client.submit(body, base=base)
        imgs = await sdcpp_client.wait_and_fetch(job, base=base)
    except httpx.HTTPError as e:
        logger.exception("stylize_image: sd-server request failed")
        return _err(f"Could not reach sd-server: {e!r}")
    except (TimeoutError, RuntimeError) as e:
        logger.exception("stylize_image: sd-server job did not complete")
        return _err(str(e))
    _g1 = time.monotonic()
    try:
        urls = [await openwebui_client.save_image(d, "stylized-image.png", api_key) for d in imgs]
    except Exception as e:
        logger.exception("stylize_image: failed to save result to Open WebUI")
        return _err(f"Stylized image but failed to save it to Open WebUI: {e!r}")
    applied = [x for x in (
        style,
        f"color_treatment={color_treatment}" if color_treatment else "",
        f"season={season}" if season else "",
        f"time_of_day={time_of_day}" if time_of_day else "",
        f"weather={weather}" if weather else "",
    ) if x]
    detail = "stylized (" + ", ".join(applied) + f"), seed {seed}."
    if not sp.get("validated", False):
        detail += " (this sdcpp pipeline is not yet live-validated)"
    return tools._success_response(detail, urls, note=model_note,
                                   timing=tools._timing(t0, _g0, _g1))


def _img_to_b64(data: bytes) -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(data).decode()



async def edit(profile, ctx, prompt, target_region, aspect_ratio, width, height, seed,
               edit_strength, rewrite_prompt, model_note, t0) -> str:
    """sd.cpp edit. Whole-image (target_region empty) runs an img2img edit: init_image
    + strength, sd.cpp's own derived schedule (no forced t2i sigmas -- trap 14).

    A MASKED edit (target_region set) is REFUSED here, not silently degraded: sd.cpp
    has no in-graph CLIPSeg node (that's a ComfyUI custom node), so the auto-segment
    the MCP's masked path relies on doesn't exist on this engine, and the caller
    supplies no explicit mask through these tools. The Phase-0 gate (c) validated
    sd.cpp's per-step outside-mask latent pin on a synthetic box mask (fidelity 5.81
    < the 6.85 VAE noise floor, README) but the feathered-CLIPSeg masked path is not
    wired yet. So masked edits keep using ComfyUI (the caller should select a model
    that provides them); whole-image edits run on sd.cpp."""
    from stackd.imagegen import tools  # lazy: avoids the module-load cycle
    if (target_region or "").strip():
        return _err(
            "masked edit (target_region) is not available on the sd.cpp engine: it "
            "has no in-graph CLIPSeg segmentation node and these tools supply no "
            "explicit mask. Ask for a ComfyUI-backed model (e.g. flux2-klein / "
            "flux2-dev-turbo) for a masked edit, or omit target_region for a "
            "whole-image edit on sd.cpp."
        )
    if not (prompt or "").strip():
        return _err("prompt is required (no chat-history fallback available over MCP).")
    try:
        api_key = await openwebui_client.resolve_user_api_key(ctx)
    except openwebui_client.UserNotRegisteredError as e:
        return tools._not_registered_response(e)
    try:
        image_url = await tools._resolve_image_url(ctx, api_key)
        source_bytes = await openwebui_client.fetch_image(image_url, api_key)
    except Exception as e:
        logger.exception("edit_image: failed to fetch source image")
        return _err(f"Could not fetch source image: {e!r}")
    try:
        w, h, _notes = tools._resolve_dimensions_from_source(source_bytes, aspect_ratio, width, height)
    except Exception as e:
        return _err(str(e))
    seed = seed if seed >= 0 else random.randint(0, 2**31 - 1)
    _rw0 = time.monotonic()
    final_prompt, rw_note = await tools._maybe_rewrite(
        sdcpp_pipelines.rewrite_config(profile), prompt, mode=rewrite_prompt,
        aspect_ratio="", width=w, height=h)
    rewrite_s = time.monotonic() - _rw0
    try:
        sp = sdcpp_pipelines.sample_params(profile, "edit")
    except sdcpp_pipelines.ToolUnsupported as e:
        return _err(str(e))
    base, base_note = runtime.sdcpp_endpoint()
    if not base:
        return _err(base_note)
    source_bytes = comfyui_client.downscale_to_exact_size(source_bytes, w, h)
    body: dict = {"prompt": final_prompt, "width": w, "height": h, "seed": seed,
                  "output_format": "png", "init_image": _img_to_b64(source_bytes),
                  "sample_params": sp["sample_params"]}
    if sp["lora"]:
        body["lora"] = sp["lora"]
    # explicit edit_strength from the caller wins over the manifest's per-verb default,
    # matching how the ComfyUI edit path honours the tool's edit_strength arg.
    body["strength"] = edit_strength if edit_strength is not None else sp.get("strength")
    _g0 = time.monotonic()
    try:
        job = await sdcpp_client.submit(body, base=base)
        imgs = await sdcpp_client.wait_and_fetch(job, base=base)
    except httpx.HTTPError as e:
        logger.exception("edit_image: sd-server request failed")
        return _err(f"Could not reach sd-server: {e!r}")
    except (TimeoutError, RuntimeError) as e:
        logger.exception("edit_image: sd-server job did not complete")
        return _err(str(e))
    _g1 = time.monotonic()
    try:
        urls = [await openwebui_client.save_image(d, "edited-image.png", api_key) for d in imgs]
    except Exception as e:
        logger.exception("edit_image: failed to save result to Open WebUI")
        return _err(f"Edited image but failed to save it to Open WebUI: {e!r}")
    detail = f"whole-image edit {w}x{h}, strength {body['strength']}, seed {seed}."
    if not sp.get("validated", False):
        detail += " (this sdcpp pipeline is not yet live-validated)"
    if rw_note:
        detail += " " + rw_note
    return tools._success_response(detail, urls, note=model_note,
                                   timing=tools._timing(t0, _g0, _g1, rewrite_s))

    return json.dumps({"error": msg})
