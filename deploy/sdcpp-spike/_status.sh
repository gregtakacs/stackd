#!/usr/bin/env bash
# Poll the detached live sd.cpp generation: log contents, output PNGs, process
# liveness, and the sd-server's own recent log lines. One command, so the
# invoking command string stays short and corruption-proof.
LOG=/tmp/sdcpp_live_gen.log
OUT=/home/greg/docker/Projects/LLM-Tools/stackd/deploy/sdcpp-spike/out-live
echo "==== probe log ===="
cat "$LOG" 2>/dev/null
echo "==== output dir ===="
ls -la "$OUT" 2>/dev/null
echo "==== probe process ===="
pgrep -af live_gen_probe2 || echo "not running"
echo "==== sd-server last 15 log lines ===="
docker logs --tail 15 sdcpp-spike 2>&1
echo "==== END ===="
