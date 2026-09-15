# sd.cpp spike — replacing ComfyUI for `flux2-dev-turbo` on the iGPU

Phase-0 feasibility gate. Nothing here is wired into stackd: no engine adapter, no
`config/media/image.yaml` change, no ladder entry. It exists to answer, with numbers and
pixels, whether stable-diffusion.cpp can run the `flux2-dev-turbo` pipeline on the Strix
Halo iGPU — which ComfyUI cannot, because ComfyUI's "unload to CPU" copies weights into
the very RAM the device just unmapped and holds the model twice (measured peak
**121.6 GB host RAM** on a 124 GB box, refused outright by
`solver.py::host_ram_headroom`; see the `vulkan:` block comments in
`AI-STACK/config.local/media/image.yaml`).

    ./run-spike.sh vulkan                     # starts container sdcpp-gpu on :12347
    SDCPP_BASE=http://127.0.0.1:12347 python3 spike.py all --size 512

One model must do **all three** verbs (generate / stylize / edit). Two passing and one
failing is a fail — that constraint is the whole point of the exercise.

## Verified 2026-09-14

| # | Question | Result |
|---|---|---|
| 1 | Does the ComfyUI **fp8mixed UNET** load, or must we drop to Q4? | **Loads natively.** `flux2_dev_fp8mixed.safetensors` reports `Diffusion model weight type stat: f32: 256 \| bf16: 171 \| f8_e4m3: 128` — the same 128 fp8 tensors its `__metadata__._quantization_metadata` declares. No precision downgrade required. |
| 2 | Does the ComfyUI **turbo LoRA** apply? | **Yes.** `Flux_2-Turbo-LoRA_comfyui.safetensors` → `loading 340/340 tensors`, `apply lora at runtime`, 0.05 s. ComfyUI-namespaced keys map fine; `--lora-apply-mode` auto-picked `at_runtime` (correct for quantized weights). |
| 3 | Does the iGPU show up? | **Yes**, via `Vulkan0 — Radeon 8060S Graphics (RADV STRIX_HALO)`, `uma: 1 \| fp16: 1 \| bf16: 0 \| fp4: 0 \| int dot: 0 \| matrix cores: KHR_coopmat`. |
| 4 | NVFP4? | **Not usable on this runtime — but the reason is narrower than it first looks.** sd.cpp has no NVFP4 *ingest* path: zero `nvfp4`/`e2m1` hits across its model IO and safetensors/GGUF loaders, so there is no way to hand it an NVFP4 tensor; and the driver independently reports `fp4: 0` for this device. Note the distinction though — building the HIP backend compiles `ggml-cuda/template-instances/mmq-instance-nvfp4.cu.o`, i.e. NVFP4 **matmul kernels exist** in ggml-hip; they are simply unreachable without a loader/quantizer that produces that dtype. So `mistral_3_small_flux2_fp4_mixed.safetensors` (398 U8 tensors) stays un-loadable, and the TE choice remains `_fp8`/`_bf16`. NVFP4 proper stays a ComfyUI/torch feature (`comfy/quant_ops.py` `TensorCoreNVFP4Layout`, Blackwell-only). |
| 5 | **Host RAM** — the thing that actually blocks dev-turbo on the iGPU | **Solved, decisively.** With `--params-backend all=disk`: container RSS **345 MiB**, cgroup peak **8.08 GB**, and the 33 GB text encoder is prepared → used → *released* (`model_manager.cpp:1159`). Against ComfyUI's 121.6 GB this is the entire ballgame. |
| 6 | **Speed — Vulkan (the wrong backend)** | `24.3-24.7 s/it` at 512² (7 steps). Useless for this model: extrapolating ~4× tokens, 1024² lands near 13 min, past `imagegen/config.py::TIMEOUT_S=600`. `gpu_busy_percent` held 99-100 while container CPU was 0.18%, so this was genuine iGPU compute — the *backend* was the wall, not the model. Cause: `diffusion_flash_attn: false` (flag silently ignored, trap 7) plus `bf16:0 fp4:0 int-dot:0` (no native low-precision matmul, so every one of the 128 `f8_e4m3` weights is dequantized per use). |
| 6b | **Speed — HIPBLAS (the answer)** | **`8.32 s/it` @512² and `25.9-26.3 s/it` @1024² — a 2.9× speedup over Vulkan on the same weights.** Full runs: **136.7 s** @512², **237.7 s @1024²** (2.06 MB PNG), both `ok: true`. 1024² clears `TIMEOUT_S=600` with ~2.5× headroom, so **dev-turbo on the iGPU is viable at production resolution** — which Vulkan was not. `Using flash attention in the diffusion model` confirms FA is live on this backend. Baselines for context: dev-turbo on the eGPU via ComfyUI is 33.8 s @1024² (so the iGPU is ~7× slower than the dGPU but needs none of the RAM), klein on this iGPU in ComfyUI is 169.9 s @1024². |
| 6c | Do the two backends differ in **quality**? | **No.** Same seed, same prompt, same artifacts: the 512² images from Vulkan0 and ROCm0 are compositionally identical. The 2.9× is pure backend efficiency, not a quality trade. |
| 7 | Can we build the HIPBLAS path on `rocm/pytorch`? | **No — but on `rocm/dev-ubuntu-24.04` yes, and it works.** That first attempt died because `rocm/pytorch` is a *pip-wheel* ROCm (`hipcc` is a `/opt/venv` shim, no `/opt/rocm/lib/llvm`, `dpkg` shows no `rocm-dev`/`rocBLAS`/`hipBLAS`), so `docs/build.md`'s clang recipe can't run: `The C compiler identification is unknown`. Rebuilt on `rocm/dev-ubuntu-24.04:7.2.4-complete` (verified via `docker manifest inspect`; 7.2.4 is the newest in `repo.radeon.com/apt` — there is no 7.14.x, so the host's "rocm7.14.1" is a PyTorch-side build id). `ggml_cuda_init: found 1 ROCm devices (Total VRAM: 126976 MiB)`, backend `#0: ROCm0`. Four packaging gotchas, all in traps 9-12. |

## Gate results — 2026-09-14, all three passed on the iGPU

`python3 spike.py all --size 512` against `sdcpp-gpu` (Vulkan0, `--params-backend all=disk`),
fp8mixed UNET + bf16 Mistral TE + flux2-vae + turbo LoRA, seed 1234, fal's 8-value
`custom_sigmas`:

| gate | result | secs | judgement |
|---|---|---|---|
| (a) generate | `ok: true`, 505 KB PNG | 204.4 | **PASS on quality.** Coherent photograph with every prompted element (desk, laptop, mug, plant, books, window daylight), correct exposure, no banding or colour cast. This is the proof that sd.cpp both recognizes **and correctly dequantizes** the 128 `F8_E4M3` UNET tensors — the file carries zero `.comfy_quant`/U8 scale tensors, so scales came from the metadata header. **No pivot to Q4_K_M or any GGUF download is required.** |
| (b) stylize | `ok: true` | 161.4 | **PASS once tuned.** At `--strength 0.6` the requested *"blue hour in light rain, moody cinematic lighting"* barely took (still bright daylight); at **0.75** it is a proper restyle — cool desaturated light, foliage shifted to autumn — with composition held exactly (laptop, both speckled pots, books, desk all in place). `encode_first_stage completed, taking 0.58s` confirms the init image is really VAE-encoded. **Guidance for the ladder entry: don't carry ComfyUI's low stylize strength over** — with only 7 sigma steps there is little noise budget, so anything below ~0.7 reads as a no-op. Also note `custom_sigmas` is sent for the t2i path; for img2img, letting sd.cpp derive its own schedule from `strength` is the honest comparison. |
| (c) edit | `ok: true` | 323.8 | **PASS.** The masked region came back a matte red ceramic mug as prompted; the two speckled pots that fell inside the box were correctly regenerated away; everything outside kept its position. `flux compute buffer` rose 1592→3057 MB, consistent with `ref_images` widening the sequence. |

**The number that matters most:** `container_peak_ram_gib: 11.69` — against ComfyUI dev-turbo's
**121.6 GB**, which is what `solver.py::host_ram_headroom` refuses outright on this 124 GB box.
~10x reduction, so the pick that could never coexist with the everyday-profile 27B now can.

**Stylize strength is a cliff, not a dial — measure it, don't inherit it.** Same source image, same
seed, same prompt, only `strength` varying:

| strength | outcome |
|---|---|
| 0.60 | effectively a no-op — still bright daylight, requested mood absent |
| 0.75 | **correct** — cool desaturated light, foliage to autumn, composition held exactly |
| 0.90 | **catastrophic** — a completely different photograph (two figures under umbrellas in rain). The prompt's *"light rain"* leaked from *style* into *content* and scene identity was destroyed |

So the usable window for stylize on the 7-step turbo schedule is narrow (~0.7-0.8). This is the
single most important tuning constraint in this spike: it is a *silent* quality failure — every
run returns `status: completed` with a valid PNG at any strength, and only looking at pixels
catches it. It also proves img2img is genuinely honored (strength really moves the noise budget)
rather than ignored. Practical consequence: the ladder entry must carry a measured stylize
strength per model, and `bench.py`'s sdcpp submission should assert the stylize strength is in
the calibrated band rather than trusting a value copied from the ComfyUI graph.

**Outside-mask fidelity: `mean_abs_sum_diff 6.85`** across 196,608 outside-mask pixels (~2.3 per
channel of 255) — i.e. the unmasked ~75% returns at the VAE round-trip noise floor. This is the
empirical case for the per-step `denoise_mask` pin (`diffusion_engine.cpp:2519`) over ComfyUI's
end-of-run `ImageCompositeMasked`, and it is the reason dev-turbo edit needed `ColorMatchV2`
hacks that sd.cpp does not. Caveat: the synthetic box mask is hard-edged, so the mug picked up
visible red ghosting where it bleeds past the boundary. CLIPSeg masks arrive with grow/feather
post-processing already in stackd's pipeline; re-check this artifact with a real feathered mask
before calling (c) production-grade.

channel of 255) — i.e. the unmasked ~75% returns at the VAE round-trip noise floor. This is the
empirical case for the per-step `denoise_mask` pin (`diffusion_engine.cpp:2519`) over ComfyUI's
end-of-run `ImageCompositeMasked`, and it is the reason dev-turbo edit needed `ColorMatchV2`
hacks that sd.cpp does not. Caveat: the synthetic box mask is hard-edged, so the mug picked up
visible red ghosting where it bleeds past the boundary. CLIPSeg masks arrive with grow/feather
post-processing already in stackd's pipeline; re-check this artifact with a real feathered mask
before calling (c) production-grade.

