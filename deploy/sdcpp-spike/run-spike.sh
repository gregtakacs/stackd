#!/usr/bin/env bash
# Launch (or relaunch) the Phase-0 sd.cpp spike server on the Strix Halo iGPU.
#
# NOT production: stackd never manages this container. It exists so the fp8mixed /
# turbo-LoRA / masked-edit questions can be answered before any engine adapter,
# YAML, or ladder entry is written. See README.md in this directory.
#
#   ./run-spike.sh [hipblas|vulkan]     (default: hipblas)
#
# Safety notes, all load-bearing:
#  * --memory cap is the guard that lets this run while comfyui-rocm + an LLM are
#    already resident on the box. The 2026-09-02 hard-hang was a ComfyUI/ROCm run
#    that OOM'd the HOST; here an overshoot OOM-kills THIS container only. Their own
#    bench.py reads the container's cgroup peak, which is evidence GTT charges land
#    in that cgroup -- so the cap bounds device memory too, not just RSS.
#  * models mounted :ro. Nothing here may write to the shared ComfyUI model tree.
#  * distinct container name + its own port so it cannot shadow comfyui-rocm(:8188)
#    or stackd's MCP(:8000), and nothing in stackd's config is touched.
set -euo pipefail

BACKEND="${1:-hipblas}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS="${MODELS:-/home/greg/docker/appdata/comfyui/models}"
OUT="$HERE/out"
PORT="${PORT:-12345}"
NAME="sdcpp-spike"

# The gate's artifacts -- the ones the spike is about, all already on disk.
# UNET is overridable so the same launcher can serve the quant A/B (ab-quant.sh):
# sd.cpp picks the diffusion weights at server START, not per request, so an A/B needs
# two server instances rather than two payloads.
UNET="${UNET:-$MODELS/diffusion_models/flux2_dev_fp8mixed.safetensors}"
TE="${TE:-$MODELS/text_encoders/mistral_3_small_flux2_bf16.safetensors}"
VAE="${VAE:-$MODELS/vae/flux2-vae.safetensors}"
LORA_DIR="${LORA_DIR:-$MODELS/loras}"


# The ComfyUI *_fp8 / *_fp4_misted text-encoder repacks are MISSING model.norm.weight
# (verified by parsing their safetensors headers) -- ComfyUI tolerates that, sd.cpp
# hard-fails metadata validation on it:
#   [ERROR] model_manager.cpp:759 - Conditioner model tensor
#           'text_encoders.llm.model.norm.weight' not in model metadata
# So the TE here is the bf16 repack, which does carry it. The UNET stays fp8mixed --
# that is the half whose precision mattered, and sd.cpp loads it natively.
if [[ "$TE" == *"_fp8.safetensors" || "$TE" == *"_fp4_mixed.safetensors" ]]; then
  echo "refusing: ComfyUI fp8/fp4 TE repacks lack model.norm.weight; use the bf16 repack" >&2
  exit 2
fi

# Resolve the iGPU render node by PCI vendor (0x1002 = AMD), NOT by AI-STACK/.env's
# IGPU_RENDER_NODE: that value is /dev/dri/renderD128, and its own comment says
# "fallback only -- dynamic detection overrides this". On this box renderD128 is PCI
# 66:00.0 = vendor 0x10de (the NVIDIA RTX PRO 6000) while renderD129 is bf:00.0 = 0x1002
# (the AMD 8060S). Passing the nvidia node makes RADV print "ggml_vulkan: No devices
# found" and the process then runs silently on CPU, reporting VRAM 0.00MB -- a number
# that looks like a legitimate low-memory result and is a lie. Defined before use.
amd_render_node() {
  for r in /sys/class/drm/renderD*; do
    [[ -e "$r/device/vendor" ]] || continue
    if [[ "$(cat "$r/device/vendor")" == "0x1002" ]]; then
      echo "/dev/dri/$(basename "$r")"
      return 0
    fi
  done
  echo "no AMD render node found (is the iGPU bound to amdgpu?)" >&2
  return 1
}

case "$BACKEND" in
  hipblas)
    IMAGE="sdcpp-spike:hipblas"
    # HIP needs the kfd node on top of the render node.
    DEVICES=(--device /dev/kfd --device "$(amd_render_node)")
    ;;
  vulkan)
    IMAGE="sdcpp-spike:vulkan"
    DEVICES=(--device "$(amd_render_node)")
    ;;
  *) echo "usage: $0 [hipblas|vulkan]" >&2; exit 2 ;;
esac

# WHY this probes sysfs instead of trusting IGPU_RENDER_NODE:
#   AI-STACK/.env ships IGPU_RENDER_NODE=/dev/dri/renderD128 and its OWN comment says
#   "fallback only -- dynamic detection overrides this". On this box renderD128 is
#   PCI 66:00.0 = vendor 0x10de (the NVIDIA RTX PRO 6000) and renderD129 is bf:00.0 =
#   0x1002 (the AMD 8060S). Handing sd.cpp the nvidia node makes RADV report
#   "ggml_vulkan: No devices found" and the process silently runs on CPU -- reported
#   VRAM 0.00MB, which looks like a legit low-memory result and is a lie. Always resolve
#   the render node by PCI vendor, never by the .env number.
amd_render_node() {
  for r in /sys/class/drm/renderD*; do
    [[ -e "$r/device/vendor" ]] || continue
    if [[ "$(cat "$r/device/vendor")" == "0x1002" ]]; then
      echo "/dev/dri/$(basename "$r")"
      return 0
    fi
  done
  echo "no AMD render node found (is the iGPU bound to amdgpu?)" >&2
  return 1
}

docker rm -f "$NAME" >/dev/null 2>&1 || true
mkdir -p "$OUT"

docker run -d --name "$NAME" --init \
  "${DEVICES[@]}" \
  --group-add "${VIDEO_GID:-44}" --group-add "${RENDER_GID:-990}" \
  --security-opt seccomp=unconfined \
  --memory=20g --memory-swap=24g \
  --ulimit nofile=65536:65536 \
  -v "$MODELS:/models:ro" \
  -v "$OUT:/work/out" \
  -p "127.0.0.1:$PORT:1234" \
  "$IMAGE" \
    --diffusion-model "/models/${UNET#"$MODELS"/}" \
    --vae "/models/${VAE#"$MODELS"/}" \
    --llm "/models/${TE#"$MODELS"/}" \
    --lora-model-dir "/models/${LORA_DIR#"$MODELS"/}" \
    --listen-ip 0.0.0.0 --listen-port 1234 \
    --params-backend diffusion=disk,te=disk \
    --diffusion-fa \
    -v

CID="$(docker inspect -f '{{.Id}}' "$NAME")"
echo "container $NAME  id ${CID:0:12}"
echo "cgroup peak: /sys/fs/cgroup/system.slice/docker-$CID.scope/memory.peak"
echo "$CID" > "$HERE/.cid"
