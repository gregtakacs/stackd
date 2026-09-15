#!/bin/bash
set -e
cd "$(dirname "$0")"
setsid python3 spike.py b --size 1024 --strength 0.75 \
  --source out/gate_a.png --out out-AB-sig >ab-sig.log 2>&1 </dev/null &
disown
setsid python3 spike.py b --size 1024 --strength 0.75 --no-custom-sigmas \
  --source out/gate_a.png --out out-AB-nosig >ab-nosig.log 2>&1 </dev/null &
disown
echo LAUNCHED_BOTH
