#!/usr/bin/env bash
# Latency A/B: fp8mixed safetensors vs the Q4_K_M GGUF -- same backend, same seed, same
# prompt, same sigma schedule, same LoRA. Separates the two candidate explanations for
# the ~24 s/it measured on Vulkan0:
#   (i)  RADV has no native low-precision matmul (its own device report says
#        bf16:0, fp4:0, int-dot:0), so every f8_e4m3 weight gets dequantized per use; or
#   (ii) attention is simply expensive here because --diffusion-fa is a no-op on Vulkan
#        (docs/performance.md lists only cpu, cuda/rocm, metal).
# Q4 much faster -> (i): the quantization tier is a real lever and a GGUF earns its
# download. Same speed -> (ii): attention is the wall, so chase HIPBLAS/FA, not size.
#
# The UNET is chosen at server START, not per request, so each arm gets its own container
# via run-spike.sh with UNET overridden.
#
# Run ONLY while nothing else is on the iGPU: these are wall-clock numbers and a
# concurrent job contaminates them without any error.
#
#   ./ab-quant.sh [hipblas|vulkan]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="${1:-vulkan}"
MODELS="${MODELS:-/home/greg/docker/appdata/comfyui/models}"
PORT_BASE="${PORT_BASE:-12400}"

wait_ready() {
  local port="$1" i
  for i in $(seq 1 90); do
    curl -s --max-time 3 "http://127.0.0.1:${port}/sdcpp/v1/capabilities" >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "server on :${port} never became ready" >&2
  return 1
}

run_arm() {
  local label="$1" unet="$2" port="$3"
  local out="$HERE/out-ab-${label}"
  echo "=== ${label}: ${unet##*/} ==="
  rm -rf "$out"; mkdir -p "$out"
  BACKEND="$BACKEND" UNET="$unet" PORT="$port" "$HERE/run-spike.sh" "$BACKEND" >/dev/null
  wait_ready "$port"
  SDCPP_BASE="http://127.0.0.1:${port}" python3 "$HERE/spike.py" a \
      --size 512 --seed 1234 --timeout 1800 --out "$out" 2>&1 | tail -1
  docker rm -f sdcpp-spike >/dev/null 2>&1 || true
}

run_arm fp8mixed "$MODELS/diffusion_models/flux2_dev_fp8mixed.safetensors" "$PORT_BASE"
run_arm q4       "$MODELS/diffusion_models/flux2-dev-Q4_K_M.gguf"          "$((PORT_BASE + 1))"

echo
echo "Both arms are 7 sampling steps at 512x512, seed 1234. Compare the 'secs' the two"
echo "print: TE pass and the disk-mode weight read (see README trap 8) are common to both,"
echo "so the DELTA is the matmul-path difference this A/B exists to measure."
