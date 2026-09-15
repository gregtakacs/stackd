"""Host-runnable live smoke that talks to a running sd-server using STDLIB
urllib only (the host python has no httpx). It still exercises the SHIPPED
sd.cpp code that matters for correctness:
  * body construction -> stackd.imagegen.sdcpp_pipelines.sample_params
  * image decoding    -> stackd.imagegen.sdcpp_client._extract_images
Only the raw submit/poll HTTP is done with urllib here; the httpx submit/poll
in sdcpp_client is exercised separately inside the daemon container.
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)

from stackd.imagegen import sdcpp_client, sdcpp_pipelines  # noqa: E402

BASE = os.environ.get("SDCPP_BASE", "http://127.0.0.1:12345").rstrip("/")
MODEL = os.environ.get("SDCPP_MODEL", "flux2-dev-turbo-sdcpp")
PROMPT = os.environ.get("SDCPP_PROMPT",
                        "a red fox wearing a tiny wizard hat, sitting on a mossy rock, "
                        "golden hour, sharp focus, cinematic")
W = int(os.environ.get("SDCPP_W", "1024"))
H = int(os.environ.get("SDCPP_H", "1024"))
SEED = int(os.environ.get("SDCPP_SEED", "1337"))
OUTDIR = os.path.join(HERE, "out-live")


def _post(url, body, timeout=30):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    sp = sdcpp_pipelines.sample_params(MODEL, "generate")
    body = {"prompt": PROMPT, "width": W, "height": H, "seed": SEED,
            "output_format": "png", "sample_params": sp["sample_params"]}
    if sp["lora"]:
        body["lora"] = sp["lora"]
    print("sample_params from shipped manifest:", body["sample_params"])
    print("lora from shipped manifest:", body.get("lora"))
    t0 = time.monotonic()
    r = _post(f"{BASE}/sdcpp/v1/img_gen", body)
    job = r.get("id") or r.get("job_id") or ""
    print(f"submitted job id={job!r} (+{time.monotonic()-t0:.1f}s)")
    if not job:
        print("!! empty job id; response:", json.dumps(r)[:400]); return 2
    while True:
        j = _get(f"{BASE}/sdcpp/v1/jobs/{job}")
        status = j.get("status")
        if status in ("completed", "failed", "cancelled"):
            break
        if time.monotonic() - t0 > 600:
            print("TIMEOUT"); return 1
        time.sleep(1.0)
    dt = time.monotonic() - t0
    print(f"job status={status} after {dt:.1f}s")
    if status != "completed":
        print("error:", j.get("error")); return 1
    imgs = sdcpp_client._extract_images(j.get("result") or {})
    print(f"decoded {len(imgs)} image(s) via sdcpp_client._extract_images")
    if not imgs:
        print("result keys:", sorted((j.get("result") or {}).keys())); return 1
    for i, d in enumerate(imgs):
        p = os.path.join(OUTDIR, f"live_t2i_{SEED}_{i}.png")
        with open(p, "wb") as f:
            f.write(d)
        print(f"  wrote {p} ({len(d)} bytes)")
    ok = all(len(d) > 1000 for d in imgs)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
