from __future__ import annotations

from dataclasses import dataclass

from stackd.config.models import Budget
from stackd.engines.base import EngineAdapter
from stackd.runner import LaunchContext, LaunchSpec


@dataclass
class VllmParams:
    served_model_name: str
    port: int = 8000
    backend_url: str | None = None       # override; default http://<container>:<port>
    gpu_memory_utilization: float = 0.9
    max_model_len: int | None = None


class VllmCudaAdapter(EngineAdapter):
    """vLLM's own args are long and stack-specific — they live in
    `engine.container.cmd_extra`; stackd just supplies image/mounts/env/network."""

    template = "vllm-cuda"
    Params = VllmParams
    default_devices = "nvidia"
    backends = ("cuda",)

    def vram_estimate(self) -> Budget:
        return self.spec.engine.budget

    def _url(self) -> str:
        return self.params.backend_url or f"http://{self.container_name()}:{self.params.port}"

    def endpoint(self, port: int | None = None) -> str | None:
        return self._url()

    def launch_spec(self, ctx: LaunchContext) -> LaunchSpec:
        spec = self._base_spec(ctx)
        spec.cmd = list(self.spec.engine.container.cmd_extra)
        spec.health_url = self._url().rstrip("/") + "/health"
        spec.ready_timeout_s = 900.0  # torch.compile + cudagraph capture, first cold boot
        return spec
