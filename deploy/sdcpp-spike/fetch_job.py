"""Fetch a sd-server job BY ID (the queue-time-aware follow-up to
live_gen_probe2): the server keeps finished jobs, so a client that stopped
polling early loses nothing -- the result is still retrievable. Decodes via
the SHIPPED sdcpp_client._extract_images and writes the PNG.
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)

from stackd.imagegen import sdcpp_client  # noqa: E402

BASE = os.environ.get("SDCPP_BASE", "http://127.0.0.1:12345").rstrip("/")
JOB = sys.argv[1] if len(sys.argv) > 1 else "job_6aa8df39_0000000a"
OUTDIR = os.path.join(HERE, "out-live")


def _get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


os.makedirs(OUTDIR, exist_ok=True)
deadline = time.monotonic() + 900
while True:
    j = _get(f"{BASE}/sdcpp/v1/jobs/{JOB}")
    st = j.get("status")
    print("poll:", st, flush=True)
    if st in ("completed", "failed", "cancelled"):
        break
    if time.monotonic() > deadline:
        print("still not terminal after 900s"); sys.exit(1)
    time.sleep(2.0)

if st != "completed":
    print("FAILED:", j.get("error")); sys.exit(1)

imgs = sdcpp_client._extract_images(j.get("result") or {})
print(f"decoded {len(imgs)} image(s) via sdcpp_client._extract_images", flush=True)
if not imgs:
    print("result keys:", sorted((j.get("result") or {}).keys())); sys.exit(1)
for i, d in enumerate(imgs):
    p = os.path.join(OUTDIR, f"live_t2i_1337_{i}.png")
    with open(p, "wb") as f:
        f.write(d)
    print(f"wrote {p} ({len(d)} bytes)", flush=True)
print("RESULT: PASS")
