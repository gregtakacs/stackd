# stackd / toolbox — TODO

Working notes for the image-editing toolbox. Every line here was checked against the code
as of 2026-09-16; where a claim is unproven it says so rather than implying it is done.

Test convention (see README):

```bash
for t in tests/smoke*.py; do python3 "$t"; done   # stdlib only
pytest
```

---

## 0B. SAM3 click-to-select (smart select) — shipped + verified 2026-09-16

Click an object, its silhouette loads as an editable mask. Built on the standalone SAM3
point prompt (`forward_segment(point_inputs=…)`, no CLIP text encoder needed — only the
1.75 GB `sam3.1_multiplex_fp16.safetensors` checkpoint).

**Wiring (server → browser):**
- `graphs.sam3_segment_graph()` + `validate_sam3_segment_graph()` — a tiny standalone graph
  (`CheckpointLoader → SAM3_Detect(positive_coords/negative_coords as JSON) → MaskToImage →
  SaveImage`). The CLIP text node is added ONLY for text prompts; a pure click omits it.
- `engine.comfy_segment_click(source, points, negatives, …)` — uploads the source, runs the
  graph, returns the mask in the load-stroke contract (RGBA, alpha=coverage), plus
  `{size, coverage_paint, coverage_after, inverted}`. Coords are normalised 0..1 and
  multiplied to pixels against the size ComfyUI actually renders at.
- `api.Toolbox(click_segmenter=…)` seam + `POST /toolbox/mask/click`. Degrades honestly:
  503 `no_click_segmenter` / `no_image_engine`, 502 on engine failure, 400 on empty or
  runaway (>24) point lists. `serve.py` passes `click_segmenter=_tb_engine.comfy_segment_click`.
- `web/toolbox.js` — a "Smart select" tool. Plain click = one positive point (fresh
  selection); shift+click adds a positive; alt/ctrl+click subtracts. The WHOLE accumulated
  point list is re-sent every click (the node is stateless) and the result REPLACES the one
  `load` stroke, so an excluded region genuinely vanishes rather than surviving as a union.
  Point-list mutation lives in `runSmart` so a failed/empty click rolls back to exactly the
  mask still on canvas.

**Latent bug found while testing the feature (fixed):** `exportLayers` skipped every stroke
without a `pts` list, so a `load`/auto stroke was invisible to the per-object contract. A
smart-select ALONE happened to work (layers null → server falls back to `mask_png`), but
**smart-select + a brush stroke shipped `layers:[brush]`, and because the server prefers
`layers` over `mask_png`, the SAM3 selection silently vanished from the render** while still
looking selected on screen. Fix: `exportLayers` now lets `mode==='load'` through
(`masks.KIND_RULES['auto'] = threshold+feather+morph`, so it is honoured like a shape). The
browser suite proves the guard with a negative control (restore the old line → the auto-layer
assertion goes red, `last=[]`).

**Verified:** offline `tests/smoke_toolbox.py` 320/320. Browser suite 78/78. Live
GPU synthetic render: red/blue split with a green square, click inside → alpha inside 255,
over the blue half 0, coverage 10.6% ≈ the square's true area, `inverted:false`.

### 0B-follow-up: per-object editing, multiple objects, smart lasso, overlay opacity — 2026-09-16

Three gaps reported after first use of Smart select, all fixed (frontend-only):

