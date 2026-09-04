from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from stackd.config._build import ConfigError
from stackd.util import parse_duration


class Backend(str, Enum):
    cuda = "cuda"
    vulkan = "vulkan"
    rocm = "rocm"
    cpu = "cpu"


class ReconcileMode(str, Enum):
    delta = "delta"
    full = "full"


@dataclass
class Pool:
    """A memory pool the validator balances as one budget.

    A *dedicated* pool has no host_reserve/load_slack and its device carries no
    vram_budget (the RTX's GDDR). A *shared* pool is the host's unified RAM: the
    iGPU's VRAM is carved from it, the host + containers reserve a floor, and
    every engine's RAM spill lands here too.
    """

    name: str
    total_gib: float
    host_reserve_gib: float = 0.0
    load_slack_gib: float = 0.0


@dataclass
class Device:
    name: str
    backend: Backend
    vram_pool: str
    # VRAM/GTT *ceiling* — the most this device may ever take from its pool.
    # Required when vram_pool is shared with the host; omit for a dedicated pool.
    vram_budget_gib: float | None = None
    # On a shared pool the amount charged to the pool is demand-driven:
    # min(sum(resident stacks on this device) + vram_headroom_gib, vram_budget_gib).
    # So an idle iGPU costs its headroom, not its whole ceiling.
    vram_headroom_gib: float = 0.0


@dataclass
class Budget:
    """Declared footprint of one engine instance, on both axes. P1 trusts these;
    P0 replaces them with a measured curve keyed on the config-signature (D5)."""

    vram_gib: float = 0.0
    ram_gib: float = 0.0


@dataclass
class MountSpec:
    host_path: str          # resolves on the docker host, not stackd's container
    container_path: str
    ro: bool = True


@dataclass
class BuildSpec:
    """`stackctl build` produces `ContainerSpec.image` from a local Dockerfile via
    the Docker Engine /build API. `context` is a path INSIDE stackd's container
    (stackd tars it and streams it to the daemon). Never built by `serve`."""

    context: str
    dockerfile: str = "Dockerfile"
    args: dict[str, str] = field(default_factory=dict)


@dataclass
class ContainerSpec:
    """How stackd runs this engine as a container. Default: stackd CREATES it
    from this recipe (image from here or runtime.images[template]). `adopt: true`
    -> stackd only start/stops an already-existing compose-declared container."""

    name: str | None = None            # default: "stackd-<model>"
    image: str | None = None           # else runtime.images[template]
    build: BuildSpec | None = None     # `stackctl build` target for a local image
    adopt: bool = False
    entrypoint: list[str] | None = None    # None -> the image's own entrypoint
    cmd_extra: list[str] = field(default_factory=list)   # appended to the rendered cmd (== the whole cmd for vllm/comfyui)
    env: dict[str, str] = field(default_factory=dict)
    mounts: list[MountSpec] = field(default_factory=list)
    ipc_host: bool = False
    port: int | None = None            # fixed port (else stackd allocates for llama.cpp)
    stop_grace_s: int | None = None    # graceful-stop timeout before SIGKILL (None -> per-backend default)
    # Container labels — values are os.path.expandvars'd at create time (so e.g.
    # a reverse-proxy Host rule can reference an env var set on the stackd service).
    labels: dict[str, str] = field(default_factory=dict)
    # Host device paths ADDED to the resolved device_profile's (e.g. comfyui-rocm
    # needs /dev/kfd on top of the vulkan profile's /dev/dri/renderD128).
    devices: list[str] = field(default_factory=list)
    shm_size: str | None = None        # per-container override of the device_profile's


