# Qwen Image 2.1 (Comfy-Org repack) — implementation plan

Status: SHIPPED + LIVE-VALIDATED 2026-09-22 on feat/qwen-image-2.1 (stackd off
feat/comfy-toolbox; AI-STACK off main). Validated: true is SET. Live evidence on
comfyui-rocm @ comfyui-rocm:rocm7141-c37 (ComfyUI 0.37.0, ROCm 7.14.1, gfx1151):
  - bench: warm 67.3s @1024^2 t2i; added 6.8G GTT; container cgroup peak 32.23G
    -> prefer numbers shipped as footprint 10 / host_ram 36 (bench tool's own
    +3G-cushion suggestion; the pre-bench 28/45 ESTIMATE was rejected by the live
    host-RAM guard -- 45 > 39.1 free -- and the model had to be forced via
    --unsafe --backend vulkan to be MEASURED at all: that guard-refusal is the
    documented purpose of the --unsafe escape hatch, NOT a bug).
  - int8 confirmed REAL on hip: comfy-kitchen 0.2.35 'Native ops: convrot_w4a4,
    int8_tensorwise, asym_w4a8_int8'; TE loads full 8917 MB, DiT full 6920 MB
    (both == the int8 file sizes).
  - edit PASS 960x1280 (canvas followed source); stylize PASS 960x1280; klein
    generate+stylize regression PASS on 0.37 after the bump.
  - THE AUTOGROW TRAP (cost the most debug time, pinned by a smoke check): the
    API wire form for TextEncodeQwenImage21 reference images is the DOTTED
    per-slot key "images.image_1": [node, slot] -- exactly as the UI subgraph
    names it. The nested {"images": {"image_1": link}} form is SILENTLY DROPPED
    (min=0 makes it validate clean; execute then sees images={} and
    `latent_w = latent_h = resolution or 1024` yields a SQUARE latent -- an edit
    of a 960x1280 source came back 1024x1024 with no error anywhere). Only a
    DIMENSION ASSERTION catches this class; a returned-PNG check passes either
    way. Server-side mechanism: execution.py -> _io.build_nested_inputs folds
    dynamic_paths keys back into the dict before execute().
  - process trap hit along the way: stackd's imagegen code AND workflow_graphs
    are baked into the ai-stack-stackd image (no source bind-mount, see README
    'Web assets are live; Python is not') -- a graph edit needs
    `docker compose build stackd && up -d` before any live submit test can see
    it, and `up -d` also wipes /tmp inside the container (copied probe scripts
    included).

Scope: generate (t2i) + edit/stylize for `qwen-image-2.1`, int8_convrot weights, iGPU
(vulkan) only, coexisting in the prefer ladder with flux2-klein / flux2-dev-turbo /
ideogram4. bf16 A/B deferred (decision: ship int8 only first).

## Verified facts (checked against the live box, not assumed)

- Model card: https://huggingface.co/Comfy-Org/Qwen-Image-2.1 (base Qwen/Qwen-Image-2.1,
  license qwen-research). Published workflows:
  - t2i:  .../workflow_templates/main/templates/image_qwen_image_2_1_t2i.json
  - edit: .../templates/image_qwen_image_2_1_image_edit.json
  Both are UI-format WITH subgraphs (definitions.subgraphs) — not API format. Downloaded
  + parsed; real node sets recovered (see Graphs below). Templates ship UI-only: the
  installed comfyui_workflow_templates pkg has no *-api* files.
- Required core nodes TextEncodeQwenImage21 + QwenImage21Cache: ABSENT from the live
  comfyui-rocm (ComfyUI 0.34.0) /object_info. Verified present at tag v0.37.0
  (comfy_extras/nodes_qwen.py; absent in v0.34/0.35/0.36). v0.37.0 published 2026-09-21
  07:35Z (PR CORE-423 "feat: Qwen-image 2.1 support" + PR 15623 cudagraphs/w4a8).
- TextEncodeQwenImage21 schema (from source):
  inputs  clip, prompt, negative_prompt, vae(opt), resolution(opt, def 1024, step 32, 0=keep),
          images.image_1..image_16 (Autogrow)
  outputs positive(COND), negative(COND), latent(LATENT)  <- latent is image_1-sized
- CLIPLoader type "qwen_image" already offered in 0.34.0. UNETLoader/VAELoader/KSampler/
  VAEEncode/ImageScale/ImageScaleBy/SaveImage/VAEDecodeTiled/ColorMatchV2 all present.