- **Grow/shrink/feather an auto selection was impossible.** Root cause: `objBBox()` tested
  `!o.pts` *before* the `mode==='load'` case, and a load stroke has no `pts`, so every smart
  selection got a `null` bbox → `hitTest` skipped it → the Select-tool inspector (which does
  offer edge+feather for an `auto` object) could never open. Fixed the guard order and added
  `measureLoadBBox()` (a tight coverage box, scanned once from the mask PNG on a 96² downsample)
  so selection, the "…% of this object" readout and hit-testing all agree on where the object
  is. `translateSel`/`scaleSel` now no-op for a `load` stroke (it has no vertices — an auto mask
  is a segmentation of the photo's real pixels, not a movable vector shape).
- **Multiple objects.** `runSmart` kept one `smartLayer` and replaced it, so a second click
  wiped the first. It is now an object model: a plain click starts a NEW independent `load`
  stroke (its own points, its own layer, separately grow/feather/delete); shift/alt refine the
  current one.
- **Smart lasso (Auto/Manual).** A Smart-tool drag traces a live loop; on release, *Auto* seeds
  SAM3 with a point-in-polygon scatter of interior points and re-uses the verified `/mask/click`
  point seam (SAM3 snaps to the true boundary), *Manual* commits the exact traced pixels as an
  add/subtract shape stroke. A `Loop: Auto/Manual` toolbar button toggles it (enabled only while
  the Smart tool is up). NOTE: the native `SAM3_Detect.bboxes` box-prompt path was investigated
  and is **broken on the fp16 multiplex checkpoint** — every wiring variant (literal JSON string,
  `CreateBoundingBoxes` with/without `editor_state`) returned an empty mask, so the Auto loop
  deliberately uses the interior-point path, which is proven on GPU.
- **Overlay opacity was inert once the server preview landed.** `compose()` drew the server's
  `overlay_png` (tint baked at a constant `alpha=0.5`) at full alpha and returned early, so the
  `overlay` slider only ever moved the pre-preview local wash. Fixed honestly: the preview
  request now carries `overlay_alpha` (from the slider), `h_mask_preview` bakes the tint at that
  alpha, and `paramsSig` folds it in so the cheap no-GPU overlay re-fires on a slider drag. The
  knob is cosmetic (never touches `canon`, so the render mask is unchanged).

**Verified:** browser 78/78, incl. plain-click→new-object vs shift-accumulate (click-point
sequence `[1,2,1]`), an Auto loop firing `/mask/click` with interior seeds (16), a Manual loop
firing NO SAM3 call and leaving a `shape` layer, and deselect→re-tap reopening an edge+feather
inspector. The deselect assertion goes RED under a `--mutate autobbox` negative control (guard
order restored), so it genuinely tests the fix. Offline 320/320 (added overlay-alpha-is-honoured
+ junk-alpha-falls-back). Live GPU: an Auto loop of interior points on the synthetic red/blue +
green square → green alpha 255, a yellow distractor 0, blue background 0, `inverted:false`.

**Still outstanding (not yet done, honest list):**

- SAM3 **cold start**: first `SAM3_Detect` call loads the checkpoint into VRAM (~10–20 s on
  ROCm); subsequent in-process calls are fast. There is NO `prewarm_segment` hook yet (only
  `_prewarm_edit` for the inpaint scaffold). The 30 s `req()` fetch timeout currently covers
  the worst case, so a first click shows "Selecting…" for that long. Recommend a background
  prewarm on tool-page load.
- The container is **hot-patched**, not rebuilt (see §1). Rebuild before trusting a deploy.
- A hand validation on a real photo (the crosswalk shot): click the person → silhouette loads
  → Replace → render. Not yet done by a human.
- `/toolbox/launch` is still 403 on the router (no oauth chain wired).
- The compositing pass (`blend_mode`/`opacity`/`color_match`/`preserve_detail`) is still dead
  (greyed knobs, §1B). The full render path has no resolution cap (156 s at 2048×1584);
  the segmentation path caps at 1024.

### 0B-follow-up-2 — smart-select masks were unselectable chrome + per-object edge was a no-op (this pass)

Two regressions survived the first follow-up and were reported against a real photo: (1) a
smart selection drew **stray squares outside the object**, and (2) grow/shrink/feather "re-render
but nothing changes" on smart-select masks. Root-caused empirically, not assumed:

- **Server per-object morph is fine.** A direct `masks.normalize_layers` probe on an `auto`
  silhouette moved coverage 5,789 → 16,829 px at `edge=+24` and applied feather; the new offline
  route checks (`smoke_toolbox`: an AUTO layer's coverage GROWS/SHRINKS with a signed edge and
  reports `edge_applied`) are green. So the frozen mask was NOT the server.
- **Bug 1 — the squares were the selection chrome.** `requestSmart` auto-`selectObj()`s every
  smart layer, so `compose → drawSelection` painted a dashed bounding rectangle **plus a solid
  blue corner "scale" handle** over each auto object — and `objBBox` falls back to the full frame
  when `measureLoadBBox` can't read the PNG, so the box covered the whole photo. An auto mask is a
  fixed silhouette (`translateSel`/`scaleSel` are deliberately no-ops on it), so that affordance
  advertised nothing and read as a stray square. Fix: `drawSelection` now early-returns for
  `mode==='load'` (the red wash already shows the selection; the inspector still opens). The
  misleading "drag to move, corner to scale" status for an auto object was replaced with a
  grow/shrink/feather hint.
- **Bug 2 — the TOOLBAR edge/feather sliders fed the path the layered server ignores.** With a
  smart layer present, `h_mask_preview`/job-create take the `normalize_layers` branch, which
  applies each layer's OWN `grow/shrink/feather` and never the global `mask_expand/feather`. The
  toolbar sliders only set the globals, so dragging them changed `paramsSig` (preview re-fired)
  but the server returned an identical overlay — literally "re-render, nothing changes." Fix: when
  an **auto** object is selected, the toolbar `edge`/`feather` sliders now write to THAT object
  (`o.edge/grow/shrink` or `o.feather`) and re-preview; `selectObj` mirrors the object's current
  values back into the sliders. Unselected / shape / brush keep the old "defaults for the next
  object" meaning. I did NOT bake the morph into the on-canvas wash: that canvas feeds
  `exportMask`/`exportLayers`, and the server re-applies the same edge, so baking it would grow the
  object twice. The WYSIWYG grow/feather feedback is the server overlay, exactly as for shapes.

**Verified:** browser **80/80** (added: toolbar edge/feather reach the selected auto layer as
`grow=30/feather=20`, and edge+feather controls exist), and two negative controls each turn
exactly the right assertion RED — `--mutate autoslider` (routing removed → auto layer hits the wire
at grow=0/feather=8) and `--mutate autobbox` (guard order restored → empty-area tap fails to
deselect). Offline **323/323** (the three new AUTO per-object coverage-morph route checks).
Container `stackd` re-synced: `web/toolbox.js`, `masks.py`, `api.py` byte-identical host↔container
(md5). `toolbox.js` is served per-request, so the fix is live without a restart.

### 0B-follow-up-3 — smart-select geometry: contiguity, radial growth, outward feather, active object, live sliders (this pass)

A real-photo pass surfaced five distinct defects, each root-caused to code (verified against the
container's PIL 12.3.0, which ships **no numpy/scipy** — every kernel below is pure PIL+python):

1. **Stray speckles → squares.** SAM3's raw output scatters a few-pixel islands around the object;
   a grow then inflates each into a visible block. Fix: `masks.keep_significant_components` (an
   8-connected flood fill) drops islands below a *relative* floor (a fraction of the dominant blob),
   so a deliberately shift-clicked hat/backpack survives while decoder noise dies. Wired at the
   source in `engine.click_segmenter` so the on-screen stroke and the render mask are both clean.
2. **Growth was square, not radial.** `masks._morph` composes a 3×3 square filter — measured: a lone
   pixel dilated by r=6 → a 13×13 = 169 px block, not a disc's ~113. A Gaussian could not fix it
   (blur-then-threshold *erases* thin masks — measured a lone pixel → 0 px). Fix: `_morph_disk`, an
   exact binary disc via a separable squared-Euclidean distance transform (Felzenszwalb), applied to
   the `auto` kind only in `_layer_coverage`. Shapes/brush/legacy keep the tested square `_morph`.
3. **Feather shrank inward.** The symmetric blur pulled the ≥128 crossing inside the edge, so the
   binarised mask was smaller than approved. Fix: `_feather_out` (grow-by-radius then blur then union)
   for `auto`, so the ≥128 footprint can only match or exceed the hard selection.
4. **No active-object cue.** `drawSelection` early-returns for auto (Bug 1 above), so nothing marked
   the selected object. Fix: `drawActiveAutoOutline` strokes the object's *actual* edge-morphed
   silhouette in cyan (never a rectangle), and it tracks the edge slider.
5. **No live slider feedback.** `schedulePreview` nulled the overlay each input, so the canvas fell
   back to the raw blit for the whole 420 ms. Fix: `compose()` builds a **display-only** wash that
   mirrors the server's disc-grow/shrink + outward feather for the SELECTED auto object, so the slider
   is live; `maskC` (export) stays raw → the server applies the geometry exactly once (no double-grow).

**Audit caught four real bugs before any green run:** background pixels retained `lab=0` and would
have been marked selected (whole-frame fill — gated on `flat`); a whole-line `INF` made `INF−INF=NaN`
and annihilated a fully-selected erode (seed-aware `_dt1d`); `_layer_coverage` never bound `kind`
(NameError on every auto/edge layer); and the new test's own `_selected` shadowed a module helper.

**Validation status:** `py_compile` clean for `masks.py`/`engine.py`/`api.py`/`smoke_toolbox.py`.
The offline suite ran once and its *only* failure was the `_selected` shadow (since fixed); a green
re-run of `python3 tests/smoke_toolbox.py` (offline, incl. the new K2 disc/denoise/feather checks) and
the browser suite (`/tmp/tbtest`) were **blocked by a harness command outage this session** and are the
remaining gate. The container has **NOT** been re-synced — it still runs the previously-validated
build, so no unvalidated kernel is live. Do `docker cp` + restart only after both suites are green.


**Still open (honest):** the human crosswalk-photo validation of the *whole* flow (click person →
Select tool → grow/shrink/feather visibly; second independent person; Auto/Manual loop; overlay
slider live) — the automated suite proves the wire contract and the chrome removal, but a person
should confirm the on-screen feel. SAM3 cold start, `/toolbox/launch` 403, dead compositing knobs,
and the uncapped full-render path are unchanged from the list below.

---

## 1. Do first — the two that can silently lose work

### Commit the toolbox
Nothing in the toolbox is committed. `git status` shows `?? stackd/toolbox/` (the whole
module, including `masks.py`, `api.py`, `engine.py`, `jobs.py`, `web/toolbox.js`,
`web/toolbox.css`) plus modified `stackd/serve.py`, `stackd/cli.py`, `pyproject.toml`,
`deploy/.env.example`, `deploy/docker-compose.yml`, and new `tests/smoke_toolbox.py`,
`tests/verify_all.py`, `tests/check_serve_live.py`.

This is a full feature sitting in the working tree with no history. Any `git clean -fd`
or branch switch with discard destroys it.

### Rebuild the running image
`stackd` is running a **hot-patched** container: the current `masks.py`, `api.py`,
`toolbox.js`, `toolbox.css` were `docker cp`'d in and restarted, not built. The next
`docker compose build stackd` reverts the container to the image's older code and the
editor will look broken while the repo looks fine.

    docker compose build stackd && docker compose up -d stackd

Confirm the rebuild actually took (shipped code has the three-flag rule):

    docker exec stackd python3 -c "import sys; sys.path.insert(0,'/app'); from stackd.toolbox import masks as M; print(M.KIND_RULES)"
    # expect: {'brush': (False, False, False), 'shape': (True, True, True), 'auto': (True, True, True)}

---

## 1B. What the controls actually do (verified 2026-09-16, afternoon)

**M1 is confirmed working end-to-end.** The "not verified by a human" item under Product
gaps is closed. The live queue has a finished job — id `de28210295c32434`,
greg@takacs.net, kind `erase`, 1312x1312, `state=done`, engine_base
`http://comfyui-rocm:8188`, artifact 3,231,254 bytes which decodes as a valid 1312x1312
RGB PNG (pulled to `/tmp/last_edit.png`). Create -> poll -> ComfyUI -> artifact -> OWU
save all worked, ~377 s wall (model swap + sampling). Still wanted for the record: a
screenshot, and the source-vs-result diff that would show the erase removed something.

### The honest control inventory

`engine._patch_graph` is the only place a spec value reaches ComfyUI, and it wires exactly
**prompt, width, height, seed**. Everything else in the panel is either mask geometry
(applied server-side by `masks.py`) or nothing at all:

| Control | Where it goes | Effect on the image |
|---|---|---|
| prompt | `MASK_POSITIVE_NODE` | yes |
| seed | `MASK_SEED_NODE` | yes |
| width / height | `MASK_WIDTH_NODE` / `MASK_HEIGHT_NODE` | yes |
| edge (per object), feather | `masks.normalize_layers` | yes (mask geometry) |
| mode (Edit / Replace) | `engine._apply_mode` rewires the KSampler conditioning | **yes** — Edit blends into the scene; Replace reimagines the mask |
| edit strength | nowhere | none |
| blend opacity | nowhere | none |
| blend mode | nowhere | none |
| colour match | nowhere | none |
| keep original detail | nowhere | none |
| variants | nowhere | none — `jobs.py` stores one `artifact_b64` |

Fixed today:

- **Blank blend options.** `select()` read `opts[i][1]` unconditionally while the blend
  caller passed bare values, so every option's `textContent` was `undefined`: a dropdown
  of eight unlabellable rows. `select()` now falls back to the value, and blend options
  carry explicit labels (`Soft light`) with values in the spelling ComfyUI's `ImageBlend`
  enum uses (`soft_light`) so the day it is wired is a passthrough.
- **Inert knobs are disabled with a stated reason** (`DEAD_KNOBS` in `toolbox.js`), greyed
  like the inspector's brush feather, plus a panel line saying only prompt, seed and the
  mask geometry affect the image today. Two offline invariants enforce it both ways: an
  enabled control must be read by the server, and a control the server reads must never be
  disabled. Mutants prove both bite (empty `DEAD_KNOBS` -> 8 failures; disabling `prompt`
  -> 1 failure). Liveness is *measured* by grepping `.get("k"` / `["k"]` in
  `engine.py`+`api.py`, never asserted — the bare-name form passes on comments.
- **The mode dropdown was theatre — now a real control (2026-09-16, 2nd pass).** `darkroom`
  once promised deterministic tone ops without the GPU while `engine.py` had zero references
  to `kind`; it was first honestified (labels stripped, "all modes run one graph" disclosed).
  A user then hit the real gap: the single repaint graph can recolor but cannot *remove* an
  object, because the KSampler's positive conditioning (node 23) references a pass built from
  the whole original image — it conditions the model on the very thing being changed away from.
  `engine._apply_mode` now reads `kind` and offers the two real paths, mirroring imagegen's
  own `_submit_edit` `preserve_scene_context` branch (the replacement the OWUI edit_image tool
  performs), bound to the painted mask: **Edit** keeps the scene-referenced pass (node 23) so
  surface edits blend in; **Replace** drops the full-scene pass (nodes 21+23) and conditions on
  the masked region alone (node 22), so the prompt can do a genuine identity-level swap/removal.
  The five theatre modes are gone; Edit and Replace are the only options. The offline suite
  pins both halves: engine reads `kind`, and `replace` rewires the graph to the region latent.
- Blend blankness has a browser mutant (`--mutate blendblank`) faithful to the shipped
  defect — it reverts BOTH halves (old `select()` and the bare-value caller), since either
  half alone still labels correctly — and it goes red on exactly `no blank options`.
- **The painted mask reached the graph with inverted polarity (2026-09-16, 3rd pass).** The
  server marks the selection 255 = edit-here (what the preview + coverage show), but
  `VAEEncodeForInpaint` / `ImageCompositeMasked` edit where the mask is **0** (the CLIPSeg
  convention the canonical path relies on). So painting a person replaced the *background*.
  Invisible in Edit mode, glaring in Replace. `engine._graph_mask` flips the alpha at the
  graph boundary (not in `normalize`, so preview/coverage are unaffected and the scaffold is
  untouched); the invert toggle composes on top. Verified on a live split-source render.


Counts after the 3rd pass: offline **299/299**. The `/tmp/tbtest` browser suite still
asserts the OLD "same repaint graph" disclosure text, so re-point that one assertion at the
new Edit/Replace note before running it — the shipped code itself is unchanged in that respect.

---

## 2. Keyboard shortcuts — the UI currently lies about them

Six tooltips advertise shortcuts that **do not exist**. There is no `keydown` handler
anywhere in `web/toolbox.js`; the only `ctrlKey` reference is line ~1217, which is
trackpad pinch-to-zoom on the wheel handler and is unrelated.

| Advertised in | Line | Text | Works? |
|---|---|---|---|
| `tbtn('brush', ...)` | 742 | `Paint the area to change (B)` | no |
| `tbtn('eraser', ...)` | 743 | `Erase from the mask (E)` | no |
| `tbtn('lasso', ...)` | 744 | `Freehand: drag a loop around the area (L)` | no |
| `tbtn('polygon', ...)` | 745 | `Click points around the area, then Close (P)` | no |
| `btn('Undo', ...)` | 754 | `Ctrl/Cmd-Z` | no, mouse only |
| `btn('Redo', ...)` | 755 | `Ctrl/Cmd-Shift-Z` | no, mouse only |

Either wire them or delete the promises. Lying in a tooltip is worse than omitting the
feature, because the user concludes they are doing it wrong rather than that it is absent.

Suggested keys, matching the existing labels:

- `B` brush, `E` eraser, `L` lasso, `P` polygon, `V` select (the Select button at line 741
  advertises nothing and should get a key: it is the entry point to all per-object adjustment)
- `Ctrl/Cmd-Z` and `Ctrl/Cmd-Shift-Z` to `undo()` (line 526) and `redoStep()` (line 534)
- `Escape` to close an in-progress shape, then deselect
- `Delete`/`Backspace` to `deleteSel()` on the selected object

### Constraints that will bite, in this order

1. **Ignore keys while typing.** The prompt is a real textarea and there are number
   inputs. Without an early bail when `e.target` is INPUT/TEXTAREA/contentEditable, typing
   "brushable" in the prompt switches tools mid-word. This is the bug that makes most first
   attempts at shortcuts feel cursed.
2. **Do not fight the browser.** `Ctrl-Z` inside a focused input must stay native undo, so
   scope the global handler to fire only when focus is not in an editable element.
3. **`Escape` and the active shape.** `active` holds an uncommitted shape; Escape should
   discard it and clear the selection outline, not call `clearMask()` — that is destructive
   and must stay behind its button.
4. **Do not duplicate the close-shape logic.** `Close shape` (line 748) checks
   `active.pts.length > 2` before committing; a key handler must reuse that path or the two
   drift apart.

Done means: keys work, **and** a test drives them. Add to the browser suite (section 3):
a `keydown` of `b` must change the active tool, and `Ctrl-Z` must remove the last committed
stroke. Removing the handler must turn that test red — without it this is the same class of
unenforced promise as the tooltips being wrong today.

---

## 3. Move the browser suite into the repo

`/tmp/tbtest/` holds the only tests that exercise the real editor in a real browser:
`run_tbtest.py`, `driver.js`, `steps_a.js`, `steps_b.js`. It currently drives 52 assertions
that the offline suite cannot make — what actually goes over the wire, whether the Select
tool can move an object, whether a brush layer is sent unfeathered.

Being in `/tmp`, it is one reboot away from gone, and it is not run by CI or by
`for t in tests/smoke*.py`.

To do:

- Relocate to `tests/browser/` (or similar) and wire into `verify_all.py` / `make test`.
- **Skip cleanly when Chrome is absent.** These tests need a browser; without a guard the
  stdlib-only suite gains a hard dependency and fails on hosts without Chrome. The README
  advertises "stdlib only", so the skip must be silent-and-reported, not an error.
- Keep the truncation guard: `run_tbtest.py` asserts `EXPECT = 62`, so a suite that dies
  part-way reports TRUNCATED instead of quietly passing on fewer assertions. Any new
  assertion must bump that number, or the guard silently loses meaning.
- `after.png`, `after2.png`, `mask.png`, `out.txt`, `o2.txt`, `dbg.py`, `gen.py`,
  `__pycache__` are scratch artifacts from development, not tests. Do not commit them.

## 4. Known-unproven coverage — do not quietly call this finished

- **`objSig()` has no proof it is load-bearing.** The `nosig` mutant (removing per-object
  geometry from the preview cache key) passes every assertion. It cannot fail today because
  `schedulePreview()` nulls `preview` before scheduling, so a stale-overlay bug is
  unreachable by the current call graph. It is defensive completeness, and the code comment
  says so. If someone later adds another `runPreview()` caller, this is exactly the test that
  would start mattering — until then, nobody should claim it is covered.
- **Touch behaviour is unverified.** Everything below was tested with a mouse.

## 5. Device verification (needs a human)

The Select tool and the new bipolar edge slider were built and tested on desktop.

- Phone: finger-tap to select, pinch-then-draw, polygon close, and whether one thumb can
  set a negative-vs-positive edge value without overshooting past the 0 detent (a bipolar
  slider is easier to overshoot than a unidirectional one — this is the main thing to judge).
- Desktop: Select tap, Fit without scrollbars, zoom, scribble self-overlap.
- **Hit tolerance may need a touch-specific value.** `hitTest()` uses `var tol = 8 / scale()`
  (line 194), i.e. 8 CSS px. That is a guess. Finger targets are usually specified nearer
  40–44 px; if taps miss on a phone, raise it for coarse pointers
  (`matchMedia('(pointer: coarse)')`) rather than globally, so mouse users keep precision.

## 6. Product gaps

- **`/toolbox/embed` conflates "no image_id" with "fetch failed".** Both surface the same
  way, so a user cannot tell "OpenWebUI did not hand me a photo" from "the photo exists but
  I could not load it" — and only one of those is their fault. Resolve is at
  `api.py` around line 444/498, where `image_id` is read from the query; the two states need
  distinct messaging.
- **M1 IS verified** — see §1B: job `de28210295c32434` rendered on comfyui-rocm and the
  artifact decodes. Wanted: the screenshot, and a source-vs-result diff proving the erase
  removed what was painted (the row records 0.89% coverage, so the change is small).
- **M2: OpenWebUI chat-bubble integration.** Not started — and confirmed absent:
  `openwebui-tools/` ships home-assistant / admin-tools / youtube-transcript and nothing
  toolbox-related, so today the standalone editor is the only mount. `engine.comfy_render`
  does save the artifact into the user's OWU library, which is what a chat mount reads.

### How to get in today

`/toolbox/launch` is a 403 in this deployment: the only stackd router is `llama.<domain>`
on `chain-no-oauth@file` (AI-STACK/docker-compose.yml ~143-152), so no trusted identity
header reaches `_launch_email()` and it refuses to mint. There is no `lab.<domain>` router
at all. The working entry is the CLI, verified today (mints, and
`GET /toolbox/embed?token=` returns 200 / 81 KB):

    docker exec stackd stackctl toolbox-token greg@takacs.net \
        --image-id /api/v1/files/<id>/content \
        --base-url https://llama.takacsfamily.com

The real fix is a launch router behind the oauth chain — as its own router, not on the
`/v1` front, which is unauthenticated-by-design.

### Still open on this thread

- Wire the compositing pass. `blend_mode` / `opacity` / `color_match` / `preserve_detail`
  all describe blending the regenerated region back over the source, which the graph never
  does. Either add it (ImageBlend-style nodes, or compose server-side against
  `_prepare_source`) or delete the controls rather than leaving them greyed forever.
- `variants` needs real support: one `artifact_b64` per row means N variants is either N
  rows or an artifact list.
- Deterministic darkroom ops (levels/curves inside the mask) are the genuinely missing
  capability, and the one a label already promised once.

## 7. Worth doing when the above is clear

- Selection outline + inspector are desktop-first; the corner scale handle is small on touch.
- No visual affordance distinguishes the global sliders ("defaults for the NEXT object")
  from the per-object inspector, beyond hint text. Users will keep expecting the globals to
  re-tune an already-placed object.
- `objSig()` and `previewSig()` are string-join signatures with no length bound; harmless at
  current object counts, unbounded if someone scripts 1000 strokes.
- README's Tests section says "~280 checks, stdlib only". That predates the toolbox suite,
  which contributes 295 on its own (`tests/smoke_toolbox.py`). Recount once the browser
  suite moves in and put the real number there.


