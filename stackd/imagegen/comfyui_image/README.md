# stackd/imagegen/comfyui_image/ — the ComfyUI runtime

The elastic image tier (`config/media/image.yaml`) has one `containers:` block per
GPU backend; stackd creates whichever one lands on the device with free VRAM. Both
run the same graphs (`../workflow_graphs/`) and the same custom node:

| backend | base | notes |
|---------|------|-------|
| `cuda`   | stock `yanwk/comfyui-boot` megapak | `clipseg_mask.py` bind-mounted in; KJNodes/Florence2 come from the megapak |
| `vulkan` | `Dockerfile.igpu` (local build) | ROCm under the hood; `docker exec stackd stackctl build image:vulkan` |

`stackd/imagegen/tools.py` runs each request against whichever ComfyUI is resident —
read from `Manager.capabilities()["image"]` (`active_model` + the `capabilities` verb
list). No "coding"/"chat" mode: the tool uses the resident model, and if it
doesn't declare the requested verb it asks the tier to swap to one that does.

## Files

- **`clipseg_mask.py`** — the `CLIPSegMask` node: text → mask for the masked-edit graph
  (`../workflow_graphs/edit/*-inpaint.json`, node id 14). Stock
  `transformers.CLIPSegForImageSegmentation` (`CIDAS/clipseg-rd64-refined`, no
  `trust_remote_code`). Replaced Florence-2, which segments garbage on ROCm/gfx1151.
  Adaptive (peak-normalised) threshold, low-res morphological close, grow/feather sized
  to the object (`grow_frac` / `feather_frac` of `sqrt(mask area)`). **Single source of
  truth** — comfyui-cuda bind-mounts it, comfyui-rocm bakes it in.

- **`Dockerfile.igpu`** — the ROCm image. `git clone ComfyUI` + Manager + KJNodes +
  Florence2 + `COPY clipseg_mask.py`, on AMD's `rocm/pytorch` base. Build context is
  **this directory**, mounted into stackd at `/app/stackd/imagegen/comfyui_image`;
  `stackctl build image:vulkan` tars it and streams it to the Docker `/build` API (via
  `ai-socket-proxy` `BUILD=1`).

- **`Dockerfile.egpu`** — CUDA equivalent, **not wired**. Point a future self-contained
  CUDA image at this instead of bind-mounting `clipseg_mask.py` into the megapak.

## Iterating on `clipseg_mask.py`

- **comfyui-cuda**: it's bind-mounted `:ro` — restart the container to reload
  (`docker rm -f comfyui-cuda`; stackd recreates it on the next converge).
- **comfyui-rocm**: `docker exec stackd stackctl build image:vulkan`, then
  `docker rm -f comfyui-rocm` so stackd recreates from the new image.
