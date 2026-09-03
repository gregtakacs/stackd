"""Inventory probe (P0): read real device + host-memory totals from the tools on
the box. Best-effort — anything missing is reported, nothing is overwritten.
Parsers are split out so they can be tested against captured sample output."""

from __future__ import annotations

import json
import shutil
import subprocess


def parse_nvidia_smi(text: str) -> list[dict]:
    """`nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader,nounits`
    -> [{index, name, vram_total_gib}] (memory.total is MiB)."""
    rows = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            rows.append({
                "index": int(parts[0]),
                "name": parts[1],
                "vram_total_gib": round(float(parts[2]) / 1024, 1),
                "backend": "cuda",
            })
        except ValueError:
            continue
    return rows


def parse_rocm_smi(text: str) -> list[dict]:
    """`rocm-smi --showmeminfo vram --json` -> [{index, vram_total_gib}]. Falls
    back to plain-text `VRAM Total Memory (B): N` lines."""
    rows: list[dict] = []
    try:
        obj = json.loads(text)
        for card, fields in obj.items():
            if not card.lower().startswith("card"):
                continue
            b = None
            for k, v in fields.items():
                if "vram total" in k.lower() and "memory" in k.lower():
                    b = float(v)
            if b:
                rows.append({
                    "index": int("".join(filter(str.isdigit, card)) or 0),
                    "vram_total_gib": round(b / 1024**3, 1),
                    "backend": "rocm",
                })
        if rows:
            return rows
    except (ValueError, AttributeError):
        pass
    for i, line in enumerate(text.splitlines()):
        low = line.lower()
        if "vram total memory" in low and "(b)" in low:
            digits = "".join(c for c in line.split(":")[-1] if c.isdigit())
            if digits:
                rows.append({"index": len(rows), "vram_total_gib": round(int(digits) / 1024**3, 1),
                             "backend": "rocm"})
    return rows


def parse_meminfo(text: str) -> dict:
    """`/proc/meminfo` -> {total_gib, available_gib, used_gib} (values are kB)."""
    kv = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, rest = line.partition(":")
            num = "".join(c for c in rest if c.isdigit())
            if num:
                kv[k.strip()] = int(num)
    total = kv.get("MemTotal", 0) / 1024**2
    avail = kv.get("MemAvailable", 0) / 1024**2
    return {
        "total_gib": round(total, 1),
        "available_gib": round(avail, 1),
        "used_gib": round(total - avail, 1),
    }


def _run(cmd: list[str]) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def probe() -> dict:
    out: dict = {"gpus": [], "host_mem": {}, "errors": []}

    nv = _run(["nvidia-smi", "--query-gpu=index,name,memory.total",
               "--format=csv,noheader,nounits"])
    if nv:
        out["gpus"] += parse_nvidia_smi(nv)
    else:
        out["errors"].append("nvidia-smi unavailable or failed")

    rc = _run(["rocm-smi", "--showmeminfo", "vram", "--json"]) or _run(["rocm-smi", "--showmeminfo", "vram"])
    if rc:
        out["gpus"] += parse_rocm_smi(rc)
    else:
        out["errors"].append("rocm-smi unavailable or failed")

    try:
        with open("/proc/meminfo") as f:
            out["host_mem"] = parse_meminfo(f.read())
    except OSError as e:
        out["errors"].append(f"/proc/meminfo: {e}")

    return out


def suggest(probe_result: dict) -> str:
    """Human-readable YAML fragment to reconcile against pools.yaml/devices.yaml."""
    lines = ["# probed — reconcile by hand against config/pools.yaml + config/devices.yaml", ""]
    hm = probe_result.get("host_mem") or {}
    if hm:
        lines += [
            "pools:",
            f"  host_unified: {{ total_gib: {hm['total_gib']} }}   "
            f"# available now: {hm['available_gib']}, in use: {hm['used_gib']}",
        ]
    cuda = [g for g in probe_result["gpus"] if g["backend"] == "cuda"]
    if cuda:
        lines += ["  cuda_vram: { total_gib: %s }   # %s" % (
            cuda[0]["vram_total_gib"], cuda[0].get("name", "cuda0"))]
    lines += ["", "devices:"]
    for g in probe_result["gpus"]:
        lines.append(f"  # {g['backend']} idx {g['index']}: {g.get('name','?')}  "
                     f"{g['vram_total_gib']} GiB total")
    for e in probe_result.get("errors", []):
        lines.append(f"# ! {e}")
    return "\n".join(lines)
