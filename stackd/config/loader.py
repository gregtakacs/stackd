from __future__ import annotations

import os
import pathlib
import re
from typing import Any, Mapping

import yaml

from stackd.config._build import ConfigError, build
from stackd.config.models import Config, Device, ModelSpec, Pool, ProfileSpec, Runtime

# ${VAR} or ${VAR:-default} — compose-style. Interpolated over the raw YAML text
# before parsing, so every box-specific value (paths, sizes, GIDs, image tags, …)
# comes from the environment and the shipped config carries no customizations. A
# bare ${VAR} that is unset/empty and has no default is a hard error — the
# deployment's .env must provide it (see deploy/.env.example).
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _parse_env_file(path: str | pathlib.Path) -> dict[str, str]:
    """The compose `.env` subset: `KEY=VALUE` per line, `#` comments, blanks
    skipped, surrounding single/double quotes stripped. Missing file -> {}."""
    p = pathlib.Path(path)
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.split(" #", 1)[0].strip()      # trailing "  # comment"
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def _interpolate(text: str, env: Mapping[str, str]) -> str:
    missing: list[str] = []

    def sub(m: "re.Match[str]") -> str:
        name, default = m.group(1), m.group(2)
        val = env.get(name)
        if val:
            return val
        if default is not None:
            return default
        missing.append(name)
        return m.group(0)

    out = _VAR_RE.sub(sub, text)
    if missing:
        uniq = sorted(set(missing))
        raise ConfigError(
            f"config references {', '.join('${' + n + '}' for n in uniq)} but "
            f"{'they are' if len(uniq) > 1 else 'it is'} not set — add to the "
            f"stackd service's environment / .env (see deploy/.env.example)"
        )
    return out


def _load_yaml(path: pathlib.Path, env: Mapping[str, str]) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    return yaml.safe_load(_interpolate(path.read_text(), env)) or {}


def _pick(base: pathlib.Path, overlay: pathlib.Path | None, name: str) -> pathlib.Path:
    """`<overlay>/<name>` if it exists, else `<base>/<name>` — a whole-file swap."""
    if overlay is not None and (overlay / name).is_file():
        return overlay / name
    return base / name


def _load_named(base: pathlib.Path, overlay: pathlib.Path | None, key: str,
                cls, id_attr: str, env: Mapping[str, str]) -> dict[str, Any]:
    """Load every *.yaml under `<dir>/<key>/`. The effective file set is
    base ∪ overlay, keyed by BASENAME — an overlay file replaces the base file of
    the same name, a new name adds, and an *empty* overlay file deletes the base
    entry. Each file is one document or a list under a top-level `<key>:` key."""
    files: dict[str, pathlib.Path] = {}
    bd = base / key
    if bd.is_dir():
        for f in sorted(bd.glob("*.yaml")):
            files[f.name] = f
    if overlay is not None and (overlay / key).is_dir():
        for f in sorted((overlay / key).glob("*.yaml")):
            files[f.name] = f
    if not files and not (bd.is_dir() or (overlay and (overlay / key).is_dir())):
        raise FileNotFoundError(f"missing config dir: {bd}")

    out: dict[str, Any] = {}
    for fname in sorted(files):
        doc = _load_yaml(files[fname], env)
        if not doc:                      # empty overlay file -> drop this entry
            continue
        entries = doc[key] if isinstance(doc, dict) and key in doc else [doc]
        for raw in entries:
            obj = build(cls, raw, fname)
            k = getattr(obj, id_attr)
            if k in out:
                raise ValueError(f"duplicate {id_attr} {k!r} (in {fname})")
            out[k] = obj
    return out


def load_config(root: str | pathlib.Path, *, env_file: str | None = None,
                overlay: str | pathlib.Path | None = None) -> Config:
    """`${VAR}` in the YAML resolves from `{**os.environ, **<env_file>}` — the file
    (default $STACKD_ENV_FILE) WINS, so a live edit of the bind-mounted .env is
    picked up by `Manager.reload_config()` without recreating the container.

    `overlay` (default $STACKD_CONFIG_OVERLAY) is a second config dir that WINS
    per file: mount your private pools.yaml / models/*.yaml / catalog/*.json there
    and the shipped `config/` stays a pristine generic example.
    """
    base = pathlib.Path(root)
    if not base.is_dir():
        raise NotADirectoryError(f"config root is not a directory: {base}")

    env_file = env_file if env_file is not None else os.environ.get("STACKD_ENV_FILE")
    env: dict[str, str] = {**os.environ, **_parse_env_file(env_file)} if env_file else dict(os.environ)

    ov = overlay if overlay is not None else os.environ.get("STACKD_CONFIG_OVERLAY")
    ovp = pathlib.Path(ov) if ov and pathlib.Path(ov).is_dir() else None

    pools_raw = _load_yaml(_pick(base, ovp, "pools.yaml"), env).get("pools", {})
    devices_raw = _load_yaml(_pick(base, ovp, "devices.yaml"), env).get("devices", {})

    pools = {k: build(Pool, {"name": k, **v}, f"pools.yaml/{k}") for k, v in pools_raw.items()}
    devices = {
        k: build(Device, {"name": k, **v}, f"devices.yaml/{k}") for k, v in devices_raw.items()
    }
    models = _load_named(base, ovp, "models", ModelSpec, "model", env)
    profiles = _load_named(base, ovp, "profiles", ProfileSpec, "profile", env)

    rt_path = _pick(base, ovp, "runtime.yaml")
    runtime = build(Runtime, _load_yaml(rt_path, env).get("runtime", {}), "runtime.yaml") \
        if rt_path.exists() else Runtime()

    return Config(pools=pools, devices=devices, models=models, profiles=profiles,
                  runtime=runtime).validate()