- int8 on ROCm: live `comfy_kitchen.list_backends()` in comfyui-rocm shows hip available
  with quantize_int8_convrot_weight, dequantize_int8_convrot_weight_dtype, int8_linear,
  w4a8_int8_linear. README matrix confirms hip covers the int8/convrot paths (WMMA on
  RDNA3/3.5/4). torch 2.12.0+rocm7.14.1, device "AMD Radeon 8060S Graphics".
- v0.37.0 requirements pins comfy-kitchen==0.2.35, comfy-aimdo==0.5.5 (current container
  has 0.2.31/0.4.15). Both new pins publish plain manylinux/abi3/py3-none-any wheels -> no
  CUDA-only wheel problem for the ROCm base.
- Weights NOT on disk (models tree = 204G of flux2/ideogram4/qwen3vl_8b_fp8_scaled etc).
  Disk free on /home/greg/docker: 1.5T. Not a blocker.
- Sizes: DiT int8 6.76 GiB / bf16 13.25 GiB (~7B). TE qwen3vl_8b int8 8.71 / bf16 16.33
  (~8.7B). VAE bf16 0.63. Chosen int8 set total ~16.1 GiB.
  bf16 file is BF16, not fp16 (user said fp16) -> no fp16 overflow risk.
- Live tier: `stackctl image show` => resident flux2-klein (ready) on igpu0/vulkan;
  headroom cuda0 0G, igpu0 83.5G. flash-next (sglang-pennyroyal, whole card) is up.
  MemAvailable 24.6 GiB BUT that includes resident klein (user: evicting klein is the
  normal single-model swap, so int8 fits comfortably; do NOT judge fit by that number).
  Precedent caution: dev-turbo booked ~121.6G real host RAM on igpu0 => footprint numbers
  MUST come from bench, never from file sizes.
- Pools: HOST_RAM_GIB=124, HOST_RESERVE_GIB=12, IGPU_VRAM_BUDGET_GIB=90 (GTT ceiling),
  igpu0 vram_headroom_gib 3, CUDA_VRAM_GIB=95.6.
- MCP submit timeout: AI-STACK/docker-compose.yml sets TIMEOUT_S=600 on stackd.
  Precedent: ideogram4 on vulkan 169.9s @1024^2 (CUDA 13.7s). Qwen2.1 = 25 steps + ~7B DiT
  + 8B VL text encode -> watch for the 600s ceiling.
- Coexistence is ladder-based (one active_model per ComfyUI). Entry key `qwen-image-2.1`
  is collision-free: _resolve_pipeline substring would otherwise match the existing
  `qwen3vl_8b_fp8_scaled` TE / Qwen3.8 LLM names. Reached via image_model:"qwen..."
  (substring works) or `stackctl image use qwen-image-2.1`. Last in ladder => never
  auto-picked over the others, same convention that shelved ideogram4.
- Validator coupling: stackd/config/models.py::_check_media_tier + _missing_graphs => a
  prefer[] capability with no models.json graph FAILS the whole config load. models.json
  entry and prefer entry must land together.
- builder.build_image tags with c.image VERBATIM => must bump COMFYUI_ROCM_IMAGE to a NEW
  tag BEFORE building or it overwrites comfyui-rocm:rocm7141 and there is no rollback.
- COMFYUI_ROCM_REF is currently `master` (unpinned moving target) -> pin v0.37.0.

## Steps

0. Branches.
   cd LLM-Tools/stackd && git checkout -b feat/qwen-image-2.1 feat/comfy-toolbox
   cd AI-STACK && git checkout -b feat/qwen-image-2.1
   (this plan file -> stackd root next to TODO.md/README.md, per repo convention)

1. GATE 0 — runtime (stop here if this fails; graphs are worthless without the nodes)
   a. AI-STACK/.env: COMFYUI_ROCM_REF=v0.37.0, COMFYUI_ROCM_IMAGE=comfyui-rocm:rocm7141-c37
      (bump BEFORE build so rocm7141 survives as the one-line revert)
   b. docker exec stackd stackctl build image:vulkan   (DOCKER_API_URL already in env)
   c. stackctl reload ; bring comfyui-rocm up on the new tag
   d. verify /object_info has TextEncodeQwenImage21 + QwenImage21Cache; comfy_kitchen hip
      still available at 0.2.35; CLIPLoader type list has qwen_image
   e. REGRESSION on the rebuilt image before adding anything: klein generate + one real
      stylize, `stackctl image bench flux2-klein --sizes 1024`. (0.34->0.37 touches
      samplers and EmptyLatentImage defaults — harmless to us only because our graphs
      pin width/height explicitly.)

