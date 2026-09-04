from __future__ import annotations

from dataclasses import dataclass, field

from stackd.config.models import Budget
from stackd.engines.base import DescribeResult, EngineAdapter, EngineState
from stackd.runner import LaunchContext, LaunchSpec


@dataclass
class ComfyuiParams:
    active_model: str
    port: int = 8188
    base_url: str | None = None
    # what the resident model can do -- generate | stylize | edit (video verbs
    # later). The MCP gates each tool on this list; the loader cross-checks that
    # every verb here has a workflow graph for `active_model` in
    # imagegen/workflow_graphs/models.json.
    capabilities: list[str] = field(default_factory=list)
    kind: str = "image"          # "image" | "video"


class ComfyuiAdapter(EngineAdapter):
    """Not a model server. `describe()` is the *intended* capability set;
    `comfyui-mcp` intersects it with ComfyUI's live state and advertises (D4)."""

    template = "comfyui"
    Params = ComfyuiParams
    default_devices = "nvidia"
    backends = ("cuda", "vulkan", "rocm")

    def vram_estimate(self) -> Budget:
        return self.spec.engine.budget

    def _url(self) -> str:
        return self.params.base_url or f"http://{self.container_name()}:{self.params.port}"

    def endpoint(self, port: int | None = None) -> str | None:
        return self._url()

    def launch_spec(self, ctx: LaunchContext) -> LaunchSpec:
        spec = self._base_spec(ctx)
        spec.cmd = list(self.spec.engine.container.cmd_extra)
        spec.health_url = self._url().rstrip("/") + "/system_stats"
        spec.ready_timeout_s = 180.0
        return spec

    def describe(self, state=EngineState.down, port=None) -> DescribeResult:
        return DescribeResult(
            state=state,
            active_model=self.params.active_model,
            served=list(self.params.capabilities),
            endpoint=self._url(),
            extra={"kind": self.params.kind, "advertiser": "comfyui-mcp"},
        )
