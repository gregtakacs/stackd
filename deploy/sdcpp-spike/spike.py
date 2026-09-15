#!/usr/bin/env python3
"""Phase-0 gate harness for the sd.cpp / flux2-dev-turbo evaluation.

Answers the three questions that decide whether sd.cpp can replace ComfyUI for this
pipeline -- ONE model must do all three, so a "pass" requires all of them:

  (a) generate : fp8mixed UNET + turbo LoRA at fal's 8-step sigmas
  (b) stylize  : img2img relight of (a)'s output
  (c) edit     : init_image + mask_image + ref_images[] -- the one that matters,
                 with a pixel check on whether the region OUTSIDE the mask survived.

Gate (c)'s check is the point of the whole exercise: the ComfyUI graph only pastes the
original back at the END (ImageCompositeMasked), which is why colour bled into the
untouched region and ColorMatchV2 got involved. sd.cpp pins the outside-mask latent
EVERY step (diffusion_engine.cpp:2519, not gated on model version), so if the mechanism
works the outside should come back near-identical modulo the VAE round-trip. Compare
against (b), a full re-render, which should differ everywhere.

Stdlib only (urllib/zlib/struct); PIL used opportunistically for the pixel diff.

  ./spike.py all
  ./spike.py c --mask-file mask.png --source photo.png
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

BASE = os.environ.get("SDCPP_BASE", "http://127.0.0.1:12345").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))

# fal/FLUX.2-dev-Turbo's published pre-shifted 8-step schedule. This list, not
# `sample_steps`, is what reproduces the ComfyUI graph's Flux2Scheduler.
TURBO_SIGMAS = [1.0, 0.6509, 0.4374, 0.2932, 0.1893, 0.1108, 0.0495, 0.00031]

GEN_PROMPT = ("a photograph of a wooden desk by a window -- a laptop, a ceramic mug, "
              "a small potted plant, and a stack of books, soft daylight")
STYLE_PROMPT = "the same scene at blue hour in light rain, moody cinematic lighting"
EDIT_PROMPT = "a matte red ceramic mug sitting on the desk"


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(path: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read())


def submit(params: dict) -> str:
    r = _post("/sdcpp/v1/img_gen", params)
    return r.get("id") or r.get("job_id") or ""


def wait(job_id: str, timeout: int = 900) -> dict:
    deadline = time.monotonic() + timeout
    path = f"/sdcpp/v1/jobs/{job_id}"
    while time.monotonic() < deadline:
        j = _get(path)
        if j.get("status") in ("completed", "failed", "cancelled"):
            return j
        time.sleep(1.0)
    raise TimeoutError(f"job {job_id} still running after {timeout}s")


def b64img(path: str) -> str:
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def save(b64: str, out_path: str) -> int:
    data = base64.b64decode(b64)
    with open(out_path, "wb") as f:
        f.write(data)
    return len(data)


def make_box_mask(w: int, h: int, out_path: str) -> str:
    """Center-box mask, WHITE = regenerate (image.cpp:337 computes (1-mask)*image, so
    white is the region to repaint -- same polarity as ComfyUI). Hand-rolled PNG so the
    gate needs no PIL; CLIPSeg replaces this in Phase 1."""
    x0, y0, x1, y1 = w // 4, h // 4, (3 * w) // 4, (3 * h) // 4
    raw = b"".join(b"\x00" + bytes((255 if (x0 <= x < x1 and y0 <= y < y1) else 0)
                                   for x in range(w)) for y in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(out_path, "wb") as f:
        f.write(png)
    return out_path


def region_diff(src: str, res: str, mask: str):
    """Mean/max abs pixel difference between src and res over pixels where the mask is
    BLACK, i.e. outside the edited region. None when PIL is unavailable."""
    try:
        from PIL import Image
    except ImportError:
        return None
    a = Image.open(src).convert("RGB")
    b = Image.open(res).convert("RGB")
    m = Image.open(mask).convert("L").resize(a.size)
    if b.size != a.size:
        b = b.resize(a.size)
    pa, pb, pm = list(a.getdata()), list(b.getdata()), list(m.getdata())
    tot = n = worst = 0
    for (r1, g1, b1), (r2, g2, b2), mv in zip(pa, pb, pm):
        if mv > 127:                       # inside the edit region: may differ
            continue
        d = abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
        tot += d
        n += 1
        worst = max(worst, d)
    if not n:
        return None
    return {"outside_pixels": n,
            "mean_abs_sum_diff": round(tot / n, 2),
            "max_abs_sum_diff": worst}


def container_peak_gib():
    try:
        cid = open(os.path.join(HERE, ".cid")).read().strip()
        v = open(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.peak").read().strip()
        return round(int(v) / 2**30, 2)
    except Exception:
        return None


def run_gate(name: str, params: dict, out_dir: str, timeout: int) -> dict:
    t0 = time.monotonic()
    try:
        jid = submit(params)
        if not jid:
            return {"gate": name, "ok": False, "error": "no job id returned"}
        j = wait(jid, timeout)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        detail = e.read().decode(errors="replace")[:400] if isinstance(e, urllib.error.HTTPError) else ""
        return {"gate": name, "ok": False, "error": f"{e!r} {detail}"}
    dt = round(time.monotonic() - t0, 1)
    if j.get("status") != "completed":
        return {"gate": name, "ok": False, "error": j.get("error") or j.get("status"), "secs": dt}
    res = j.get("result") or {}
    # Documented shape (api.md:868): result.images[] is a LIST of {index, b64_json}.
    # Reading result.b64_json instead yields a bogus "completed with no image" -- the
    # sampler can run to 7/7 and still look like a failure, so keep both shapes and, on
    # a miss, dump the keys instead of guessing.
    b64s = []
    for it in (res.get("images") or []):
        if isinstance(it, dict) and it.get("b64_json"):
            b64s.append(it["b64_json"])
        elif isinstance(it, str):
            b64s.append(it)
    if not b64s and res.get("b64_json"):
        b64s = [res["b64_json"]]
    if not b64s:
        return {"gate": name, "ok": False, "secs": dt,
                "error": "completed with no image", "result_keys": sorted(res.keys())}
    files = []
    for i, b in enumerate(b64s):
        p = os.path.join(out_dir, f"gate_{name}.png" if i == 0 else f"gate_{name}_{i + 1}.png")
        files.append({"file": p, "bytes": save(b, p)})
    return {"gate": name, "ok": True, "secs": dt,
            "file": files[0]["file"], "bytes": files[0]["bytes"], "images": len(files)}



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gate", choices=["a", "b", "c", "all"])
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--guidance", type=float, default=4.0, help="FluxGuidance analogue")
    ap.add_argument("--strength", type=float, default=0.6, help="stylize denoise")
    ap.add_argument("--edit-strength", type=float, default=1.0)
    ap.add_argument("--lora", default="Flux_2-Turbo-LoRA_comfyui.safetensors")
    ap.add_argument("--no-lora", action="store_true")
    ap.add_argument("--source", default="", help="source png (default: gate a output)")
    ap.add_argument("--mask-file", default="", help="mask png (default: synthesized box)")
    ap.add_argument("--no-custom-sigmas", action="store_true",
                    help="omit custom_sigmas so sd.cpp derives its own schedule. For "
                         "img2img (stylize/edit) this is the HONEST test: forcing the "
                         "t2i turbo sigmas on top of init_image+strength overrides "
                         "sd.cpp's strength-derived schedule and fights it -- the "
                         "`total_steps != custom_sigmas_count - 1` WARN is the tell, "
                         "and at 1024^2/strength .75 it produced ghosted double-"
                         "exposures (duplicated laptop, floating third mug).")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    W = H = a.size
    lora = [] if a.no_lora else [{"path": a.lora, "multiplier": 1.0}]
    sp = {"sample_steps": a.steps, "sample_method": "euler",
          "custom_sigmas": TURBO_SIGMAS,
          "guidance": {"txt_cfg": 1.0, "distilled_guidance": a.guidance}}
    # Gates b/c are img2img: there sd.cpp derives its own schedule from `strength`,
    # so forcing the t2i turbo sigmas on top overrides that derivation and the two
    # fight (server WARN: `total_steps != custom_sigmas_count - 1`). At 1024^2 /
    # strength 0.75 that produced a ghosted double exposure (duplicated laptop,
    # floating third mug). With --no-custom-sigmas, img2img omits them instead.
    sp_i2i = dict(sp)
    if a.no_custom_sigmas:
        sp_i2i.pop("custom_sigmas", None)

    # NOTE: /sdcpp/v1/capabilities advertises mask_image/ref_images/lora from a STATIC
    # table (routes_sdcpp.cpp:175), so it proves the route exists, not that it works on
    # Flux.2. Record it; trust the pixels.
    try:
        feats = (_get("/sdcpp/v1/capabilities").get("features_by_mode") or {}).get("img_gen")
    except Exception as e:
        feats = None
        print(f"WARN capabilities probe failed: {e!r}", file=sys.stderr)

    results: list[dict] = []
    gates = ["a", "b", "c"] if a.gate == "all" else [a.gate]
    src_a = os.path.join(a.out, "gate_a.png")

    def emit(r: dict) -> dict:
        results.append(r)
        print(json.dumps(r), flush=True)
        return r

    if "a" in gates:
        emit(run_gate("a", {"prompt": GEN_PROMPT, "width": W, "height": H, "seed": a.seed,
                            "sample_params": sp, "lora": lora, "output_format": "png"},
                      a.out, a.timeout))

    if "b" in gates:
        src = a.source or src_a
        if not os.path.exists(src):
            emit({"gate": "b", "ok": False, "error": f"no source {src}; run gate a first"})
        else:
            emit(run_gate("b", {"prompt": STYLE_PROMPT, "width": W, "height": H, "seed": a.seed,
                                "init_image": b64img(src), "strength": a.strength,
                                "sample_params": sp_i2i, "lora": lora, "output_format": "png"},
                          a.out, a.timeout))

    if "c" in gates:
        src = a.source or src_a
        if not os.path.exists(src):
            emit({"gate": "c", "ok": False, "error": f"no source {src}; run gate a first"})
        else:
            mask = a.mask_file or os.path.join(a.out, "mask_box.png")
            if not os.path.exists(mask):
                make_box_mask(W, H, mask)
            r = run_gate("c", {"prompt": EDIT_PROMPT, "width": W, "height": H, "seed": a.seed,
                               "init_image": b64img(src), "mask_image": b64img(mask),
                               "ref_images": [b64img(src)],
                               "ref_image_args": "preset=flux2",
                               "strength": a.edit_strength,
                               "sample_params": sp_i2i, "lora": lora, "output_format": "png"},
                         a.out, a.timeout)
            if r.get("ok"):
                r["outside_mask_fidelity"] = region_diff(src, r["file"], mask)
                print("outside-mask fidelity:", json.dumps(r["outside_mask_fidelity"]), flush=True)
            emit(r)

    ok = {g: next((x["ok"] for x in results if x["gate"] == g), False) for g in ("a", "b", "c")}
    print("\n=== summary ===")
    print(json.dumps({"capabilities_features": feats,
                      "container_peak_ram_gib": container_peak_gib(),
                      "requested_gates": gates,
                      "gates": results,
                      "ALL_THREE_PASS": all(ok.values())}, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())



