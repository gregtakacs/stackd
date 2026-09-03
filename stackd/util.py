from __future__ import annotations

import re

_DUR = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> int:
    """'45m' -> 2700. Accepts s/m/h/d. Raises ValueError on anything else."""
    m = _DUR.match(s)
    if not m:
        raise ValueError(f"bad duration {s!r} — expected like '45m', '2h', '30s', '1d'")
    return int(m.group(1)) * _UNIT_S[m.group(2)]
