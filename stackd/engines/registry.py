from __future__ import annotations

from stackd.engines.base import EngineAdapter
from stackd.engines.comfyui import ComfyuiAdapter
from stackd.engines.llamacpp import LlamacppCudaAdapter, LlamacppVulkanAdapter
from stackd.engines.sglang_pennyroyal import SglangPennyroyalAdapter
from stackd.engines.vllm import VllmCudaAdapter

TEMPLATES: dict[str, type[EngineAdapter]] = {
    cls.template: cls
    for cls in (
        LlamacppCudaAdapter,
        LlamacppVulkanAdapter,
        VllmCudaAdapter,
        SglangPennyroyalAdapter,
        ComfyuiAdapter,
    )
}


def adapter_for(model_spec, device) -> EngineAdapter:
    return TEMPLATES[model_spec.engine.template](model_spec, device)
