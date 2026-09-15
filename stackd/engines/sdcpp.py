from __future__ import annotations

from dataclasses import dataclass, field

from stackd.config.models import Budget
from stackd.engines.base import DescribeResult, EngineAdapter, EngineState
from stackd.runner import LaunchContext, LaunchSpec


# sd-server (stable-diffusion.cpp's HTTP server) listens on this port when stackd
# manages it. ComfyUI uses 8188 for the same reason: only ONE image-engine container
# is ever resident (the elastic tier runs one), so comfyui and sd.cpp coexist without
# ever binding the same port on the same box — they are never up together. The spike
# published :12345->:1234 for host-side curl; under stackd there is no host publish
# (engines are reached by container name on the shared docker network), so the port
# only has to agree between --listen-port and the health URL, which it does here.
SDCPP_PORT = 8188


@dataclass
class SdcppParams:
    """Knobs the sdcpp engine adapter renders into an sd-server command line.

    The per-checkpoint *paths* (diffusion / text-encoder / vae / lora dir) and the
    pipeline's sampling recipe do NOT live here — they are resolved from
    imagegen/sdcpp_pipelines.json by `active_model` at launch time, exactly as
    ComfyUI's per-verb graphs are resolved from workflow_graphs/models.json by
    active_model per request. That keeps this manifest the single source of truth:
    loading a checkpoint into sd.cpp selects weights at server START (not per
    request the way ComfyUI swaps a checkpoint inside one running process), so which
    pipeline is resident is a launch decision sourced from the same table the
    capability check reads."""

    active_model: str
    port: int = SDCPP_PORT
    base_url: str | None = None
    # what the resident pipeline can do -- generate | stylize | edit. Cross-checked
    # at load against imagegen/sdcpp_pipelines.json (config/models.py::
    # _missing_capability_sources), the sd.cpp analog of the ComfyUI graph check.
    capabilities: list[str] = field(default_factory=list)
    kind: str = "image"          # "image" | "video"
    # container-side mount root the model paths resolve under. NOT /models: that
    # container path is already claimed globally for the LLM checkpoint tree
    # (engines/base.py._base_spec's ctx.extra_mounts), so a container that also
    # tries to mount its own /models gets refused outright by the Docker API
    # ("Duplicate mount point: /models", found live 2026-09-15 on the first real
    # activation attempt) -- the same reason comfyui-cuda/-rocm mount their tree at
    # /root|/opt/ComfyUI/models instead of bare /models. Must match the container
    # block's mount and sdcpp_pipelines.json's `models_dir`.
    models_dir: str = "/comfyui-models"
    # staged-to-disk backends: the whole point of running dev-turbo on the iGPU is
    # that the 33 GB text encoder is prepared -> used -> released rather than pinned
    # (spike Q5: 8.08 GB cgroup peak vs ComfyUI's 121.6 GB). diffusion=disk,te=disk
    # is what made that measurement possible; it is the default, not an experiment.
    params_backend: str = "diffusion=disk,te=disk"


class SdcppAdapter(EngineAdapter):
    """stable-diffusion.cpp (the sd-server HTTP engine), a selectable PEER of ComfyUI
    on the elastic image tier. Like ComfyuiAdapter it is not a model server in the
    llamacpp/vllm sense: describe() is the *intended* capability set, intersected with
    live state by the MCP. Renders the sd-server argv the Phase-0 spike verified on
    the Strix Halo iGPU (HIPBLAS, --diffusion-fa, weights staged to disk)."""

    template = "sdcpp"
    Params = SdcppParams
    default_devices = "amd"
    # stackd's iGPU device backend is "vulkan" even for the ROCm/HIPBLAS build (the
    # device_profiles.vulkan block supplies the /dev/dri render node + video/render
    # GIDs; the container block adds /dev/kfd, same as comfyui-rocm). cuda/cpu kept so
    # the same engine can be benched there later without a second template.
    backends = ("cuda", "vulkan", "rocm", "cpu")

    def vram_estimate(self) -> Budget:
        return self.spec.engine.budget

    def _url(self) -> str:
        return self.params.base_url or f"http://{self.container_name()}:{self.params.port}"

    def endpoint(self, port: int | None = None) -> str | None:
        return self._url()

    def render_args(self) -> list[str]:
        """The sd-server argv, sourced from imagegen/sdcpp_pipelines.json for the
        resident active_model. A pure method (no LaunchContext) so it is unit-testable
        without a device, and so a missing/renamed checkpoint is a clear config error
        at spawn rather than a container that starts and 500s every request."""
        from stackd.imagegen import sdcpp_pipelines

        p = sdcpp_pipelines.pipeline_args(self.params.active_model,
                                           models_dir=self.params.models_dir)
        args: list[str] = [
            "--diffusion-model", p["diffusion_model"],
            "--vae", p["vae"],
            "--llm", p["text_encoder"],
        ]
        if p.get("lora_model_dir"):
            args += ["--lora-model-dir", p["lora_model_dir"]]
        args += [
            "--listen-ip", "0.0.0.0",
            "--listen-port", str(self.params.port),
            "--params-backend", self.params.params_backend,
            # flash-attention was the 2.9x HIPBLAS-vs-Vulkan lever (spike Q6/6b);
            # passed explicitly so a regression to the slow path is a config change,
            # never a silent default flip.
            "--diffusion-fa",
        ]
        args += list(p.get("extra_args") or [])
        return args

    def launch_spec(self, ctx: LaunchContext) -> LaunchSpec:
        spec = self._base_spec(ctx)
        # container.cmd_extra lets an operator append raw flags without a code change;
        # it comes AFTER the manifest-rendered argv so an explicit override there is
        # the last word (sd-server honours the later --listen-port).
        spec.cmd = [*self.render_args(), *self.spec.engine.container.cmd_extra]
        # /sdcpp/v1/capabilities is a cheap no-inference GET (routes_sdcpp.cpp) that
        # only answers once the model manager has finished loading -- a real readiness
        # gate, and the same probe the spike used to confirm the route is wired.
        spec.health_url = self._url().rstrip("/") + "/sdcpp/v1/capabilities"
        # the 33 GB TE is read from disk and prepared before this answers; the spike
        # saw cold first-load on the order of minutes on this box's NVMe.
        spec.ready_timeout_s = 1800.0
        return spec

    def describe(self, state=EngineState.down, port=None) -> DescribeResult:
        return DescribeResult(
            state=state,
            active_model=self.params.active_model,
            served=list(self.params.capabilities),
            endpoint=self._url(),
            extra={"kind": self.params.kind, "advertiser": "stackd-imagegen",
                   "engine": self.template},
        )
