"""Reference environment for the smoke suite.

stackd's `config/*.yaml` interpolates every box-specific value from the
environment (`${VAR}`) and errors on anything unset. The tests load that config,
so they need those vars present. Import this module before `load_config`:

    import _env  # noqa: F401

Values come from `deploy/.env.example` when it can be found (the single source
of truth); otherwise from the inline fallback below. Uses `setdefault`, so a real
environment always wins.
"""

from __future__ import annotations

import os
import pathlib

_FALLBACK = {
    "TZ": "UTC",
    "DOCKERDIR": "/srv/stackd",
    "STACKD_NETWORK": "stackd-net",
    "CUDA_VRAM_GIB": "95.6",
    "HOST_RAM_GIB": "128",
    "HOST_RESERVE_GIB": "28",
    "IGPU_VRAM_BUDGET_GIB": "90",
    "IGPU_RENDER_NODE": "/dev/dri/renderD128",
    # keep load_config() deterministic — don't let this host's real DRM topology
    # (which card got renderD128) override the fixed value above (see loader.py).
    "STACKD_DISABLE_GPU_AUTODETECT": "1",
    "VIDEO_GID": "44",
    "RENDER_GID": "990",
    "HSA_GFX_VERSION": "11.5.1",
    "LLAMACPP_CUDA_IMAGE": "ghcr.io/ggml-org/llama.cpp:server-cuda",
    "LLAMACPP_VULKAN_IMAGE": "ghcr.io/ggml-org/llama.cpp:server-vulkan",
    "VLLM_IMAGE": "vllm/vllm-openai:latest",
    "COMFYUI_CUDA_IMAGE": "yanwk/comfyui-boot:cu130-megapak-pt211-20260826",
    "COMFYUI_ROCM_IMAGE": "comfyui-rocm:local",
    "COMFYUI_ROCM_BASE": "rocm/pytorch:rocm7.14.1_ubuntu24.04_py3.12_pytorch_release_2.12.0",
    "COMFYUI_ROCM_REF": "master",
    "STACKD_SRC": "/opt/stackd",
}


def _from_example() -> dict[str, str]:
    here = pathlib.Path(__file__).resolve()
    for base in here.parents:
        cand = base / "deploy" / ".env.example"
        if cand.is_file():
            out: dict[str, str] = {}
            for line in cand.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
    return {}


for _k, _v in {**_FALLBACK, **_from_example()}.items():
    if _v:
        os.environ.setdefault(_k, _v)

# The smoke suite runs FakeRunner load/teardown cycles against the checked-in
# config/, and the reconciler's passive EMA would otherwise fold those synthetic
# durations into config/catalog/*.json on every run. Freeze it for tests.
os.environ.setdefault("STACKD_PASSIVE_TIMINGS", "0")