## Traps — each one cost real time, all of them look like something else

1. **`IGPU_RENDER_NODE` is wrong on this box.** `/dev/dri/renderD128` is PCI `66:00.0`, vendor `0x10de` — the **NVIDIA dGPU**. The iGPU is `renderD129` (`bf:00.0`, vendor `0x1002`). The `.env` comment says "fallback only — dynamic detection overrides this", and stackd does resolve it dynamically (`stackd-code-autocomplete` gets `renderD129`); a hand-run container that trusts the `.env` value gets `ggml_vulkan: No devices found` and then **silently runs on CPU reporting `VRAM 0.00MB`** — a number that looks like a plausible low-memory result. `run-spike.sh` now resolves the render node by PCI vendor for exactly this reason.
2. **The ComfyUI fp8/fp4 text-encoder repacks are incomplete.** `mistral_3_small_flux2_fp8.safetensors` and `..._fp4_mixed.safetensors` both **omit `model.norm.weight`** (verified by parsing their headers); ComfyUI tolerates that, sd.cpp hard-fails: `Conditioner model tensor 'text_encoders.llm.model.norm.weight' not in model metadata`. Use the **bf16** repack (495 tensors, norm present). The UNET stays fp8mixed — that is the half whose precision mattered.
3. **Job results are `result.images[].b64_json`, a list of objects** (api.md:868), *not* `result.b64_json`. Reading the wrong key yields "completed with no image" after a full successful 7/7 sample — a false negative that looks like an engine failure.
4. **`custom_sigmas` means N−1 steps.** Passing fal's published 8-value list yields 7 steps and two server `WARN`s (`total_steps != custom_sigmas_count - 1`). Not an error, but it is a real semantic difference from the ComfyUI graph's `Flux2Scheduler steps=8`, so a like-for-like comparison has to send 9 values.
5. **`/sdcpp/v1/capabilities` is static.** `features_by_mode.img_gen` advertises `mask_image`/`ref_images`/`lora` from a fixed table (`routes_sdcpp.cpp:175`) regardless of model. It proves the route exists; it proves nothing about Flux.2.
6. **Don't hardcode `VK_ICD_FILENAMES`.** Upstream's ICD is `radeon_icd.json`; pointing the env var at `radeon_icd.x86_64.json` yields the same deceptive zero-device state as trap #1.
7. **`--diffusion-fa` is silently ignored on Vulkan.** The startup dump reads back `diffusion_flash_attn: false` even though the flag was passed, matching `performance.md`'s support list ("cpu, cuda/rocm, metal"). No error, no warning — you just pay ~2x attention cost and have to notice by reading the echo. Combined with the device's `bf16: 0 | fp4: 0 | int dot: 0` (no native low-precision matmul on Vulkan here), this is why 24 s/step: HIPBLAS is not a refinement, it is the backend with the fp8 matmul ISA the fp8mixed weights actually want.
8. **`--params-backend disk` is not free — it re-reads the weights.** Observed 7 `loading tensors completed` events across only **2** generations, each ~35 GB from NVMe (~245 GB read for two images; ~7-13 s each at 2.7-4.5 GB/s). Peak RAM is the thing being bought, and it is bought with I/O on every request. Before production, re-measure with a resident tier (`--max-vram` / `--auto-fit` / `--offload-to-cpu` / `--mmap`) and use `--profile` to get the per-stage split — `examples/common/common.cpp` advertises exactly those knobs (`--auto-fit`, `--max-vram`, `--offload-to-cpu`, `--mmap`, `--vae-tiling`, `--keep-seg-compute-buffer`, `--preserve-weights-on-shrink`), which is how a 32B model fits at all on a unified-memory 124 GB box.

