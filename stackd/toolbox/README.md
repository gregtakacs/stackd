# stackd/toolbox — the Comfy Toolbox (mask editor + edit engine)

> **Status: M0 PASSED** (human-verified on PC + phone); **M1 code LANDED** (worker + real
> ComfyUI render seam + daemon mount + `REDEEM_ON_CREATE`, all green in the offline suite at
> 202/202). A sandboxed `srcdoc` opaque-origin frame paints, reports its height, and reaches
> the toolbox API cross-origin — in the sandboxed panel exactly as in the unsandboxed
> control. The in-chat rich-UI embed mount is viable; M1 builds against it. **M1 is NOT yet
> human-verified end-to-end** (needs a resident image engine on a live GPU host) — the
> `MILESTONE` string `/toolbox/health` reports stays `M0-passed` until that run happens, per
> the rule that a milestone bumps on human verification, not on code merely merging. See
> *Roadmap* for the result and the corrected M1 scope.

Round-1 milestone **M0**: prove a mask can be *authored at all*. Mask authoring turned out

to be the gate for the whole feature — an MCP tool that takes a mask but no way to draw
one is unusable no matter how good the graph is. So the editor comes first and the engine
is built for it, not the other way round.

Everything here is one contract (`EditSpec`) and one job API, mounted two ways: the
in-chat Open WebUI rich-UI embed and a standalone `lab.<domain>` page. Both run the
identical bundle, which is why `web/toolbox.js` uses no cookies, no storage and no
relative URLs — the embed mount is an opaque-origin sandboxed `srcdoc` iframe.

## Run the M0 spike

```bash
cd ~/docker/Projects/LLM-Tools/stackd
python3 -m stackd.toolbox.spike --port 8191                 # synthetic photo
python3 -m stackd.toolbox.spike --port 8191 --image ~/some/photo.jpg
```

It prints the PC URL and the LAN URL to open on a phone. The page shows two panels running
the byte-identical editor, differing in exactly one variable:

| panel | how mounted | proves |
|---|---|---|
| **A** | same-origin `<iframe src>`, no sandbox | the editor itself works (the control) |
| **B** | `<iframe srcdoc>` + `sandbox="allow-scripts"` | Open WebUI's real embed conditions |

**Four pass criteria** — read them off the header cells, not by eye. Every cell now exists
per panel (A gets `cnta/canvasa/ha/scrolla/exporta/servera`, B gets the same without the
`a`), because a harness that instruments only the panel under test tells you nothing:

1. **painting reaches the frame** — drag in panel B; "events seen" must rise and "canvas
   got events" must say YES. If it stays at 0, a scroll container is eating the drag and
   the in-chat mount is dead on phones.
2. **the mask arrives server-side with real coverage** — press **Preview mask** in each
   panel; "server round-trip" must report a non-zero `cov=` and an overlay must appear.
3. **the frame reports its own height** — "height reported" > 0. `iframe:height` is the
   only postMessage Open WebUI 0.11.3 honours (grepped out of its bundle).
4. **no internal scrollbar** — both "internal scrollbar" cells must read **no (fits)**.
   `YES — Npx hidden` means the editor under-reports its height and the user is left
   fighting a scrollbar inside the editor, which on a phone is unusable. If A fits and B
   does not, the *sandbox* is clamping the height; if neither fits, the bug is
   `toolbox.js:reportHeight`, not Open WebUI. The probe reports content height and viewport
   height separately precisely so these two causes stay distinguishable — one number alone
   cannot tell them apart, and they demand opposite fixes.

Interpreting it: **A green + B red** ⇒ the blocker is the sandbox/CORS, so the in-chat
mount is abandoned and the standalone page becomes the primary mount. **Both red** ⇒ an
editor bug, and nothing was learned about Open WebUI. The probe's export self-test
(measured alpha count vs analytic `πr²`) separates "canvas broken" from "network
broken", and the "server round-trip" cell separates a dead server from a blocked frame.

Two things that look like failures and are not: panel A's synthetic-stroke button reports
that it cannot script panel B (correct — an opaque-origin `srcdoc` frame has a null
`contentDocument`, and that fact is the experiment), and a `?probe=1` on a mount whose
daemon has `spike_enabled=False` silently serves an uninstrumented editor, so all cells
stay at `?` — check `/toolbox/health`'s `spike` flag before believing a red run.

