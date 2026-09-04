"""Persisted runtime state — what is actually running, written atomically after
every mutating command so `stackctl` stays a plain short-lived process (no
daemon required for P2; `stackctl run` is just a tick loop)."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from dataclasses import asdict, dataclass, field

from stackd.engines.base import EngineState


def default_state_path() -> pathlib.Path:
    env = os.environ.get("STACKD_STATE")
    if env:
        return pathlib.Path(env)
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return pathlib.Path(base) / "stackd" / "state.json"


@dataclass
class StackRuntime:
    name: str
    owner_profile: str
    kind: str = "container"
    device: str = ""
    identity: list = field(default_factory=list)
    state: EngineState = EngineState.down
    handle: str | None = None
    container: str | None = None
    port: int | None = None
    health_url: str | None = None
    endpoint: str | None = None
    started_at: float | None = None
    ready_at: float | None = None
    ready_timeout: float = 300.0
    stop_grace: int | None = None
    restarts: int = 0
    backoff_until: float | None = None
    unhealthy_ticks: int = 0
    intentional_stop: bool = False


@dataclass
class ImageSlot:
    """The elastic image tier's single resident ComfyUI (config/media/image.yaml).
    Not a profile member — stackd runs it in whatever device VRAM is left after
    the active profile's LLM models are placed, and it stays put (sticky
    `active_model`) until the profile changes or someone requests otherwise."""

    active_model: str
    kind: str = "image"
    backend: str = ""
    device: str = ""
    container: str | None = None
    handle: str | None = None
    endpoint: str | None = None
    health_url: str | None = None
    state: EngineState = EngineState.down
    capabilities: list = field(default_factory=list)
    started_at: float | None = None
    ready_at: float | None = None
    ready_timeout: float = 300.0
    unhealthy_ticks: int = 0
    since: float | None = None            # when THIS active_model was chosen
    pinned_by: str = "auto"              # auto | user | capability
    identity: list = field(default_factory=list)
    intentional_stop: bool = False


@dataclass
class RuntimeState:
    active_profile: str
    pinned: bool = False
    entered_at: float | None = None
    last_switch_at: float | None = None
    last_served_at: float | None = None
    stacks: dict[str, StackRuntime] = field(default_factory=dict)
    image: "ImageSlot | None" = None

    # --- persistence -------------------------------------------------------------
    @classmethod
    def fresh(cls, default_profile: str) -> "RuntimeState":
        return cls(active_profile=default_profile)

    @classmethod
    def load(cls, path: str | pathlib.Path, default_profile: str) -> "RuntimeState":
        path = pathlib.Path(path)
        if not path.exists():
            return cls.fresh(default_profile)
        raw = json.loads(path.read_text())
        stacks = {
            k: StackRuntime(**{**v, "state": EngineState(v.get("state", "down"))})
            for k, v in raw.get("stacks", {}).items()
        }
        raw["stacks"] = stacks
        img = raw.get("image")
        raw["image"] = (
            ImageSlot(**{**img, "state": EngineState(img.get("state", "down"))})
            if img else None
        )
        return cls(**raw)

    def boot_reset(self) -> list[str]:
        """Called once at daemon start. A fresh process gets a fresh retry budget
        and a clean slate:

        * stacks that are NOT actively up (state error/down, or no handle) are
          DROPPED — converge/tick then treats them as missing and spawns them
          fresh. This clears a persisted `give-up` (a crash loop that exhausted
          retries in a previous run, often on a fault a reboot has since cleared)
          and the `down`+handle=None limbo that tick can't act on.
        * stacks that look up (warming/ready with a handle) are kept, with their
          crash bookkeeping zeroed; tick re-verifies them against the real
          container.

        Returns the dropped names."""
        dropped = []
        for name, rt in list(self.stacks.items()):
            if rt.handle is None or rt.state in (EngineState.error, EngineState.down):
                dropped.append(name)
                del self.stacks[name]
            else:
                rt.restarts = 0
                rt.backoff_until = None
                rt.unhealthy_ticks = 0
        if self.image is not None and (
            self.image.handle is None
            or self.image.state in (EngineState.error, EngineState.down)
        ):
            dropped.append(f"image:{self.image.active_model}")
            self.image = None
        elif self.image is not None:
            self.image.unhealthy_ticks = 0
        return dropped

    def save(self, path: str | pathlib.Path) -> None:
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        for s in payload["stacks"].values():
            s["state"] = s["state"].value if isinstance(s["state"], EngineState) else s["state"]
        if payload.get("image"):
            st = payload["image"]["state"]
            payload["image"]["state"] = st.value if isinstance(st, EngineState) else st
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