2. GATE 1 — weights into ${DOCKERDIR}/appdata/comfyui/models (shared cuda+vulkan mount)
   diffusion_models/qwen_image_2.1_int8_convrot.safetensors   (7,256,783,064 B)
   text_encoders/qwen3vl_8b_int8_convrot.safetensors          (9,350,798,360 B)
   vae/qwen_image_2.1_vae_bf16.safetensors                    (  675,509,688 B)
   sha256 vs HF LFS oids (cb74113c…, 8bfd0f6e…, bb21f747…). Then confirm all three appear
   in the container's /object_info combo lists for UNETLoader/CLIPLoader/VAELoader.

3. GRAPHS (API format; hand-converted from the official subgraphs, repo conventions)
   workflow_graphs/generate/qwen-image-2-1.json
   workflow_graphs/edit/qwen-image-2-1.json
   workflow_graphs/stylize/qwen-image-2-1.json
   Faithful core: UNETLoader(int8,weight_dtype default) + CLIPLoader(qwen3vl_8b_int8,
   type=qwen_image) + VAELoader(qwen vae) -> TextEncodeQwenImage21(clip,prompt,
   negative_prompt) -> KSampler(cfg 1, euler, simple, denoise 1, steps 25) ->
   VAEDecode -> SaveImage.
   Adaptations to this repo:
   - PrimitiveStringMultiline (prompt/negative) + PrimitiveInt (width/height/seed) front-
     ends so workflows.set_node patches by role with NO server change.
   - SaveImage NOT SaveImageAdvanced (the latter is frontend-only; would fail validation).
   - VAEDecodeTiled (tile 512/overlap 64) like every other graph here — matters more on
     the iGPU for the decode host-RAM spike.
   - negative = TE slot 1 with "" (NO ConditioningZeroOut, NO negative param — consistent
     with the existing tools).
   - filename_prefix QI21_txt2img / QI21_img2img / QI21_stylized.
   - generate: EmptyLatentImage(width,height) from width/height roles.
   - edit/stylize: source LoadImage -> TE images.image_1 WITH vae linked; sampler latent =
     TE slot-2 image-sized latent. This reproduces the template's
     ComfySwitchNode(switch=false)/resolution=0 path: canvas follows image_1, and
     comfyui_client.downscale_to_exact_size already made the upload exactly w x h.
     No VAEEncode / no denoise role -> edit_strength is a no-op (surface as a note).
   - edit keeps QwenImage21Cache(device,dtype). stylize adds ImageScaleBy so upscale_by
     still means something.
   OPEN ITEM (decided by first live submit, one-line fix either way): API key for the
   Autogrow input is "images.image_1" (mirrored from UI JSON) vs nested
   "images": {"image_1": …}.

4. REGISTRY — workflow_graphs/models.json entry "qwen-image-2.1"
   validated:false until a live run passes; tools generate/edit/stylize with role->node
   maps; edit has NO inpaint key (masked target_region must raise clean ToolUnsupported —
   no inpaint graph is published for this model); prompt_rewrite -> qwen_image_2_1.txt
   (decide json true/false like ideogram4 vs klein); _comment notes documenting the
   edit_strength no-op and the missing masked mode.

5. PER-MODEL GEOMETRY (small code change, tools.py + workflows.py)
   Optional `geometry: {multiple, max_pixels, practical_max_pixels, aspect_presets}` in
   models.json; _round16 -> _round_to(v, multiple); model-aware cap message (the current
   string hardcodes "Flux.2 Klein 9B's 4MP limit"); klein/dev keep today's numbers
   byte-identically. Qwen wants multiples of 32 (card says so; EmptyLatentImage can take
   any integer, and the TE rounds refs to 32 itself). Presets snapped: square 1312^2,
   landscape 2048x1568, widescreen 2048x1152, ultrawide 2048x832, tall 1152x2048…