**Operational gotchas**, each of which cost real time during the first live run:
`--print-urls` **prints and exits** (`spike.py:182`); it is discovery, not an additive flag.
The token HMAC key is **random per run** unless you pass `--secret`, so a token minted
outside that process fails its signature check and `/toolbox/embed` returns an error page —
which is exactly how a healthy server can look like it serves nothing. And the probe only
attaches when the *serving* process has `spike_enabled=True` **and** the URL carries
`?probe=1`; the harness appends it for you, but a hand-typed embed URL will not have it.

### The harness is itself a program, and it had three bugs

Every cell on the spike page arrives through `web.harness_document()`, which injects the
embed URL by replacing the literal token `__TB_EMBED_URL__` in `harness.js` with a
JSON-quoted string. That substitution is the only seam between server and rig, and it broke
in ways that all looked identical from the outside — a page full of `?` cells, which a human
reads as "the iframe mount is failing" rather than "the page never loaded anything":

1. A rewrite of `harness.js` read the embed URL from the **query string** instead of the
   placeholder. `embed` was `null`, neither `<iframe>` was ever assigned a `src`, and both
   panels sat at `about:blank` while the page displayed its own placeholders as results.
2. The placeholder was then restored, but **quoted** — `'__TB_EMBED_URL__'` — while the
   injector uses `_json_for_script()` (`json.dumps`), which *already* supplies the quotes.
   The result was a string beginning with a literal `"` character, so both iframes requested
   a path starting with a quote and 404'd. **This is what produced "totally blank frames".**
   Rule: the template never adds quotes, exactly as in `embed_document`'s config bootstrap.
3. `harness.html` lost the `<script>` wrapper around `__TB_SCRIPT__` in an unrelated edit, so
   ~7 KB of controller code was spliced into `<body>` as **text**. Nothing executed, and every
   substitution test still passed, because substitution had happened — just not inside an
   executable element. Wrapping is now done by `harness_document()` in Python, which the suite
   actually exercises; the template carries a bare placeholder and a warning not to tag it.
4. `str.replace` substitutes **every** occurrence, comments included, so the sentinel may be
   spelled in exactly one place and prose must never name it, nor write script-tag literals:
   doing so both defeats the sentinel and fools the greps that count tag balance.
5. The scroll/height readings were taken against `contentDocument` even for a frame that had
   never fired `load`, so an absent panel could be stamped with the *confident* reading
   "no (fits)".

