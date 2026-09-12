"""The one side-effecting layer. Every engine is a container that **stackd
creates** from its own config (single source of truth) — a `LaunchSpec` carries
the image, command, mounts (host paths — they resolve on the docker host),
device access and network.

`DockerApiRunner` drives the Docker Engine API through a scoped socket-proxy
(`DOCKER_API_URL`) — this is the production path when stackd runs in its own
container. `LocalRunner` shells the `docker` CLI (stackd on the host).
`FakeRunner` is in-memory for tests and GPU-less demos.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Protocol


@dataclass
class Mount:
    host_path: str
    container_path: str
    ro: bool = True

    def to_bind(self) -> str:
        return f"{self.host_path}:{self.container_path}" + (":ro" if self.ro else "")


@dataclass
class DeviceKnobs:
    """Resolved per-backend container knobs (from runtime.device_profiles)."""
    gpus: str | None = None
    devices: list[str] = field(default_factory=list)
    group_add: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)
    ipc_host: bool = False
    shm_size: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class LaunchContext:
    """What the adapter needs to render a LaunchSpec, from runtime.yaml."""
    host_models_dir: str = "/models"
    network: str | None = None
    port: int | None = None
    device_index: int = 0
    images: dict[str, str] = field(default_factory=dict)   # template -> image ref
    extra_mounts: list[Mount] = field(default_factory=list)
    device: DeviceKnobs = field(default_factory=DeviceKnobs)


@dataclass
class LaunchSpec:
    name: str                                   # stable, stackd-owned container name
    image: str | None = None                    # None => start an already-existing container
    cmd: list[str] = field(default_factory=list)
    entrypoint: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    mounts: list[Mount] = field(default_factory=list)
    gpus: str | None = None                      # "all" -> DeviceRequests
    device_paths: list[str] = field(default_factory=list)
    group_add: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)
    network: str | None = None
    ipc_host: bool = False
    shm_size: str | None = None
    ulimits: dict[str, int] = field(default_factory=dict)   # e.g. {"nofile": 65536} -- soft==hard
    mem_limit_gib: float | None = None           # hard cgroup memory ceiling (memory == memory-swap)
    labels: dict[str, str] = field(default_factory=dict)
    health_url: str | None = None
    # optional cheap liveness endpoint used for the STEADY-STATE poll once the
    # stack is `ready` (health_url stays the readiness gate + the deep check).
    # Lets an engine whose /health runs a real forward pass (SGLang-Pennyroyal)
    # not wake the GPU every tick. When None, health_url is polled as before.
    live_health_url: str | None = None
    # if >0 and live_health_url is set: still hit the (generative) health_url
    # every Nth ready-tick to catch a wedged scheduler that keeps serving the
    # cheap endpoint.
    deep_health_every: int = 0
    ready_timeout_s: float = 300.0
    stop_grace_s: int | None = None


class Runner(Protocol):
    def spawn(self, spec: LaunchSpec) -> str: ...
    def stop(self, handle: str, *, remove: bool = False, timeout: int = 30) -> None: ...
    def poll(self, handle: str) -> int | None: ...          # None=running, int=exit code, -1=gone
    def last_error(self, handle: str) -> str | None: ...    # OCI/runtime failure text, if any
    def http_ok(self, url: str, timeout: float = 2.0) -> bool: ...
    def probe_accelerator(self, backend: str) -> bool: ...
    def gpu_free_mib(self) -> int | None: ...               # MiB free on cuda0; None = nvidia-smi unresponsive


def _bytes(size: str | None) -> int | None:
    if not size:
        return None
    u = {"k": 1024, "m": 1024**2, "g": 1024**3}.get(size[-1].lower())
    return int(float(size[:-1]) * u) if u else int(size)


def _nvidia_smi(args: list[str], timeout: float = 8.0) -> str | None:
    """Run `nvidia-smi <args>`, bounded even when the driver is WEDGED. A hung
    nvidia-smi sits in an uninterruptible ioctl and ignores SIGKILL, so
    subprocess's own timeout can't reap it and `subprocess.run(timeout=)` blocks
    past its deadline. Wrapping in coreutils `timeout` lets our call return
    regardless (the orphaned nvidia-smi clears when the driver recovers / reboot).
    Returns stdout on success, None on any failure or timeout."""
    try:
        r = subprocess.run(
            ["timeout", "-k", "2", str(int(timeout)), "nvidia-smi", *args],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _parse_free_mib(out: str | None) -> int | None:
    """First value from `nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits`."""
    if not out:
        return None
    try:
        return int(out.strip().splitlines()[0].split(",")[0].strip())
    except (ValueError, IndexError):
        return None


def _create_payload(spec: LaunchSpec) -> dict:
    host: dict = {
        "Binds": [m.to_bind() for m in spec.mounts],
        "RestartPolicy": {"Name": "no"},
    }
    if spec.network:
        host["NetworkMode"] = spec.network
    if spec.ipc_host:
        host["IpcMode"] = "host"
    if spec.gpus:
        host["DeviceRequests"] = [{"Driver": "", "Count": -1, "Capabilities": [["gpu"]]}]
    if spec.device_paths:
        host["Devices"] = [
            {"PathOnHost": p, "PathInContainer": p, "CgroupPermissions": "rwm"}
            for p in spec.device_paths
        ]
    if spec.group_add:
        host["GroupAdd"] = spec.group_add
    if spec.security_opt:
        host["SecurityOpt"] = spec.security_opt
    if (sh := _bytes(spec.shm_size)):
        host["ShmSize"] = sh
    if spec.ulimits:
        host["Ulimits"] = [{"Name": k, "Soft": v, "Hard": v} for k, v in spec.ulimits.items()]
    if spec.mem_limit_gib:
        mem_bytes = int(spec.mem_limit_gib * 1024 ** 3)
        host["Memory"] = mem_bytes
        host["MemorySwap"] = mem_bytes   # == Memory -> no additional swap, clean OOM-kill at the ceiling
    body: dict = {
        "Image": spec.image, "Cmd": spec.cmd, "HostConfig": host,
        "Env": [f"{k}={v}" for k, v in spec.env.items()],
        # stackd owns health-checking; disable any HEALTHCHECK baked into the image.
        "Healthcheck": {"Test": ["NONE"]},
    }
    if spec.entrypoint is not None:
        body["Entrypoint"] = spec.entrypoint
    if spec.labels:
        # ${VARS} in label values resolve from stackd's own env (e.g. traefik host rules).
        body["Labels"] = {k: os.path.expandvars(v) for k, v in spec.labels.items()}
    return body


# --------------------------------------------------------------- Docker Engine API


class DockerApiRunner:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None,
             *, timeout: float = 30.0) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"content-type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _exists(self, name: str) -> dict | None:
        code, raw = self._req("GET", f"/containers/{name}/json")
        return json.loads(raw) if code == 200 else None

    def spawn(self, spec: LaunchSpec) -> str:
        # adopt (compose-defined) container: stackd holds no create spec for it,
        # only start/stops it in place.
        if spec.image is None:
            self._req("POST", f"/containers/{spec.name}/start")
            return spec.name
        # stackd-owned: the launch spec is authoritative. A container already
        # carrying this name is from a previous life and may have been created
        # with different args — most notably a dynamically-assigned llamacpp
        # `--port`, which shifts when the profile order changes which model claims
        # 11500 first. Blindly starting the stale one leaves its real
        # --port/env/mounts out of sync with the spec stackd health-checks
        # against, and it never heals (permanent "warming"). Recreate instead.
        if self._exists(spec.name) is not None:
            dc, dr = self._req("DELETE", f"/containers/{spec.name}?force=1", timeout=45)
            if dc not in (204, 404) and self._exists(spec.name) is not None:
                raise RuntimeError(f"replace {spec.name}: stale container won't "
                                   f"remove: {dc} {dr[:200]!r}")
        code, raw = self._req("POST", f"/containers/create?name={spec.name}",
                              _create_payload(spec))
        if code not in (201, 204):
            raise RuntimeError(f"create {spec.name}: {code} {raw[:200]!r}")
        self._req("POST", f"/containers/{spec.name}/start")
        return spec.name

    def stop(self, handle: str, *, remove: bool = False, timeout: int = 30) -> None:
        # Docker holds the /stop request open for up to `t` seconds (SIGTERM →
        # wait → SIGKILL); the HTTP read timeout must clear that, or a slow
        # graceful stop (vLLM: stop_grace_s=60) raises mid-converge and leaves
        # the rest of the teardown undone. Teardown is best-effort — the
        # container is on its way down and tick confirms — so swallow a timeout.
        try:
            self._req("POST", f"/containers/{handle}/stop?t={timeout}",
                      timeout=timeout + 15)
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if remove:
            try:
                self._req("DELETE", f"/containers/{handle}?force=1", timeout=45)
            except (urllib.error.URLError, TimeoutError, OSError):
                pass

    def poll(self, handle: str) -> int | None:
        info = self._exists(handle)
        if info is None:
            return -1
        st = info.get("State", {})
        return None if st.get("Running") else int(st.get("ExitCode", 0))

    def last_error(self, handle: str) -> str | None:
        # `State.Error` carries the OCI/runtime failure text (e.g. "nvidia-
        # container-cli: initialization error: nvml error: driver/library
        # version mismatch") when the container never got a process running at
        # all — the case a bare exit code can't explain. Empty when the process
        # started and exited on its own (a real app-level crash — see its logs).
        info = self._exists(handle)
        err = (info or {}).get("State", {}).get("Error") or ""
        return err or None

    def http_ok(self, url: str, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return 200 <= r.status < 300
        except Exception:
            return False

    def probe_accelerator(self, backend: str) -> bool:
        if backend == "cuda":
            return _nvidia_smi(["-L"]) is not None
        try:
            return subprocess.run(["timeout", "-k", "2", "8", "rocm-smi", "--showid"],
                                  capture_output=True, timeout=13).returncode == 0
        except Exception:
            return False

    def gpu_free_mib(self) -> int | None:
        return _parse_free_mib(_nvidia_smi(
            ["--query-gpu=memory.free", "--format=csv,noheader,nounits"]))


# ------------------------------------------------------------------- docker CLI


class LocalRunner:
    """stackd on the host, driving the `docker` CLI."""

    def spawn(self, spec: LaunchSpec) -> str:
        # adopt (compose-defined) container: only start/stop it in place.
        if spec.image is None:
            subprocess.run(["docker", "start", spec.name], check=True, timeout=60)
            return spec.name
        # stackd-owned: the spec is authoritative — drop any stale container of
        # this name and recreate from the current spec (see DockerApiRunner.spawn
        # for why blindly starting the old one strands the engine).
        if subprocess.run(["docker", "inspect", spec.name],
                          capture_output=True).returncode == 0:
            subprocess.run(["docker", "rm", "-f", spec.name], capture_output=True, timeout=60)
        cmd = ["docker", "create", "--name", spec.name, "--restart", "no"]
        if spec.network:
            cmd += ["--network", spec.network]
        if spec.gpus:
            cmd += ["--gpus", spec.gpus]
        for d in spec.device_paths:
            cmd += ["--device", d]
        for g in spec.group_add:
            cmd += ["--group-add", g]
        for s in spec.security_opt:
            cmd += ["--security-opt", s]
        if spec.shm_size:
            cmd += ["--shm-size", spec.shm_size]
        for k, v in spec.ulimits.items():
            cmd += ["--ulimit", f"{k}={v}:{v}"]
        if spec.mem_limit_gib:
            cmd += ["--memory", f"{spec.mem_limit_gib}g", "--memory-swap", f"{spec.mem_limit_gib}g"]
        if spec.ipc_host:
            cmd += ["--ipc", "host"]
        for m in spec.mounts:
            cmd += ["-v", m.to_bind()]
        for k, v in spec.env.items():
            cmd += ["-e", f"{k}={v}"]
        if spec.entrypoint is not None:
            cmd += ["--entrypoint", spec.entrypoint[0]]
        cmd += [spec.image, *spec.cmd]
        subprocess.run(cmd, check=True, timeout=60)
        subprocess.run(["docker", "start", spec.name], check=True, timeout=60)
        return spec.name

    def stop(self, handle: str, *, remove: bool = False, timeout: int = 30) -> None:
        subprocess.run(["docker", "stop", "-t", str(timeout), handle], capture_output=True)
        if remove:
            subprocess.run(["docker", "rm", "-f", handle], capture_output=True)

    def poll(self, handle: str) -> int | None:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", handle],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return -1
        running, _, code = r.stdout.strip().partition(" ")
        return None if running == "true" else int(code or 0)

    def last_error(self, handle: str) -> str | None:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Error}}", handle],
                           capture_output=True, text=True)
        err = r.stdout.strip() if r.returncode == 0 else ""
        return err or None

    def http_ok(self, url: str, timeout: float = 2.0) -> bool:
        return DockerApiRunner.http_ok(self, url, timeout)  # type: ignore[arg-type]

    def probe_accelerator(self, backend: str) -> bool:
        return DockerApiRunner.probe_accelerator(self, backend)  # type: ignore[arg-type]

    def gpu_free_mib(self) -> int | None:
        return DockerApiRunner.gpu_free_mib(self)  # type: ignore[arg-type]


# -------------------------------------------------------------------------- fake


@dataclass
class _FakeC:
    spec: LaunchSpec
    running: bool = True
    exit_code: int | None = None
    error: str | None = None


class FakeRunner:
    def __init__(self, *, ready_after: int = 1, accel_ok: bool = True, persist_path=None,
                 gpu_free_mib: int = 95000) -> None:
        self.ready_after = ready_after
        self.accel_ok = accel_ok
        # gpu_free_mib(): pops the next value each call (last repeats); a None entry
        # simulates an unresponsive nvidia-smi. Tests script this for the drain.
        self.gpu_free_values: list[int | None] = [gpu_free_mib]
        self.containers: dict[str, _FakeC] = {}
        self._hits: dict[str, int] = {}
        self.probe_calls = 0
        self.removed: list[str] = []
        self._persist = pathlib.Path(persist_path) if persist_path else None
        self._load()

    def _load(self) -> None:
        if not self._persist or not self._persist.exists():
            return
        d = json.loads(self._persist.read_text())
        self.containers = {
            n: _FakeC(LaunchSpec(**{**c["spec"], "mounts": [Mount(**m) for m in c["spec"].get("mounts", [])]}),
                      c["running"], c["exit_code"], c.get("error"))
            for n, c in d.get("containers", {}).items()
        }
        self._hits = d.get("hits", {})
        self.probe_calls = d.get("probe_calls", 0)

    def _save(self) -> None:
        if not self._persist:
            return
        self._persist.parent.mkdir(parents=True, exist_ok=True)
        self._persist.write_text(json.dumps({
            "containers": {n: {"spec": asdict(c.spec), "running": c.running,
                               "exit_code": c.exit_code, "error": c.error}
                          for n, c in self.containers.items()},
            "hits": self._hits, "probe_calls": self.probe_calls,
        }))

    def spawn(self, spec: LaunchSpec) -> str:
        self.containers[spec.name] = _FakeC(spec)
        self._save()
        return spec.name

    def stop(self, handle: str, *, remove: bool = False, timeout: int = 30) -> None:
        c = self.containers.get(handle)
        if c:
            c.running = False
            c.exit_code = c.exit_code if c.exit_code is not None else 0
        if remove and handle in self.containers:
            del self.containers[handle]
            self.removed.append(handle)
        self._save()

    def poll(self, handle: str) -> int | None:
        c = self.containers.get(handle)
        if c is None:
            return -1
        return None if c.running else (c.exit_code if c.exit_code is not None else 0)

    def last_error(self, handle: str) -> str | None:
        c = self.containers.get(handle)
        return c.error if c else None

    def http_ok(self, url: str, timeout: float = 2.0) -> bool:
        self._hits[url] = self._hits.get(url, 0) + 1
        self._save()
        return self._hits[url] >= self.ready_after

    def probe_accelerator(self, backend: str) -> bool:
        self.probe_calls += 1
        self._save()
        return self.accel_ok

    def gpu_free_mib(self) -> int | None:
        return self.gpu_free_values.pop(0) if len(self.gpu_free_values) > 1 else self.gpu_free_values[0]

    # test helpers
    def crash(self, handle: str, exit_code: int = 139, error: str | None = None) -> None:
        c = self.containers[handle]
        c.running = False
        c.exit_code = exit_code
        c.error = error

    def reset_health(self, url: str) -> None:
        self._hits.pop(url, None)


def default_runner() -> Runner:
    if os.environ.get("STACKD_FAKE") == "1":
        return FakeRunner()
    api = os.environ.get("DOCKER_API_URL")
    if api:
        return DockerApiRunner(api)
    return LocalRunner()
