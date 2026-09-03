# catalog/ — measured footprint curves (P0)

One JSON file per engine config-signature. Each models a stack's **VRAM** and
**RAM** as a line over context length, fitted from the `points`. The validator
and reconciler prefer a curve when one exists and fall back to the stack's
declared `budget:` otherwise — so this directory is purely additive.

```
key         "<template>|<device>|<model>|p<parallel>"  (also the filename, sanitised)
source      "measured"  — from `stackctl bench` on the real box
            "estimate"  — a hand seed; `validate --strict` still flags these
points      { "vram": [[ctx, gib], ...], "ram": [[ctx, gib], ...] }   >=2 points to get a slope
```

## The seeds here are `estimate`s

Numbers are seeded from a prior proxy's
`scenario_everyday` comment. **Replace them with real measurements:**

```
stackctl bench everyday-chat      --points 131072,262144,393216
stackctl bench everyday-autocomplete --points 8192,16384,32768
stackctl bench coding-flash       --ingest coding-flash.measured.json   # vLLM: 1 point
stackctl bench everyday-image     --ingest everyday-image.measured.json  # ComfyUI: 1 point
```

`--ingest` takes `{"vram": [[ctx, gib], ...], "ram": [[...]]}` and skips the
spawn — use it for engines where you measured by hand or where context doesn't
sweep the footprint.

## Checking the fit

```
stackctl validate everyday --catalog catalog            # shows source + Δ vs declared per stack
stackctl validate everyday --catalog catalog --strict   # non-zero exit if any curve is not "measured"
stackctl solve everyday-chat --catalog catalog --budget-gib 42   # max ctx that fits
stackctl bench --verify everyday                         # spawn the profile, diff prediction vs reality (2 GiB tol)
```
