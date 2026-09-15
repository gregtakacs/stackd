"""Pipeline registry for the stable-diffusion.cpp (sd-server) image engine.

The sd.cpp analog of ``workflows.py`` (which drives ComfyUI). Where ComfyUI
patches a per-verb API-format graph by node id, sd.cpp takes a flat JSON body
posted to ``POST /sdcpp/v1/img_gen``; the per-verb ``tools`` block here is that
body's sampling recipe, and the top-level ``diffusion_model``/``vae``/
``text_encoder``/``lora_model_dir`` are the server-start weights the
:mod:`stackd.engines.sdcpp` adapter renders into ``sd-server`` argv.

Single source of truth, same discipline as workflow_graphs/models.json: config
validation (config/models.py::_check_media_tier -> _missing_capability_sources)
cross-checks each ``prefer[]`` capability against the verbs declared here, so a
ladder row can never advertise a verb the client doesn't actually know how to
send. Stdlib only.
"""

from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_MANIFEST = os.path.join(_HERE, "sdcpp_pipelines.json")


class UnknownPipeline(ValueError):
    pass


class ToolUnsupported(ValueError):
    """pipeline exists but doesn't offer this verb (generate | stylize | edit)."""


def _load() -> dict:
    with open(_MANIFEST) as f:
        return json.load(f)


PIPELINES = _load()


def model_names() -> list[str]:
    return list(PIPELINES.get("models", {}).keys())


def default_model_name() -> str:
    return PIPELINES.get("default") or (model_names()[0] if model_names() else "")


def entry_for(active_model: str) -> dict:
    m = (PIPELINES.get("models") or {}).get(active_model)
    if m is None:
        raise UnknownPipeline(
            f"Unknown sdcpp pipeline '{active_model}'. "
            f"Available: {', '.join(model_names()) or '(none)'}."
        )
    return m


def tools_for(active_model: str) -> set[str]:
    """The verbs with a `tools` block for this pipeline. `_`-prefixed keys are notes
    and ignored. Mirrors workflows.tools_for so config/models.py can validate an
    sdcpp prefer[] row exactly as it does a comfyui one."""
    entry = (PIPELINES.get("models") or {}).get(active_model) or {}
    return {k for k in (entry.get("tools") or {}) if not k.startswith("_")}


def resolve_model_name(model: str | None, fallback: str | None = None) -> str:
    name = (model or fallback or "").strip() or default_model_name()
    if name not in (PIPELINES.get("models") or {}):
        raise UnknownPipeline(
            f"Unknown sdcpp image model '{name}'. Available: {', '.join(model_names())}."
        )
    return name


def _resolve_entry(model: str | None, fallback: str | None = None) -> tuple[str, dict]:
    name = resolve_model_name(model, fallback)
    return name, entry_for(name)


def rewrite_config(model: str | None, fallback: str | None = None) -> dict:
    """The pipeline's prompts/*.txt rewrite settings, or {} if it declares none."""
    _, entry = _resolve_entry(model, fallback)
    return dict(entry.get("prompt_rewrite") or {})



def _join(models_dir: str, rel: str) -> str:
    if not rel:
        return rel
    if rel.startswith("/"):            # already absolute inside the container
        return rel
    return f"{models_dir.rstrip('/')}/{rel}"


def pipeline_args(active_model: str, *, models_dir: str | None = None) -> dict:
    """Absolute-in-container paths + extra flags the SdcppAdapter renders into
    sd-server argv. ``models_dir`` defaults to the entry's own ``models_dir`` then
    /models. Raises UnknownPipeline for an unregistered model, and ValueError when a
    required checkpoint is missing -- so a bad config is a clear spawn-time error."""
    entry = entry_for(active_model)
    md = models_dir or entry.get("models_dir") or "/models"
    out = {
        "diffusion_model": _join(md, entry.get("diffusion_model") or ""),
        "vae": _join(md, entry.get("vae") or ""),
        "text_encoder": _join(md, entry.get("text_encoder") or ""),
    }
    if entry.get("lora_model_dir"):
        out["lora_model_dir"] = _join(md, entry["lora_model_dir"])
    out["extra_args"] = list(entry.get("extra_args") or [])
    for key in ("diffusion_model", "vae", "text_encoder"):
        if not out[key]:
            raise ValueError(
                f"sdcpp pipeline {active_model!r}: missing required {key!r} in sdcpp_pipelines.json"
            )
    return out


def sample_params(active_model: str, tool: str) -> dict:
    """The per-verb sampling block for the /sdcpp/v1/img_gen body plus the lora list
    and any strength / ref_image_args. Raises ToolUnsupported when this pipeline
    doesn't declare `tool`. custom_sigmas present => passed through verbatim (the
    t2i turbo schedule); absent/null => OMITTED so sd.cpp derives its own strength
    schedule for img2img (forcing the t2i sigmas there is the ghosting trap, README 14).
    """
    entry = entry_for(active_model)
    spec = (entry.get("tools") or {}).get(tool)
    if spec is None:
        raise ToolUnsupported(
            f"sdcpp pipeline {active_model!r} does not support {tool!r} "
            f"(declares: {', '.join(sorted(tools_for(active_model))) or 'none'})."
        )
    sp: dict = {
        "sample_steps": int(spec.get("sample_steps", 8)),
        "sample_method": spec.get("sample_method", "euler"),
        "guidance": {
            "txt_cfg": float(spec.get("txt_cfg", 1.0)),
            "distilled_guidance": float(spec.get("guidance", 3.5)),
        },
    }
    cs = spec.get("custom_sigmas")
    if cs is not None:
        sp["custom_sigmas"] = list(cs)
    loras = spec.get("loras")
    lora = spec.get("lora")
    if loras:
        lora_list = [dict(x) for x in loras]
    elif lora:
        lora_list = [{"path": lora, "multiplier": float(spec.get("lora_multiplier", 1.0))}]
    else:
        lora_list = []
    return {
        "sample_params": sp,
        "lora": lora_list,
        "strength": spec.get("strength"),
        "ref_image_args": spec.get("ref_image_args"),
        "validated": bool(entry.get("validated", False)) and bool(spec.get("validated", True)),
    }
