from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stackd.config.models import Budget
from stackd.engines.base import DescribeResult, EngineAdapter, EngineState
from stackd.runner import LaunchContext, LaunchSpec

# added by stackd to every llama-server
_SERVER_FLAGS = ["--flash-attn", "on", "--kv-unified", "--cont-batching",
                 "--no-context-shift", "--metrics"]


@dataclass
class LlamacppParams:
    ctx: int
    parallel: int = 1
    n_gpu_layers: int = 99
    extra_args: list[str] = field(default_factory=list)
    sampling: dict[str, Any] = field(default_factory=dict)


class _LlamacppBase(EngineAdapter):
    Params = LlamacppParams

    def vram_estimate(self) -> Budget:
        return self.spec.engine.budget

    def endpoint(self, port: int | None = None) -> str | None:
        p = port or self.spec.engine.container.port
        return f"http://{self.container_name()}:{p}" if p else None

    def launch_spec(self, ctx: LaunchContext) -> LaunchSpec:
        p: LlamacppParams = self.params
        port = ctx.port or self.spec.engine.container.port or 8080
        cmd = [
            "--model", f"/models/{self.spec.engine.model}.gguf",   # runtime.mounts puts it here
            "--host", "0.0.0.0", "--port", str(port),
            "--ctx-size", str(p.ctx), "--parallel", str(p.parallel),
            "--n-gpu-layers", str(p.n_gpu_layers),
            *_SERVER_FLAGS,
        ]
        for key, val in p.sampling.items():
            cmd += [f"--{key.replace('_', '-')}", str(val)]
        cmd += list(p.extra_args)

        spec = self._base_spec(ctx)   # entrypoint: from container.entrypoint, else the image's own
        spec.cmd = cmd + list(self.spec.engine.container.cmd_extra)
        spec.health_url = f"http://{self.container_name()}:{port}/health"
        spec.ready_timeout_s = 300.0
        return spec

    def describe(self, state=EngineState.down, port=None) -> DescribeResult:
        return DescribeResult(
            state=state,
            active_model=self.spec.engine.model,
            served=[s.api_name for s in self.spec.serves],
            endpoint=self.endpoint(port),
        )


class LlamacppCudaAdapter(_LlamacppBase):
    template = "llamacpp-cuda"
    default_devices = "nvidia"
    backends = ("cuda",)


class LlamacppVulkanAdapter(_LlamacppBase):
    template = "llamacpp-vulkan"
    default_devices = "rocm"
    backends = ("vulkan", "rocm")
