"""Live smoke: drive a REAL text-to-image through stackd's shipped
sdcpp_client + sdcpp_pipelines against a running sd-server, exactly as the
MCP's sd.cpp generate path would build the body -- but bypassing the Open
WebUI save + dimension-autodetect helpers (those are engine-agnostic and
already covered). Proves the engine-specific submit/poll/decode path works
end to end. Writes the PNG to out-live/.
"""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)

from stackd.imagegen import sdcpp_client, sdcpp_pipelines  # noqa: E402

BASE = os.environ.get("SDCPP_BASE", "http://127.0.0.1:12345")
MODEL = os.environ.get("SDCPP_MODEL", "flux2-dev-turbo-sdcpp")
PROMPT = os.environ.get("SDCPP_PROMPT",
                        "a red fox wearing a tiny wizard hat, sitting on a mossy rock, "
                        "golden hour, sharp focus, cinematic")
W = int(os.environ.get("SDCPP_W", "1024"))
H = int(os.environ.get("SDCPP_H", "1024"))
SEED = int(os.environ.get("SDCPP_SEED", "1337"))
OUTDIR = os.path.join(HERE, "out-live")


async def main() -> int:
    os.makedirs(OUTDIR, exist_ok=True)
    sp = sdcpp_pipelines.sample_params(MODEL, "generate")
    body = {"prompt": PROMPT, "width": W, "height": H, "seed": SEED,
            "output_format": "png", "sample_params": sp["sample_params"]}
    if sp["lora"]:
        body["lora"] = sp["lora"]
    print(f"POST {BASE}/sdcpp/v1/img_gen  model={MODEL} {W}x{H} seed={SEED}")
    print("body sample_params:", body["sample_params"])
    print("body lora:", body.get("lora"))
    t0 = time.monotonic()
    job = await sdcpp_client.submit(body, base=BASE)
    print(f"submitted job id={job!r} (+{time.monotonic()-t0:.1f}s)")
    if not job:
        print("!! empty job id -- server did not accept the submission")
        return 2
    imgs = await sdcpp_client.wait_and_fetch(job, base=BASE, timeout=600)
    dt = time.monotonic() - t0
    print(f"completed: {len(imgs)} image(s) in {dt:.1f}s")
    paths = []
    for i, d in enumerate(imgs):
        p = os.path.join(OUTDIR, f"live_t2i_{SEED}_{i}.png")
        with open(p, "wb") as f:
            f.write(d)
        paths.append((p, len(d)))
        print(f"  wrote {p} ({len(d)} bytes)")
    ok = all(n > 1000 for _, n in paths)
    print("RESULT:", "PASS" if ok else "FAIL (suspiciously small)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
