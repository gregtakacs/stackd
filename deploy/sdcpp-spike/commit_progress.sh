#!/bin/bash
set -e
cd "$(dirname "$0")/../.."   # -> stackd repo root
# real deliverables only; keep scratch probes untracked
git add deploy/sdcpp-spike/spike.py deploy/sdcpp-spike/README.md
git commit -m "sdcpp spike: fix img2img custom_sigmas confound; record gate-(c) fidelity PASS

- spike.py: --no-custom-sigmas flag was declared but never used; every gate
  forced the t2i TURBO_SIGMAS onto img2img, fighting sd.cpp's strength-derived
  schedule (server WARN total_steps != custom_sigmas_count-1) and ghosting the
  1024^2/strength-0.75 stylize into a double exposure. Gates b/c now use sp_i2i
  (sd.cpp's own schedule); only gate a keeps the turbo sigmas.
- README: gate (c) outside-mask fidelity measured 5.81 (< 6.85 VAE noise floor),
  so the per-step latent pin (diffusion_engine.cpp:2519) works -- no colour
  bleed, no ColorMatchV2 crutch. Caveats: synthetic box mask (need CLIPSeg
  feathered retest), inside-box edit still ran under forced schedule. Trap 14."
echo "COMMIT_DONE"
git --no-pager log --oneline -1