6. CONFIG — append the prefer entry to BOTH stackd/config/media/image.yaml and
   AI-STACK/config.local/media/image.yaml (the overlay wins per-file; the local one is
   what actually runs), AFTER ideogram4:
     - active_model: qwen-image-2.1
       capabilities: [generate, stylize, edit]
       backends: [vulkan]                # cuda LATER: append cuda + numbers, no code change
       footprint_gib: { vulkan: <measured> }
       host_ram_gib:  { vulkan: <measured> }
   Start ~28 / ~45, then REPLACE with `stackctl image bench qwen-image-2.1 --sizes 1024`
   output + provenance comments in the same style as the neighbours. backends:[vulkan]
   only = structurally prevents cuda pickup now (_check_media_tier needs a footprint per
   backend). NOTE the honest failure mode: with flash-next up, host_ram_headroom may
   legitimately REFUSE the swap — that's the guard working; remedy is a lighter profile
   (image-gen / igpu-chat), not a smaller booking.

7. TOOLBOX GUARD — toolbox/engine.py::_pick_model falls back to _DEFAULT_MODEL when the
   resident pipeline declares no builtin inpaint graph (else a resident qwen/ideogram4
   makes the painted-mask/retouch path raise ToolUnsupported). Regression-test retouch
   after qwen becomes selectable.

8. DOCS — stackd/imagegen/prompts/qwen_image_2_1.txt rewrite system prompt carrying the
   card's own guidance: native 2K, prefer multiples of 32, transparent-image phrasing
   pattern, and <image1>/<image2> reference syntax for multi-image edit prompts. Add
   Qwen section to the imagegen README/tool_docs note.

9. TESTS
   offline: python3 tests/smoke_imagegen.py (extend it), and `stackctl validate` —
   validates the new prefer[] entry against models.json.
   smoke additions: models.json has qwen-image-2.1; every role id in its nodes map exists
   in the corresponding graph; every class_type in the 3 new graphs passes an allowlist of
   core/executable nodes (this check is what catches a SaveImageAdvanced-style mistake);
   qwen geometry rounds to 32 and klein still to 16.
   live: bench -> direct POST /prompt of each of the 3 graphs (bypass the MCP layer to
   isolate graph errors from tools bugs; this is also what settles images.image_1) ->
   MCP generate_image / edit_image / stylize_image against image_model:qwen ->
   REGRESSION klein + dev-turbo + ideogram4 still OK on the rebuilt 0.37 container ->
   flip validated:true with measured footprints.
   QwenImage21Cache tuning (iGPU-specific, do not assume): the node doc says device "auto
   uses spare VRAM, then RAM" — on this device RAM IS the VRAM, so auto double-books the
   same pool. Measure auto/cpu/off x default/int8; keep winner as graph default, document
   in the models.json comment.

## Deferred / out of scope (flag, don't hide)
- bf16 A/B: bench int8 vs bf16 vs (int8 DiT + bf16 TE). Expectation stated, NOT asserted:
  int8 should be FASTER here (half the weight traffic on an LPDDR5X-bound iGPU, and hip
  exposes real int8/convrot kernels rather than faking it); any softness more likely from
  the 8B VL TE (drives edit instruction-following) than the DiT. Revisit via an optional
  model_files override in models.json (differs in only 3 filename strings) rather than a
  4th duplicated graph. Also unexplored: w4a8 TE (6.31 GiB — the variant actually likely
  to degrade visibly) and the two dedicated PE TEs (qwen3.5_9b_…_pe_t2i/_pe_i2i) that the
  published template does NOT use (reason unknown).
- Masked target_region edits + toolbox painted-mask flow: no inpaint graph published.
- CUDA enablement later: append cuda to backends + numbers, bump COMFYUI_CUDA_IMAGE
  (cu130-megapak-pt211-20260921, pushed after v0.37.0, probably already has the nodes).

## Commands reference
  stackctl: docker exec stackd stackctl …   (build target name: image:vulkan)
  comfyui-rocm ip 192.168.90.23:8188 (no published port); stackd front :11444, MCP :8000
  models dir /home/greg/docker/appdata/comfyui/models (host)

## CUDA enablement (2026-09-22, done)

