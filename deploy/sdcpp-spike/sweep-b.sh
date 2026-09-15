#!/usr/bin/env bash
# Stylize-strength sweep: gate (b) at 0.6 barely relit the scene, so characterize the
# strength axis before anyone tunes a production ladder entry. Each run is ~160s.
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SDCPP_BASE=http://127.0.0.1:12347
for s in 0.75 0.9 1.0; do
  python3 "$D/spike.py" b --size 512 --strength "$s" --source "$D/out/gate_a.png" \
      --out "$D/out-sweep-$s" 2>&1 | tail -2
done
