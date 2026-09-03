"""Dependency-free sanity check: `python3 tests/smoke.py` from the repo root.
The full suite is tests/test_validator.py (needs pytest)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

from stackd.config.loader import load_config  # noqa: E402
from stackd.planner import plan_transition  # noqa: E402
from stackd.validator import validate_profile  # noqa: E402

CFG = pathlib.Path(__file__).resolve().parent.parent / "config"


def _approx(a: float, b: float, tol: float = 0.5) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    cfg = load_config(CFG)
    checks: list[tuple[str, bool]] = []

    checks.append(("pools", set(cfg.pools) == {"cuda_vram", "host_unified"}))
    checks.append(("profiles", set(cfg.profiles) == {"everyday", "coding"}))

    ev = validate_profile(cfg, "everyday")
    checks.append(("everyday fits", ev.ok))
    cuda = next(p for p in ev.pools if p.pool == "cuda_vram")
    checks.append(("everyday cuda_vram=76", _approx(cuda.used_gib, 76.0)))
    uni = next(p for p in ev.pools if p.pool == "host_unified")
    # 28 reserve + 4 slack + (autocomplete 2 + headroom 3) + (chat 3 + image 18)
    checks.append(("everyday host_unified=57", _approx(uni.used_gib, 57.3, 0.6)))
    checks.append(
        ("everyday flags contention", any("contention on cuda0" in f for f in ev.flags))
    )

    co = validate_profile(cfg, "coding")
    checks.append(("coding fits", co.ok))
    checks.append(("coding keeps autocomplete", "everyday-autocomplete" in co.resident))
    checks.append(("coding drops chat", "everyday-chat" not in co.resident))
    checks.append(("coding flags R1", any("R1 discipline" in f for f in co.flags)))

    cuda = next(p for p in co.pools if p.pool == "cuda_vram")
    checks.append(("coding fills cuda0 (tight but ok)", co.ok and cuda.headroom_gib < 10))
    checks.append(("coding-flash stands in for chat (serves assistant*)",
                   any(se.api_name == "assistant*" for se in cfg.models["coding-flash"].serves)))

    plan = plan_transition(cfg, "everyday", "coding")
    checks.append(("plan teardown", plan.teardown == ["everyday-chat", "everyday-image"]))
    checks.append(("plan spawn", sorted(plan.spawn) == ["coding-flash", "coding-image"]))
    checks.append(("plan keep", plan.keep == ["everyday-autocomplete"]))

    # ${VAR} / ${VAR:-default} interpolation + .env-file override
    import tempfile

    from stackd.config._build import ConfigError
    from stackd.config.loader import _interpolate, _parse_env_file
    E = {"STACKD_SMOKE_V": "xyz", "EMPTYVAR": ""}
    checks.append(("interp: set var", _interpolate("a: ${STACKD_SMOKE_V}/m", E) == "a: xyz/m"))
    checks.append(("interp: default used", _interpolate("${NOPE_X:-def}", E) == "def"))
    checks.append(("interp: empty env falls to default", _interpolate("${EMPTYVAR:-d}", E) == "d"))
    checks.append(("interp: bare $ untouched", _interpolate("p$w", E) == "p$w"))
    try:
        _interpolate("${NOPE_X}", E)
        checks.append(("interp: unset+no default raises", False))
    except ConfigError as e:
        checks.append(("interp: unset+no default raises", "NOPE_X" in str(e)))

    ef = pathlib.Path(tempfile.mkdtemp()) / "x.env"
    ef.write_text('# c\nFOO=bar\nQUX="q q"  # trailing\n\nBLANK\n')
    parsed = _parse_env_file(ef)
    checks.append(("_parse_env_file", parsed == {"FOO": "bar", "QUX": "q q"}))
    # a .env-file value overrides os.environ for config interpolation
    import os
    os.environ["CUDA_VRAM_GIB"] = "999"
    ef2 = pathlib.Path(tempfile.mkdtemp()) / "y.env"
    ef2.write_text("CUDA_VRAM_GIB=42\n")
    cfg2 = load_config(CFG, env_file=str(ef2))
    checks.append((".env file overrides os.environ", cfg2.pools["cuda_vram"].total_gib == 42.0))
    os.environ["CUDA_VRAM_GIB"] = "95.6"

    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in checks)}/{len(checks)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
