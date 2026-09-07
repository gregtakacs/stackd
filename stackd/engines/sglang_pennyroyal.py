from __future__ import annotations

from dataclasses import dataclass

from stackd.config.models import Budget
from stackd.engines.base import EngineAdapter
from stackd.runner import LaunchContext, LaunchSpec


@dataclass
class SglangParams:
    # The name SGLang advertises / accepts in `body["model"]`. Optional: stackd
    # defaults it to the stack name and injects `--served-model-name` into the
    # launch command itself, so it never has to be repeated in cmd_extra.
    served_model_name: str | None = None
    port: int = 8001
    backend_url: str | None = None       # override; default http://<container>:<port>
    context_length: int | None = None    # informational — the real value is in cmd_extra
    mem_fraction_static: float | None = None   # informational — real value is in cmd_extra
    # {caller_effort: engine_effort} request-time rewrite for `reasoning_effort`
    # — same contract as VllmParams.reasoning_effort_map. The fork's Qwen3 parser
    # already accepts any value, so this is usually left unset; provided for
    # symmetry / an explicit identity map. See serve._map_reasoning_effort.
    reasoning_effort_map: dict[str, str] | None = None


class SglangPennyroyalAdapter(EngineAdapter):
    """The `jpezzulli/sglang-rtxpro6000` "Pennyroyal" SGLang fork, built for the
    RTX PRO 6000 (sm120). NOT vanilla SGLang — the image (built from
    `stackd/sglang_pennyroyal_image/Dockerfile.cuda` via `stackctl build`) and the
    fork-only launch flags (DFLASH, mamba-radix-cache, gdn-mtp-cache-mode,
    linear-attn backends, ple-offload-embedding, HiCache page_first, …) both come
    from that fork.

    The adapter itself is engine-generic, same shape as `VllmCudaAdapter`: the
    long, stack-specific arg list lives in `engine.container.cmd_extra`; stackd
    just supplies image / mounts / env / network and owns `--served-model-name`.
    """

    template = "sglang-pennyroyal"
    Params = SglangParams
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
        # /health on the Pennyroyal fork runs a real 64-token forward pass — keep
        # it as the readiness gate + a periodic deep check, but poll the cheap
        # metadata endpoint in steady state so `--sleep-on-idle` can actually
        # park the GPU between requests instead of being woken every tick.
        base = self._url().rstrip("/")
        spec.health_url = base + "/health"
        spec.live_health_url = base + "/get_model_info"
        spec.deep_health_every = 15          # ~5 min at the 20s default tick
        # Cold boot: torch.compile + FlashInfer JIT + the 524K-token KV pool
        # allocation + (on the 27B) a second speculative draft model. Well past
        # vLLM's 900s.
        spec.ready_timeout_s = 1800.0
        return spec