## Traps from the ROCm/HIPBLAS build — each cost a full rebuild cycle

9. **`CMAKE_BUILD_WITH_INSTALL_RPATH=ON` without `CMAKE_INSTALL_RPATH` embeds an EMPTY rpath.**
    The symptom is exit 127 `error while loading shared libraries: libomp.so`, indistinguishable
    from "libomp is missing" — so the obvious fix (installing/copying libomp) makes the failure
    *move* rather than disappear, and you conclude the backend is broken. It isn't: the binary
    simply cannot find its own sibling. Upstream's `docker/Dockerfile` sets both flags together;
    copy that pairing, and note that in a Dockerfile `$ORIGIN` must be written `$$ORIGIN` or
    docker interpolates it to empty.
10. **`rocm-llvm` puts clang on NO PATH.** It installs only `/opt/rocm/lib/llvm/bin/clang{,++}`
    (clang-22); `command -v clang` returns nothing, and `/opt/rocm/bin` has no clang either.
    `-DCMAKE_C_COMPILER=clang` therefore dies at `The C compiler identification is unknown` —
    the *same* message `rocm/pytorch` gave, which is what nearly convinced me HIPBLAS was
    unbuildable here. Use absolute compiler paths (which is what `docs/build.md` itself does
    for MUSA).