The structural fix is the deadman: `CELLS` enumerates every readout, `put()` records what has
actually been written, and after 5 s anything still unfilled is stamped **NO DATA** with a
verdict naming the dead stage ("NEITHER PANEL EVER LOADED" vs "loaded but the probe stayed
silent"). A blank cell is no longer a representable state. An instrument that can fail
silently is worse than no instrument, because it turns a broken rig into a negative
experimental result — and on this project that result decides whether the in-chat mount gets
abandoned.

**Caveat on the criterion-3 evidence.** An earlier run showed panel B growing taller, which
was read as proof that a sandboxed opaque-origin frame can `postMessage` its height to the
parent. The rig that produced that reading attributed `iframe:height` messages by arrival
order, not by `e.source`, so panel A's report could have grown panel B's frame. Criterion 3
counts as **unproven until this version re-reports it**, where per-frame attribution is exact.


The spike shares `Toolbox.dispatch()` with what `stackd serve` will mount in M1, so a
green run is evidence about the real system rather than about a throwaway imitation. It is
**not** production surface: `/toolbox/spike` and the probe exist only when
`spike_enabled=True`.

## Verify

```bash
python3 tests/smoke_toolbox.py       # 507 checks: tokens, mask semantics, routes, contract,
                                     # harness wiring, and the shipped JS/CSS as the build
                                     # serves them (attribute whitelist, dead-knob ledger,
                                     # brace/paren balance, DOM-id cross-checks)

python3 tests/tbtest/run_tbtest.py   # 93 browser assertions in real headless Chrome against
                                     # the SHIPPED bytes through the SHIPPED assembler: the
                                     # region model (merge/split/inherit), pixel-exact
                                     # hit-testing, live morphological display for every
                                     # object (not just the selected one), layer export per
                                     # connected region, mobile gesture lockup, scrollbars.

# Every browser assertion must be able to go RED on the defect it guards. Each --mutate
# name rebuilds the page with one faithful defect reintroduced; the run must FAIL:
python3 tests/tbtest/run_tbtest.py --mutate rawwash   # deselected objects snap back to raw
                                                      # geometry (the reported bug #1)
#   ... widetol (bbox-shadowed hit-test), allmerge (one blob = one object), autoslider
#   (toolbar knobs ignored), brushfeather (KIND_RULES bypass), kindstr, twoslider, noderive,
#   selguard, sticky, nosig, softpunch, ctlorder, blendblank, legacy — see run_tbtest.py
```

Runs on a stdlib+pillow host — no ComfyUI, no Open WebUI, no browser. Covers polarity
(alpha vs luminance vs an opaque-alpha screenshot export), resample-to-working-dims,
grow/shrink/feather, the empty-vs-small distinction, route auth (no token / forged /
wrong scope / cross-job token), honest 503s, CORS, and the JS↔server field contract.

## Files

| file | what it is |
|---|---|
| `tokens.py` | single-use HMAC launch tokens + reusable job tokens, derived from stackd's shared secret with a domain separator |
| `masks.py` | what a painted mask MEANS: polarity, resample to the graph's working dims, grow/shrink/feather, coverage verdicts, dry-run overlay. **The authoritative mask module.** |
| `api.py` | `/toolbox/*` as a `dispatch()` any host can mount (stdlib only) |
| `web.py` | server-renders the self-contained embed document; inlines CSS/JS because a srcdoc frame has no base URL to resolve against |
| `spike.py` | the standalone M0 server above |
| `web/toolbox.js` | the mount-agnostic editor. Objects are CONNECTED PAINT REGIONS (two-pass union-find over the composited mask), each with live per-object edge/feather; strokes are kept only as replay history for undo and eraser-ordering, never as the unit of selection |
| `web/probe.js` | M0 instrumentation — injected into the spike only, never a real mount |
| `web/harness.html`, `web/harness.js` | the two-panel feasibility page |


## Deliberate decisions worth knowing before changing something

- **The browser is a crude painter; the server owns mask semantics.**
  `masks.normalize()` resamples to the dims the job actually submits, because the graph
  conditions on `ImageScale` at the working size and `VAEEncodeForInpaint` takes the mask
  at exactly those dims. Trusting the client's pixel grid is how you edit the wrong third
  of a photo. A slider change therefore never needs a re-paint.
- **Two coverage thresholds, not one.** A floor copied from the CLIPSeg path
  (`imagegen/comfyui_client.py`, 0.3%) rejected the flagship use case outright: one pimple
  on a 1024x768 photo is ~0.18%. CLIPSeg can afford that bar only because GrowMask's
  mandatory margin inflates a real detection; a hand-painted spot stays small. Hence
  `USER_MASK_EMPTY_FLOOR` (refuses literal nothing) and `COVERAGE_FLOOR` (an informational
  "this is small" note, never an error).
- **Judge the paint, not the blur.** A Gaussian feather pushes energy outward, so the
  above-threshold pixel count *rises* with feather (measured 0.0023 -> 0.0033). Empty/tiny
  verdicts use `coverage_paint`, measured before the blur; `coverage_after` is the real
  blend extent and is what the status line displays.
- **Invert is a parameter, not pixels.** The graph already wires an InvertMask node
  (`workflows.MASK_INVERT_NODE`); baking an inversion into the alpha would make the
  feather/expand maths disagree with the preview the user approved.
- **No caller-supplied image or mask URL.** The mask arrives as an opaque base64 payload
  and the photo via an opaque id we minted — the principle documented all over
  `imagegen/tools.py`: a caller-supplied image reference is a footgun that has already
  fired once in this codebase.
- **Token scoping is asymmetric on purpose.** Reads verify `single_use=False`; the write
  (`/toolbox/jobs`) redeems single-use once creation costs real GPU time. In M0 creation
  is a stub, so `api.REDEEM_ON_CREATE = False` — **M1 must flip it to True**, and
  `/toolbox/health` reports the flag so it cannot quietly stay False past the milestone.
  Replay protection is in-process, so a daemon restart frees an un-redeemed launch token
  until its (minutes-long) `exp`; the Store-backed queue in M1 is where cross-restart
  single-use becomes worth the schema.
- **JSON travels as `text/plain`.** From an opaque-origin frame that keeps every call a
  *simple* CORS request needing no preflight round-trip. This is a latency and failure-mode
  choice, not a workaround for a missing feature: `OPTIONS` IS answered for every path with
  the full CORS set (`api.py:_route`), verified live. What is *not* verified is a
  preflighted JSON-content-type POST from a real sandboxed frame, so until that is exercised
  in M1 the editor avoids it. If you ever see a toolbox call fail with a CORS error on a
  `content-type: application/json` request, look here first — the failure looks exactly like
  a sandbox problem and is not one.
- **`_read_body()` exactly once per request.** `BaseHTTPRequestHandler`'s `rfile` is
  consumed by the first read, so a handler that re-reads its body works against a naive
  test fake and breaks against the real server. The fake now consumes on first read so
  that bug class fails in the suite instead.
- **Poll, don't hold.** The editor polls `/toolbox/jobs/poll` rather than holding a
  10-minute request open, so a session survives a daemon restart or a proxy idle timeout.
- **Reserve the scrollbar gutter; do not let the frame scroll.** A classic (non-overlay)
  vertical scrollbar takes ~15px out of the *viewport width*. The canvas and every control are
  laid out at `100%`, so the instant a vertical bar appears the page is 15px too wide, gains a
  horizontal bar, which steals back the height the host just fitted — the two then chase each
  other and the user inside a chat bubble is left fighting a nested scroll container to draw.
  The editor document therefore sets `scrollbar-gutter: stable` (width is constant whether or
  not the bar is showing, so the reported height is measured at the width it will render at)
  and `overflow-x: clip` (content may not scroll sideways at all; `clip` not `hidden`, because
  `hidden` mints a scroll box that would itself reserve a gutter). The spike's outer page needs
  the identical treatment one level up — two `1 1 340px` flex panels plus a page-level vertical
  bar overflow each other the same way. The probe measures **both** axes (`overflowX` and
  `overflowY`): a rig that reports only `Y` will stamp "no (fits)" while the user looks at a
  horizontal bar, and an unfalsifiable check is not a check.
- **Report height when media actually decodes.** An appended `<img>` has no layout height
  until it loads, so `showImage` and the render-artifact loop re-report on `onload`, not on
  `appendChild`. Reporting at append time measures the pre-image document, the host sizes the
  frame short, and the internal scrollbar returns — which is the specific failure this whole
  pass exists to eliminate. The one-shot height ladder ([0, 60, 250, 700, 1600] ms) cannot
  cover a preview that arrives seconds later.

## Roadmap

**M0 — DONE, verified by a human on a PC and a phone.** The three pass criteria came back
green in BOTH panels, including the one that decided the architecture: a `srcdoc` +
`sandbox="allow-scripts"` opaque-origin frame can (a) receive pointer/touch drags and paint
them, (b) `postMessage` its own height and have the host fit it, and (c) make a cross-origin
`fetch` to the toolbox API (server round-trip reported `200 cov=…`). Panel A (same-origin
control) behaved identically to panel B (the Open WebUI conditions), which is what makes the
B result attributable to the sandbox being permissive rather than to our own code working.
**Decision locked: the in-chat rich-UI embed mount is viable; M1 builds against it, with the
standalone page kept as the secondary mount.** Scrollbars: gone, after `scrollbar-gutter:
stable` + `overflow-x: clip` (see the deliberate-decisions note — it is a shipped-product CSS
concern, not spike-only).
**M1** the vertical slice. Corrected against the actual `imagegen` code (these three items in
the first draft were wrong and are fixed here):
  - The masked graph is **`edit/flux2-klein-inpaint.json`** with `MASK_SEGMENT_NODE="14"` =
    **`CLIPSegMask`** (text segmentation), which feeds GrowMask node 16 via `["14", 0]`. It is
    NOT a node to retarget to LoadImageMask in place. For a *painted* mask we add a sibling
    graph (registered in `models.json` under a new `inpaint_graph`, per the hard rule at
    workflows.py:158 that any inpaint graph reuse the exact `MASK_*` scaffold layout so
    id-based rewiring works) whose node 14 is a `LoadImageMask` fed by the uploaded mask PNG,
    keeping every other node id byte-identical.
  - The post-diffusion blend stack **already exists** in that graph: ColorMatchV2 = node 26,
    ImageCompositeMasked = node 30, the soft-edge chain = nodes 31–35. Do NOT rebuild it;
    parameterise the existing nodes. The earlier draft listed "ImageBlend + ColorMatchV2 +
    detail-preserve" as new work — only ImageBlend/opacity and the detail-preserve weight are
    genuinely additive.
  - `imagegen/_submit_edit` (tools.py:995) already does mask-file upload + `{"mask":
    [MASK_SEGMENT_NODE, 0]}` rewiring via `comfyui_client.upload_to_comfy` /
    `submit_workflow` / `wait_and_fetch`. Reuse those three, do not reimplement.
  Still to build in M1: a `jobs` table in `Store` (add to `_SCHEMA`, follow the
  `_migrate()`/`PRAGMA table_info` idempotent-add-column pattern at store.py:146) + a worker
  thread + cancel via `POST /interrupt`; artifacts uploaded under the calling user's own OWU
  key (`runtime.resolve_user_key(email)` already exists); `Toolbox.dispatch()` mounted from
  `stackd/serve.py` `do_GET`/`do_POST` behind `path.startswith("/toolbox/")`, ahead of the
  `_authed()` gate because toolbox routes carry their own HMAC launch token (never a cookie,
  never the admin key); Traefik `lab.<domain>` router whose chain omits
  `customFrameOptionsValue: SAMEORIGIN` (otherwise `chat.<domain>` cannot frame it) and sets
  `CSP: frame-ancestors https://chat.<domain>` instead; engine pre-warm via
  `runtime.request_capability("edit")` when an editor opens; and `REDEEM_ON_CREATE = True`.
  - **M1 LANDED** (this round, offline suite 202/202): the job store + worker are
    `jobs.JobQueue`/`jobs.JobStore` — deliberately NOT in `store.py`'s ledger (jobs.py's
    docstring explains the blast-radius reason; the original "add to `_SCHEMA`" note is
    superseded). `submit`/`cancel` are injected, so the whole state machine — queued→running
    →done|error|cancelled, the orphan sweep, cancel of a queued AND a running job — runs with
    fakes, no GPU/httpx/PIL. The REAL ComfyUI path is `engine.comfy_render`/`comfy_cancel`
    (lazy httpx), reusing `comfyui_client.upload_to_comfy`/`submit_workflow`/`wait_and_fetch`
    and saving the artifact under `runtime.resolve_user_key(email)`. `Toolbox.dispatch()` is
    mounted from `serve.py` ahead of `_authed()` via `_toolbox_dispatch`; `/toolbox/launch`
    mints the launch token from a forward-auth–verified email header (deny-by-default).
    Engine pre-warm fires on `/toolbox/embed`; `REDEEM_ON_CREATE` is now `True`.
  - **STILL OPEN for M1** (needs a live GPU host, cannot be answered offline): the end-to-end
    human verification (paint → render → artifact in OWU) that lets `MILESTONE` bump to
    `M1-passed`; and the `lab.<domain>` Traefik router (lives in the proxy/NAS-STACK repo —
    the compose here just exposes `/toolbox/*` on :11444 + the `STACKD_TOOLBOX_SECRET`/
    `STACKD_TOOLBOX_DB` env). A non-builtin model's `inpaint_graph` may need adding to
    `models.json` before `comfy_render` can serve it; the klein graph is the verified path.
**M2** the in-chat embed mount — a thin native OWU Function returning `HTMLResponse` +
`Content-Disposition: inline`, required because MCP tool returns do *not* get embed
treatment — plus the `retouch_image` MCP tool for assistant-driven flows.
**M3** non-destructive layer stack, gallery, saved recipes, batch.
**M4** video/music: multi-slot media tiers (`state.py`'s single `ImageSlot` -> per-kind
slots), artifact-typed jobs, cleaner support for `SaveVideo`/`SaveAudio*` outputs, and
bench-derived footprints per kind. The M1 job model is what makes a multi-minute render
survivable at all, which is why it was pulled forward rather than deferred with the models.