@dataclass
class DeviceProfile:
    """Per-backend container knobs (runtime.yaml). Merged into every stackd-created
    engine whose resolved device has this backend — so 'run something on the iGPU'
    is a config line, not a new compose service."""

    gpus: str | None = None                 # "all" -> Docker DeviceRequests (CUDA)
    devices: list[str] = field(default_factory=list)      # host device paths, e.g. /dev/dri/renderD128
    group_add: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)
    ipc_host: bool = False
    shm_size: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class EngineSpec:
    template: str
    model: str | None = None          # the artifact: gguf basename / served name / checkpoint
    # Template-specific knobs; validated by the matching engine adapter's Params.
    params: dict[str, Any] = field(default_factory=dict)
    container: ContainerSpec = field(default_factory=ContainerSpec)


@dataclass
class Placement:
    # Devices this model may run on, in preference order. Empty = any device whose
    # backend the engine template supports. First that fits wins.
    devices: list[str] = field(default_factory=list)


@dataclass
class ServedEntry:
    api_name: str                     # exact name or a glob ("assistant*")
    # Force-merged into every request routed here (reasoning_effort, sampling, …).
    preset: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelSpec:
    """One entry in the models/ catalog — a servable model + how to run it. The
    *device* is chosen by the solver, not pinned here."""

    model: str
    engine: EngineSpec
    budget: Budget = field(default_factory=Budget)
    placement: Placement = field(default_factory=Placement)
    serves: list[ServedEntry] = field(default_factory=list)


@dataclass
class ProfileSpec:
    profile: str
    priority: int
    default: bool = False
    idle_evict: str | None = None
    # Models to keep loaded while this profile is active — LIST ORDER IS PRIORITY.
    # The solver places each on the first fitting device; ones that fit nowhere
    # are reported unplaced (a request for them can still trigger reactive entry).
    models: list[str] = field(default_factory=list)


@dataclass
class CleanerConfig:
    """The built-in ComfyUI scratch janitor (stackd/cleaner.py) — was the
    comfyui-cleaner container. `enabled` here is the boot default; POST
    /cleaner/on|off flips it live."""

    enabled: bool = True
    file_ttl_min: int = 2
    interval_s: int = 90
    scratch_dir: str = "/comfyui-scratch"


@dataclass
class Runtime:
    """runtime.yaml — how stackd reaches Docker and what images/mounts engines use."""
    host_models_dir: str = "/models"
    network: str | None = None
    images: dict[str, str] = field(default_factory=dict)              # template -> image ref
    device_profiles: dict[str, DeviceProfile] = field(default_factory=dict)  # backend -> knobs
    mounts: list[MountSpec] = field(default_factory=list)             # applied to every created engine
    comfyui_cleaner: CleanerConfig = field(default_factory=CleanerConfig)


@dataclass
class MediaLoadable:
    """One entry in a media tier's ``prefer:`` list — a checkpoint/pipeline the
    tier can load. stackd tries them top-down and loads the first that fits the
    device it lands on; a capability the resident one lacks escalates to the first
    *capable* entry that still fits. ``active_model`` keys
    imagegen/workflow_graphs/models.json; ``footprint_gib`` is a VRAM estimate per
    backend (refined by ``stackctl bench`` later)."""

    active_model: str
    capabilities: list[str] = field(default_factory=list)   # generate | stylize | edit | ...
    backends: list[str] = field(default_factory=list)       # cuda | rocm | vulkan -> which containers[<b>]
    footprint_gib: dict[str, float] = field(default_factory=dict)   # backend -> VRAM GiB


@dataclass
class MediaTier:
    """config/media/<kind>.yaml — an elastic image/video/audio tier stackd runs in
    whatever device capacity is left after the active profile's LLM models are
    placed. Never displaces an LLM engine. ``active_model`` is chosen at runtime
    from ``prefer:``, not declared here."""

    kind: str                                       # "image" | "video" | "audio" (= filename stem)
    margin_gib: float = 4.0                          # headroom safety cushion per device
    containers: dict[str, ContainerSpec] = field(default_factory=dict)   # backend -> how to run the engine
    prefer: list[MediaLoadable] = field(default_factory=list)            # load order


