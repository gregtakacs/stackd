#!/usr/bin/env bash
# Entrypoint for the SGLang "Pennyroyal" runtime container (see Dockerfile.cuda).
#
# stackd's SglangPennyroyalAdapter hands us the complete SGLang argument list as
# "$@" (the model YAML's container.cmd_extra, plus stackd's injected
# --served-model-name). We only make the cache tree exist and exec the server.
set -euo pipefail

# Compiler / JIT cache tree — CACHE_BASE (=/cache) is a bind mount in the real
# deployment so the second cold start skips torch.compile + FlashInfer JIT.
: "${CACHE_BASE:=/cache}"
mkdir -p "${CACHE_BASE}"/{huggingface,torch,torchinductor,triton,cuda,flashinfer,sglang/jit,torch_extensions}

# Phase-2 NIXL FILE persistence writes here when the --hicache-storage-backend
# nixl flags are added back to cmd_extra; harmless to pre-create.
[ -n "${SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR:-}" ] && \
    mkdir -p "${SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR}" || true

# Optional NVMe-backed PLE (pennyroyal v2.5.0, NVME-PLE.md). The fork's own
# launchers (configs/pennyroyal/serve-flash-next*.sh) source ple-backend.sh to
# wire this up; stackd invokes `sglang serve` directly via cmd_extra, so that
# wiring is replicated here instead. First OOM'd attempt at this (2026-09-12)
# turned out to be silently running the default RAM path the whole time,
# because none of this had happened -- SGLANG_PLUGINS/PYTHONPATH were never
# set, so `import sglang_ssd_stream` was never reachable. Requires the model
# YAML's cmd_extra to ALSO point --model-path at the prepared overlay (not the
# original checkpoint) and to OMIT --ple-offload-embedding -- ple-backend.sh's
# nvme branch reassigns TARGET_MODEL and clears PLE_ARGS for exactly that
# reason; this script can't rewrite argv, so those two must be set correctly
# by the caller (see coding-img.yaml).
PENNY_PLE_BACKEND="${PENNY_PLE_BACKEND:-ram}"
if [ "${PENNY_PLE_BACKEND}" = "nvme" ]; then
    : "${PENNY_PLE_NVME_MODEL:?Set PENNY_PLE_NVME_MODEL to the prepared NVMe snapshot}"
    : "${PENNY_PLE_SOURCE_MODEL:?Set PENNY_PLE_SOURCE_MODEL to the original checkpoint path (for the preflight)}"
    PENNY_PLE_PLUGIN_DIR="${PENNY_PLE_PLUGIN_DIR:-/opt/pennyroyal/.ple-nvme}"
    [ -f "${PENNY_PLE_PLUGIN_DIR}/sglang_ssd_stream/plugin.py" ] || {
        echo "Optional NVMe reader missing; see tools/ple_nvme/README.md" >&2
        exit 1
    }
    if [ -n "${SGLANG_PLUGINS:-}" ] && [ "${SGLANG_PLUGINS}" != "ssd_stream" ]; then
        echo "NVMe PLE recipe cannot combine unqualified SGLANG_PLUGINS" >&2
        exit 1
    fi
    export SGLANG_PLUGINS=ssd_stream
    export PYTHONPATH="${PENNY_PLE_PLUGIN_DIR}:/opt/pennyroyal/python${PYTHONPATH:+:${PYTHONPATH}}"
    echo "Checking NVMe PLE artifact and reader compatibility..." >&2
    CUDA_VISIBLE_DEVICES='' /opt/pennyroyal/.venv/bin/python \
        /opt/pennyroyal/scripts/pennyroyal/check_ple_nvme.py \
        --source "${PENNY_PLE_SOURCE_MODEL}" --prepared "${PENNY_PLE_NVME_MODEL}" >&2
fi

exec sglang serve "$@"
