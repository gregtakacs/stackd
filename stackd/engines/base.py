from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from stackd.runner import LaunchContext, LaunchSpec, Mount

if TYPE_CHECKING:
    from stackd.config.models import Budget, Device, ModelSpec


class EngineState(str, Enum):
    down = "down"
    warming = "warming"
    ready = "ready"
    error = "error"


@dataclass
class _NoParams:
    pass


@dataclass
class DescribeResult:
    state: EngineState
    active_model: str | None
    served: list[str] = field(default_factory=list)
    endpoint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class EngineAdapter(ABC):
    """One parametric container recipe for a catalog model. Adapters are pure:
    they render a `LaunchSpec` + health URL; the reconciler + Runner do the work."""

    template: ClassVar[str] = ""
    Params: ClassVar[type] = _NoParams
    default_devices: ClassVar[str] = "nvidia"
    backends: ClassVar[tuple[str, ...]] = ()   # device backends this engine can run on

    def __init__(self, spec: "ModelSpec", device: "Device") -> None:
        self.spec = spec
        self.device = device
        self.params = self.validate_params(spec.engine.params)

    @classmethod
    def validate_params(cls, params: dict[str, Any]):
        from stackd.config._build import build

        return build(cls.Params, params, f"params({cls.template})")

    # -- shared LaunchSpec plumbing --------------------------------------------------
    def container_name(self) -> str:
        return self.spec.engine.container.name or f"stackd-{self.spec.name}"

    def _base_spec(self, ctx: LaunchContext) -> LaunchSpec:
        c = self.spec.engine.container
        d = ctx.device                       # resolved per-backend DeviceKnobs
        mounts = [Mount(m.host_path, m.container_path, m.ro)
                  for m in (*ctx.extra_mounts, *c.mounts)]
        env = {**d.env, **c.env}
        if d.gpus:                            # CUDA — the toolkit injects nvidia-smi
            env.setdefault("NVIDIA_VISIBLE_DEVICES", "all")
            env.setdefault("NVIDIA_DRIVER_CAPABILITIES", "compute,utility")
        return LaunchSpec(
            name=self.container_name(),
            image=None if c.adopt else (c.image or ctx.images.get(self.template)),
            entrypoint=c.entrypoint,
            env=env,
            mounts=[] if c.adopt else mounts,
            gpus=d.gpus,
            device_paths=[*d.devices, *c.devices],
            group_add=list(d.group_add),
            security_opt=list(d.security_opt),
            network=ctx.network,
            ipc_host=c.ipc_host or d.ipc_host,
            shm_size=c.shm_size or d.shm_size,
            ulimits=dict(c.ulimits),
            mem_limit_gib=c.mem_limit_gib,
            labels=dict(c.labels),
            stop_grace_s=c.stop_grace_s,
        )

    @abstractmethod
    def vram_estimate(self) -> "Budget":
        ...

    @abstractmethod
    def launch_spec(self, ctx: LaunchContext) -> LaunchSpec:
        ...

    @abstractmethod
    def endpoint(self, port: int | None = None) -> str | None:
        ...

    def health_url(self, port: int | None = None) -> str | None:
        return self.launch_spec(LaunchContext(port=port)).health_url

    def describe(self, state: EngineState = EngineState.down,
                 port: int | None = None) -> DescribeResult:
        return DescribeResult(
            state=state,
            active_model=self.spec.engine.model,
            served=[s.api_name for s in self.spec.serves],
            endpoint=self.endpoint(port),
        )
