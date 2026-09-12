"""Persisted runtime state — what is actually running, written atomically after
every mutating command so `stackctl` stays a plain short-lived process (no
daemon required for P2; `stackctl run` is just a tick loop)."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from dataclasses import asdict, dataclass, field, fields

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
    live_health_url: str | None = None   # cheap steady-state liveness (see LaunchSpec)
    deep_health_every: int = 0           # hit health_url every Nth ready-tick anyway
    health_ticks: int = 0               # counter toward deep_health_every
    endpoint: str | None = None
    started_at: float | None = None
    ready_at: float | None = None
    stopped_at: float | None = None
    ready_timeout: float = 300.0
    stop_grace: int | None = None
    restarts: int = 0
    backoff_until: float | None = None
    unhealthy_ticks: int = 0
    intentional_stop: bool = False
    last_error: str | None = None        # OCI/runtime failure text from the last crash, if any


@dataclass
class DeadMark:
    """A profile whose engine exhausted its restart budget and was evicted back
    to the default profile. Stays until someone manually retries it (`use()`
    with `manual=True` clears the mark before converging) — reactive routing
    (`route()`) refuses to re-enter a dead profile on its own, so a caller gets
    a clear error instead of stackd silently re-running the same doomed 5-attempt
    crash loop on every request while the GPU keeps drawing power for nothing."""
    stack: str
    reason: str
    since: float


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
    # The profile we switched AWAY from on the most recent transition, so an
    # in-progress swap can tell a requestor "Model load (from → to) in progress".
    # Set in Manager._enter on a real switch; cleared when the target is fully up.
    switch_from: str | None = None
    last_served_at: float | None = None
    stacks: dict[str, StackRuntime] = field(default_factory=dict)
    image: "ImageSlot | None" = None
    # "<profile>:<resident image active_model>" the `image: warm` forced
    # generation has already fired for. Reset to None on every profile switch
    # (converge()) so re-activating a warm profile warms it again; also
    # naturally re-arms on a manual `/image/model` swap under the SAME
    # profile, since the resident model in the key changes — see
    # Reconciler._maybe_warm_image.
    warmed_for: str | None = None
    # profile name -> DeadMark, for a profile evicted after its engine gave up.
    dead: dict[str, DeadMark] = field(default_factory=dict)
    # the nvidia driver package version a human has already acknowledged on the
    # dashboard (see Manager._read_nvidia_update) — showing again only once a
    # NEWER version shows up is the "once per version" part of that banner.
    nvidia_update_dismissed: str | None = None

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
        _sr = {f.name for f in fields(StackRuntime)}
        stacks = {
            k: StackRuntime(**{**{kk: vv for kk, vv in v.items() if kk in _sr},
                               "state": EngineState(v.get("state", "down"))})
            for k, v in raw.get("stacks", {}).items()
        }
        _dm = {f.name for f in fields(DeadMark)}
        raw["dead"] = {
            k: DeadMark(**{kk: vv for kk, vv in v.items() if kk in _dm})
            for k, v in raw.get("dead", {}).items()
        }
        raw["stacks"] = stacks
        img = raw.get("image")
        _is = {f.name for f in fields(ImageSlot)}
        raw["image"] = (
            ImageSlot(**{**{kk: vv for kk, vv in img.items() if kk in _is},
                         "state": EngineState(img.get("state", "down"))})
            if img else None
        )
        # tolerate keys from a newer/older schema (e.g. a since-removed field)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

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
        * a persisted `dead` mark is cleared too — a fresh process gets a fresh
          chance, same reasoning as the restart-budget reset above (the fault
          that killed it, e.g. a driver mismatch, may be exactly what the
          restart fixed).

        Returns the dropped names."""
        self.dead.clear()
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
