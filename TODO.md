# stackd / toolbox — TODO

Working notes for the image-editing toolbox. Every line here was checked against the code
as of 2026-09-16; where a claim is unproven it says so rather than implying it is done.

Test convention (see README):

```bash
for t in tests/smoke*.py; do python3 "$t"; done   # stdlib only
pytest
```

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
| mode / kind | job row only; `engine.py` never reads it | **none** — all five modes run one graph |
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
- **The mode dropdown lied.** `darkroom` promised deterministic tone ops without the GPU
  while `engine.py` has zero references to `kind` — every mode runs the same GPU Klein
  inpaint, so choosing it to save GPU time would have billed the GPU anyway. Labels are now
  plain names plus `Darkroom (not implemented)`, and the panel says all modes run one
  repaint graph. If someone wires `kind` into the engine, the premise check fails and the
  labels get revisited deliberately.
- Blend blankness has a browser mutant (`--mutate blendblank`) faithful to the shipped
  defect — it reverts BOTH halves (old `select()` and the bare-value caller), since either
  half alone still labels correctly — and it goes red on exactly `no blank options`.

Counts after today: offline **295/295**, browser **62/62**.

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