def _missing_graphs(active_model: str, caps: list[str]) -> list[str] | None:
    """Verbs in ``caps`` with no workflow graph for ``active_model`` in
    imagegen/workflow_graphs/models.json, or None when the registry can't be
    imported (core stackd without the ``[imagegen]`` extra still validates)."""
    if not caps:
        return []
    try:
        from stackd.imagegen.workflows import tools_for
    except Exception:
        return None
    have = tools_for(active_model or "")
    return [c for c in caps if c not in have]


def _check_comfyui_capabilities(m: ModelSpec) -> None:
    caps = list(m.engine.params.get("capabilities") or [])
    active_model = m.engine.params.get("active_model") or ""
    missing = _missing_graphs(active_model, caps)
    if missing:
        raise ConfigError(
            f"model {m.model!r}: capabilities {missing} have no workflow graph for "
            f"active_model {active_model!r}"
        )


def _check_media_tier(tier: MediaTier) -> None:
    for i, ld in enumerate(tier.prefer):
        where = f"media/{tier.kind}: prefer[{i}] {ld.active_model!r}"
        if not ld.backends:
            raise ConfigError(f"{where} lists no backends")
        for b in ld.backends:
            if b not in tier.containers:
                raise ConfigError(f"{where} needs backend {b!r} but there is no containers.{b} block")
            if b not in ld.footprint_gib:
                raise ConfigError(f"{where} is missing footprint_gib.{b}")
        if tier.kind == "image":
            missing = _missing_graphs(ld.active_model, ld.capabilities)
            if missing:
                raise ConfigError(
                    f"{where} capabilities {missing} have no workflow graph "
                    f"(imagegen/workflow_graphs/models.json)"
                )


@dataclass
class Config:
    pools: dict[str, Pool]
    devices: dict[str, Device]
    models: dict[str, ModelSpec]
    profiles: dict[str, ProfileSpec]
    runtime: Runtime = field(default_factory=Runtime)
    media: dict[str, MediaTier] = field(default_factory=dict)

    def validate(self) -> "Config":
        from stackd.engines.registry import TEMPLATES

        for d in self.devices.values():
            if d.vram_pool not in self.pools:
                raise ConfigError(f"device {d.name!r}: unknown vram_pool {d.vram_pool!r}")
            pool = self.pools[d.vram_pool]
            if (pool.host_reserve_gib or pool.load_slack_gib) and d.vram_budget_gib is None:
                raise ConfigError(
                    f"device {d.name!r} draws VRAM from shared pool {pool.name!r} — "
                    f"vram_budget_gib is required"
                )

        for m in self.models.values():
            if m.engine.template not in TEMPLATES:
                raise ConfigError(
                    f"model {m.model!r}: unknown engine template {m.engine.template!r} "
                    f"(known: {', '.join(sorted(TEMPLATES))})"
                )
            for dev in m.placement.devices:
                if dev not in self.devices:
                    raise ConfigError(f"model {m.model!r}: placement names unknown device {dev!r}")
            try:
                TEMPLATES[m.engine.template].validate_params(m.engine.params)
            except ConfigError as e:
                raise ConfigError(f"model {m.model!r}: {e}") from None

            if m.engine.template == "comfyui":
                _check_comfyui_capabilities(m)

        defaults = [p.profile for p in self.profiles.values() if p.default]
        if len(defaults) != 1:
            raise ConfigError(
                f"exactly one profile must be default:true — found {len(defaults)} "
                f"({', '.join(defaults) or 'none'})"
            )

        for pr in self.profiles.values():
            if pr.idle_evict is not None:
                parse_duration(pr.idle_evict)
            for mn in pr.models:
                if mn not in self.models:
                    raise ConfigError(f"profile {pr.profile!r}: unknown model {mn!r}")

        for tier in self.media.values():
            _check_media_tier(tier)
        return self
