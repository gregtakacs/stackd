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

exec sglang serve "$@"
