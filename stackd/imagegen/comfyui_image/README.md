# stackd/imagegen/comfyui_image/ — the ComfyUI runtime

stackd creates **both** ComfyUI containers itself (from
`config/models/{everyday,coding}-image.yaml` `container:` blocks). They run the same
graphs (`../workflow_graphs/`) and the same custom node, differing only in GPU:

| model (container)         | GPU                              | base | notes |
|---------------------------|----------------------------------|------|-------|
| `everyday-image` (`comfyui-cuda`) | NVIDIA RTX PRO 6000, CUDA | stock `yanwk/comfyui-boot` megapak | `clipseg_mask.py` bind-mounted in; KJNodes/Florence2 come from the megapak |
| `coding-image` (`comfyui-rocm`)   | AMD Radeon 8060S iGPU, ROCm | `Dockerfile.igpu` → `comfyui-rocm:rocm7141` | built by `stackctl build coding-image` |

`stackd/imagegen/tools.py` (`_current_mode`) picks which one a request lands on:
resident `flux2-dev*` ⇒ everyday, else coding.

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
  `stackctl build coding-image` tars it and streams it to the Docker `/build` API (via
  `ai-socket-proxy` `BUILD=1`).

- **`Dockerfile.egpu`** — CUDA equivalent, **not wired**. Point a future self-contained
  CUDA image at this instead of bind-mounting `clipseg_mask.py` into the megapak.

## Iterating on `clipseg_mask.py`

- **comfyui-cuda**: it's bind-mounted `:ro` — restart the container to reload
  (`docker rm -f comfyui-cuda`; stackd recreates it on the next converge).
- **comfyui-rocm**: `docker exec stackd stackctl build coding-image`, then
  `docker rm -f comfyui-rocm` so stackd recreates from the new image.
