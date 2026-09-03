# stackd — the orchestrator. Runs alongside a scoped docker-API proxy,
# creates the CUDA llama.cpp engine container, adopts the rest.
#
#   nvidia-smi is injected by the NVIDIA container runtime (NVIDIA_DRIVER_
#   CAPABILITIES=utility) — not installed here.
#   rocm-smi is best-effort; without it the iGPU probe falls back to sysfs/free.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

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
