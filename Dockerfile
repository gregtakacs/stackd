# stackd — the orchestrator. Runs alongside a scoped docker-API proxy,
# creates the CUDA llama.cpp engine container, adopts the rest.
#
#   nvidia-smi is injected by the NVIDIA container runtime (NVIDIA_DRIVER_
#   CAPABILITIES=utility) — not installed here.
#   The dashboard's live iGPU stats (GET /gpu -> igpu0) read the amdgpu sysfs
#   node first (needs the container to see /dev/dri + the always-mounted /sys),
#   with rocm-smi installed below as a fallback. Neither works unless the compose
#   service grants /dev/kfd + /dev/dri and the video/render groups.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg tini \
    && rm -rf /var/lib/apt/lists/*

# rocm-smi — a *fallback* for the iGPU telemetry (the amdgpu-sysfs reader in
# telemetry.py is primary and needs no package). Its own best-effort apt layer:
# a repo hiccup or a non-AMD base must not break the image build. Pin ROCM_APT to bump.
ARG ROCM_APT=6.2.4
RUN set -eu; \
    ( curl -fsSL https://repo.radeon.com/rocm/rocm.gpg.key | gpg --dearmor > /usr/share/keyrings/rocm.gpg \
      && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${ROCM_APT} jammy main" \
         > /etc/apt/sources.list.d/rocm.list \
      && apt-get update \
      && apt-get install -y --no-install-recommends rocm-smi-lib \
      && ln -sf /opt/rocm/bin/rocm-smi /usr/local/bin/rocm-smi ) \
    || echo "WARN: rocm-smi install skipped — iGPU telemetry falls back to amdgpu sysfs"; \
    rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/rocm/bin:$PATH

RUN pip install --no-cache-dir "pyyaml>=6"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY stackd ./stackd
COPY config ./config
# [imagegen] pulls mcp/httpx/pillow/uvicorn/starlette for the embedded image MCP tools
# (was the standalone comfyui-mcp container).
RUN pip install --no-cache-dir -e ".[imagegen]"

ENV PYTHONUNBUFFERED=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=utility \
    STACKD_HOST=0.0.0.0 \
    STACKD_PORT=11444 \
    STACKD_DB=/data/stackd.db \
    STACKD_STATE=/data/state.json

# cli.py's DEFAULT_CONFIG resolves to /app/config here, so `stackctl <cmd>`
# (via `docker exec`) and the CMD below both pick it up with no -C flag.
EXPOSE 11444
EXPOSE 8000

ENTRYPOINT ["tini", "--", "python3", "-m", "stackd.cli"]
CMD ["serve"]
