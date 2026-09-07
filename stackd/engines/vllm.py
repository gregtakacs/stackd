from __future__ import annotations

from dataclasses import dataclass

from stackd.config.models import Budget
from stackd.engines.base import EngineAdapter
from stackd.runner import LaunchContext, LaunchSpec


@dataclass
class VllmParams:
    # The name vLLM advertises / accepts in `body["model"]`. Optional: stackd
    # defaults it to the stack name and injects `--served-model-name` into the
    # launch command itself, so it never has to be repeated in cmd_extra.
    served_model_name: str | None = None
    port: int = 8000
    backend_url: str | None = None       # override; default http://<container>:<port>
    gpu_memory_utilization: float = 0.9
    max_model_len: int | None = None
    # {caller_effort: engine_effort} — the HTTP front rewrites a request's
    # `reasoning_effort` through this before forwarding, so a client using the
    # OpenAI vocabulary (none/minimal/low/medium/high) lands on whatever this
    # build actually accepts. A value not listed here passes through untouched;
    # omit the whole key to disable rewriting. See serve._map_reasoning_effort.
    reasoning_effort_map: dict[str, str] | None = None


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

    def served_name(self) -> str:
        return self.params.served_model_name or self.spec.name

    def launch_spec(self, ctx: LaunchContext) -> LaunchSpec:
        spec = self._base_spec(ctx)
        cmd = list(self.spec.engine.container.cmd_extra)
        # stackd owns --served-model-name (== served_name(), which the HTTP front
        # rewrites body["model"] to). Drop any copy in cmd_extra, then set ours.
        out: list[str] = []
        i = 0
        while i < len(cmd):
            if cmd[i] == "--served-model-name":
                i += 2
                continue
            out.append(cmd[i])
            i += 1
        out += ["--served-model-name", self.served_name()]
        spec.cmd = out
        spec.health_url = self._url().rstrip("/") + "/health"
        spec.ready_timeout_s = 900.0  # torch.compile + cudagraph capture, first cold boot
        return spec