**Image:** `yanwk/comfyui-boot:cu130-megapak-pt211-20260921` — pulled, NOT
rebuilt. Its bundled ComfyUI is EXACTLY the v0.37.0 commit (`git describe` →
v0.37.0, 73c9bad4; the megapak tracks release commits, and this one shipped the
day of the release). The old pin (…20260910) bundled a 09-09 commit — pre-v0.37,
no qwen nodes, no kitchen. Revert = repoint COMFYUI_CUDA_IMAGE at …20260910
(still local).
  - GOTCHA: the megapak is a two-python image. Default `python3` = system 3.12;
    the ML stack (torch 2.11+cu130, comfy-kitchen 0.2.35) lives on 3.13 at
    /usr/bin/python3.13. An import test with the wrong interpreter looks like a
    missing package (this happened during verification; `pip3 show` said
    "installed in /usr/local/lib64/python3.13/…" while `python3` 3.12 got
    ModuleNotFoundError).
  - Pre-activation gate (all PASS, --cpu mode, zero GPU): node classes in
    /object_info; all three qwen graphs validated (incl. dotted images.image_1);
    shared models dir visible.

**The SM120/xformers bug (why `--disable-xformers`):** the megapak bundles
xformers, so ComfyUI v0.37.0 selects `attention_xformers` as the global
attention (the ROCm image has no xformers → SDPA). Qwen's DiT + TE both use the
global `optimized_attention`; their tensor-bias (masked) attention reaches
xformers' FMHA dispatcher, which on Blackwell (capability 12.0) has NO matching
operator — fa3F/cutlassF are built for <=9.0 ("too new"), fa2F 2.8.3 refuses
Tensor attn_bias — and `attention_xformers` does not fall back:
NotImplementedError, prompt dead in 0.04s. Fix is config-only: added
`--disable-xformers` to the cuda container's CLI_ARGS (documented flag in
comfy/cli_args.py:154) → global attention falls through to PyTorch SDPA, the
exact path the ROCm build runs. Trade (accepted, noted in both yamls):
dev-turbo/ideogram cuda attention also moves xformers→SDPA (fast on SM120 via
cuDNN; regression-benched dev-turbo after the flip: 10.6s @1024², PASS).
  - NOT a kitchen bug: the first (contaminated) bench misattributed this to
    comfy-kitchen because the fa3F/fa2F/cutlassF naming looked like the
    kitchen registry — the traceback shows xformers.ops.fmha.dispatch. Kitchen's
    int8_convrot linear kernels run fine on SM120 (the measured runs used them).

**Measured numbers (booked: footprint vulkan 13 / cuda 20, host_ram vulkan 18):**
  - cuda bench (0921 image, SDPA): cold load+warmup 26.5s; 1024² 12.1s first /
    4.6s warm; 2048² 24.7s; VRAM added 15.19G cold + 2.06G 2K activations
    → 20 booked. (iGPU parity for comparison: 67s warm @1024², 13 booked.)
  - xformers-vs-SDPA A/B (dev-turbo, same container, both fresh): 6.1s = 6.1s
    @1024², VRAM peaks 81.92 vs 81.76G (noise). On this SM120 card SDPA (cuDNN
    path) matches the stale fa2-2.8.3 build exactly — the flag costs nothing for
    the no-bias flux models; for qwen it is the only path that works at all.
    Both are flash-style low-memory, so no VRAM difference either. The flag
    only changes ComfyUI's global attention SELECT; the xformers package stays
    installed for custom nodes that import it directly.
  - backends order flipped to [cuda, vulkan] (2026-09-22): tier tries cuda
    first (never evicts an LLM for VRAM), refuses when headroom < 20G
    (chat-code-img: cuda0 reads 0G), falls back to vulkan. First-fit-wins,
    the klein convention.
  - The bench's own "suggested footprint 48-50 / host_ram 67" was REJECTED:
    baseline VRAM was 30.5G because the auto-scheduler had dev-turbo loading in
    the SAME container before the bench relabel (same-backend swap keeps the
    container + checkpoint cache), so its footprint double-counted dev-turbo and
    its host_ram is the cgroup-peak staging trap again (--reserve-vram 4 parks
    idle models in reclaimable host RAM). Per-run "added" deltas are the honest
    column; whole-machine host delta <= 1.7G → no cuda host_ram_gib booked
    (same convention as klein/ideogram/dev-turbo).

**Activation was live, no stackd rebuild:** AI-STACK is bind-mounted at
/run/aistack inside stackd, and `stackctl reload` re-reads config + .env.
Sequence: .env COMFYUI_CUDA_IMAGE → 0921; both image.yamls gained backends
[vulkan, cuda] + cuda footprint + CLI_ARGS --disable-xformers; reload (twice);
bench; final numbers; reload. A stackd image rebuild is still owed for the
BAKED default config/ copy (committed in the stackd repo; the running container
keeps the old baked config until the next rebuild — harmless, config.local
shadows it).