11. **`libomp.so` does not exist under `/opt/rocm` at all** (`find /opt/rocm -name 'libomp.so*'`
    is empty), yet ggml-hip links it. Install `libomp-dev` and *vendor* `libomp.so` +
    `libomp.so.5` from `/usr/lib/llvm-*/lib` next to the binary. Beware `find ... -exec cp {} \;`
    in a Dockerfile RUN: docker's line-continuation parser eats the backslash, so `-exec`
    arrives unterminated — use a `for` loop. And `cp "$d"/libomp.so*` from only the first
    matching dir copies `libomp.so.5` but not the unversioned `libomp.so` the loader asks for.
12. **`ENV` is not applied to `RUN` layers**, so a `sd-server --version` smoke test runs in a
    different library environment than the container will, and fails for reasons the shipped
    image never hits. Test with `env LD_LIBRARY_PATH=...`, and keep the expensive compile in its
    own layer so fixing packaging bugs doesn't recompile 258 HIP targets. Also: `/sd-server`
    (vs `/opt/sd.cpp/build/bin/sd-server`) exists only in the re-layered Vulkan image — a
    hardcoded `--entrypoint /sd-server` in the launcher is exit 127 on the ROCm image, so both
    images must rely on their own `ENTRYPOINT`.
13. **`rocminfo` in a container may not see the iGPU**, which is why `docs/build.md` says to set
    `$GFX_NAME` manually; we pass `-DGPU_TARGETS/-DAMDGPU_TARGETS=gfx1151` explicitly and keep
    `rocminfo` only as a presence check. `rocm-device-info` is **not** a package in
    `repo.radeon.com/apt/7.2.4` — naming it aborts the whole apt transaction (exit 100).

