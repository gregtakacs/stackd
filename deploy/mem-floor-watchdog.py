#!/usr/bin/env python3
"""Abort a runaway model load before the kernel picks who dies.

Pair this with `stackctl image bench --unsafe <model>` on a box where the GPU
and the host share one pool of RAM (this box: a Strix Halo iGPU with no VRAM
carve-out). `--unsafe` exists precisely to bypass stackd's estimate-based
refusals so a footprint can be measured when the current estimate is what is
blocking it — which means nothing stackd owns is guarding the run any more.
The container's own `mem_limit_gib` is not the safety net people assume it is
either: a cgroup charges file cache it is free to reclaim, while `MemAvailable`
discounts that same cache, so a load can drive the machine to a 5 GiB floor
without either limit firing (measured here on flux2-dev-turbo, 2026-09-08).
This watches the number that actually matters and removes the container.

Nothing here needs root: /proc/meminfo is world-readable and docker rm only
needs the docker group.

usage: ./mem-floor-watchdog.py [floor_gib=6] [seconds=600] [container=comfyui-rocm]
exits: 0 always — it either fired, or the window elapsed harmlessly.
"""
import subprocess
import sys
import time

floor = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
guest = sys.argv[3] if len(sys.argv) > 3 else "comfyui-rocm"


def mem_available_gib():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / 1048576.0
    raise SystemExit("mem-floor-watchdog: no MemAvailable in /proc/meminfo?")


def lowest(samples):
    return min(samples) if samples else None


t0 = time.time()
floor_hit = False
readings = []
print(f"mem-floor-watchdog: watching MemAvailable for {secs:.0f}s, floor "
      f"{floor:.1f} GiB, container {guest}", flush=True)
while time.time() - t0 < secs:
    avail = mem_available_gib()
    readings.append(avail)
    if avail < floor:
        print(f"MEM FLOOR t+{time.time() - t0:.1f}s  MemAvailable {avail:.2f} GiB "
              f"< {floor:.1f}  ->  removing {guest}", flush=True)
        subprocess.run(["docker", "rm", "-f", guest], check=False)
        print("mem-floor-watchdog: container removed; check what the load cost "
              "before benching it again", flush=True)
        floor_hit = True
        break
    time.sleep(0.4)

low = lowest(readings)
if not floor_hit:
    print(f"mem-floor-watchdog: window elapsed, lowest MemAvailable seen "
          f"{low:.2f} GiB — the load stayed inside the floor", flush=True)