## Gate (c) masked-edit — PASSED on the core claim (2026-09-14, HIPBLAS @1024²)

The whole reason to prefer sd.cpp over ComfyUI for masked edits is *where* the
outside-mask pixels get pinned. ComfyUI pastes the original back only at the END
(`ImageCompositeMasked`), which is why colour bled into untouched regions and forced
`ColorMatchV2` into the graph; sd.cpp pins the outside-mask latent EVERY denoise step
(`diffusion_engine.cpp:2519`, not gated on model version). Measured against a clean
t2i source and the synthetic box mask:

    outside-mask fidelity: mean_abs_sum_diff 5.81  (outside_pixels 786432 of 1048576)

5.81 sits **below** the ~6.85 VAE round-trip noise floor, so the untouched region
returns near-identical modulo the VAE — the mechanism works, no colour bleed, no
colour-match crutch needed. Caveats before this is production-grade: (a) this run used
the synthetic box mask, not a feathered **CLIPSeg** mask — retest with a real
segmentation mask (soft edges stress the pin differently); (b) the *inside*-box edit in
this artifact still ran under the forced t2i schedule (see trap 14), so the inside needs
the `--no-custom-sigmas` re-run too.

14. **Forcing the t2i turbo `custom_sigmas` onto an img2img request fights sd.cpp's own
    strength-derived schedule and ghosted the stylize.** The single confound in the
    original HIPBLAS stylize at 1024² / strength 0.75: it produced a **double-exposure**
    (duplicated laptop on the right, a third floating mug, doubled window frames).
    Vulkan's clean 0.75 restyle was only ever run at 512², so the backend was not the
    variable — *my request* was. With `init_image`+`strength`, sd.cpp derives its own
    sigma schedule; also sending the 8-value t2i `TURBO_SIGMAS` overrides that derivation
    and the two disagree. The tell is the server WARN `total_steps != custom_sigmas_count
    - 1`. `spike.py` now sends `custom_sigmas` only for gate (a); gates (b)/(c) use
    `sp_i2i` (sd.cpp's own schedule). An honest stylize/edit A/B must restyle from the
    clean **`out/gate_a.png`**, never an already-processed image (re-feeding a ghosted
    image confounds the comparison).

