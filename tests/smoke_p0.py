"""P0 — footprint-curve catalog, validator wiring, bench harness, probe parsers.
No deps: `python3 tests/smoke_p0.py`."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _env  # noqa: F401,E402  — reference ${VAR} env for config interpolation

from stackd.bench import FakeSampler, bench_stack, ingest, verify  # noqa: E402
from stackd.catalog import Axis, Catalog, curve_key  # noqa: E402
from stackd.config.loader import load_config  # noqa: E402
from stackd.probe import parse_meminfo, parse_nvidia_smi  # noqa: E402
from stackd.runner import FakeRunner  # noqa: E402
from stackd.validator import validate_profile  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = ROOT / "config"
SEED = ROOT / "catalog"
CHECKS: list[tuple[str, bool]] = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def main() -> int:
    cfg = load_config(CFG)

    # --- linear fit ------------------------------------------------------------
    ax = Axis.from_points([[100, 12.0], [200, 14.0], [300, 16.0]])
    check("fit slope", approx(ax.slope, 0.02))
    check("fit intercept", approx(ax.intercept, 10.0))
    check("at(250) interpolates", approx(ax.at(250), 15.0))
    check("max_x solves the inverse", approx(ax.max_x(15.0), 250.0))
    flat = Axis.from_points([[0, 88.0]])
    check("single point -> flat", flat.slope == 0.0 and flat.at(999) == 88.0)
    check("flat axis never binds", flat.max_x(50.0) is None)

    # --- fixture catalog (stable, independent of the live catalog/ contents) ----
    fix = pathlib.Path(tempfile.mkdtemp()) / "cat"
    fix.mkdir(parents=True)
    fc = Catalog(path=fix)
    fc.write_curve(curve_key(cfg, "chat", "cuda0"), model="m", engine="llamacpp-cuda",
                   device="cuda0", source="estimate",
                   vram_points=[[65536, 22.0], [262144, 40.0]], ram_points=[[65536, 2.0], [262144, 4.0]])
    cat = Catalog.load(fix)
    check("fixture catalog loaded", len(cat.curves) == 1)
    cur = cat.for_model(cfg, "chat", "cuda0")
    check("curve matched by config-signature", cur is not None and cur.source == "estimate")
    check("slope from 2 points", approx(cur.vram.slope, (40.0 - 22.0) / (262144 - 65536), 1e-6))
    check("estimate @ 262144 == 40", approx(cur.estimate(262144)[0], 40.0, 0.1))
    check("max_x inverts the line", approx(cur.vram.max_x(31.0), 163840, 2000))

    # --- update_timings: EMA fold + gross-outlier rejection --------------------
    tk = curve_key(cfg, "chat", "cuda0")
    for _ in range(4):
        fc.update_timings(tk, load_s=260.0)          # establish EMA ~260, n=4
    t = fc.update_timings(tk, load_s=1005.0)         # cold JIT compile — must NOT fold
    check("outlier not folded into the EMA", 250.0 < t["load_s"] < 275.0)
    check("outlier still visible as last_load_s", t["last_load_s"] == 1005.0)
    check("outlier does not bump n", t["n"] == 4)
    t = fc.update_timings(tk, load_s=360.0)          # 1.38x — a slow-but-plausible load, folds
    check("in-band sample folds", 275.0 < t["load_s"] < 330.0 and t["n"] == 5)

    # --- validator prefers the catalog + reports source/delta -------------------
    base = validate_profile(cfg, "chat")
    withcat = validate_profile(cfg, "chat", catalog=cat)
    cuda_base = next(p for p in base.pools if p.pool == "cuda_vram").used_gib
    cuda_cat = next(p for p in withcat.pools if p.pool == "cuda_vram").used_gib
    check("catalog changes the pool total", cuda_cat != cuda_base)
    check("cuda_vram with the chat curve = 40", approx(cuda_cat, 40.0, 0.2))
    check("source recorded as estimate", withcat.sources["chat"] == "estimate")
    check("delta vs declared recorded (40 curve - 30 declared)",
          withcat.deltas["chat"][0] == 10.0)
    check("models with no curve flagged unmeasured",
          "code-autocomplete" in withcat.unmeasured)
    check("no-catalog: sources all 'declared'",
          set(base.sources.values()) == {"declared"})
    check("no-catalog: nothing flagged unmeasured", base.unmeasured == [])

    # --- ingest -> measured, delta flips, no longer unmeasured -------------------
    tmp = pathlib.Path(tempfile.mkdtemp()) / "cat"
    tmp.mkdir(parents=True)
    icat = Catalog(path=tmp)
    ingest(cfg, "chat", icat,
           {"vram": [[262144, 41.0]], "ram": [[262144, 3.2]]}, notes="hand-measured")
    icat2 = Catalog.load(tmp)
    rep = validate_profile(cfg, "chat", catalog=icat2)
    check("ingested curve is 'measured'", rep.sources["chat"] == "measured")
    check("chat no longer unmeasured", "chat" not in rep.unmeasured)
    check("a model with no curve is still unmeasured", "code-autocomplete" in rep.unmeasured)
    check("measured value used (41 vram)",
          approx(next(p for p in rep.pools if p.pool == "cuda_vram").used_gib, 41.0, 0.5))

    # --- bench harness with fakes ----------------------------------------------------
    bdir = pathlib.Path(tempfile.mkdtemp()) / "bench"
    bcat = Catalog(path=bdir)
    sampler = FakeSampler(vram0=30.0, vram_per_ktok=0.04, ram0=2.0, ram_per_ktok=0.005)
    out = bench_stack(cfg, "chat", FakeRunner(ready_after=1), sampler, bcat,
                      ctx_points=[131072, 262144, 393216], models_dir="/models",
                      source="estimate")
    check("bench wrote a curve file", out.exists())
    fitted = bcat.for_model(cfg, "chat", "cuda0")
    # FakeSampler is linear in ctx; slope should recover vram_per_ktok/1000
    check("bench recovered the vram slope", approx(fitted.vram.slope, 0.04 / 1000, 1e-6))
    check("bench recovered the ram slope", approx(fitted.ram.slope, 0.005 / 1000, 1e-6))
    check("bench curve key is the config-signature",
          fitted.key == curve_key(cfg, "chat", "cuda0"))

    # --- verify (predicted vs measured diff) — self-consistent vs the prediction
    pred = {p.pool: p.used_gib for p in validate_profile(cfg, "chat", catalog=icat2).pools}
    near = {k: v + 1.0 for k, v in pred.items()}
    far = {k: v + 5.0 for k, v in pred.items()}
    vr = verify(cfg, "chat", icat2, near, tol_gib=2.0)
    check("verify computes per-pool deltas", len(vr["pools"]) == 2)
    check("verify passes within 2 GiB", vr["pass"] is True)
    vr2 = verify(cfg, "chat", icat2, far, tol_gib=2.0)
    check("verify fails when off by >2 GiB", vr2["pass"] is False)

    # --- probe parsers -----------------------------------------------------------
    gpus = parse_nvidia_smi("0, NVIDIA RTX PRO 6000, 97887\n")
    check("nvidia-smi parse", gpus and approx(gpus[0]["vram_total_gib"], 95.6, 0.2))
    mem = parse_meminfo("MemTotal:       131010816 kB\nMemAvailable:    9740000 kB\n")
    check("meminfo parse total", approx(mem["total_gib"], 124.9, 0.2))
    check("meminfo parse used", mem["used_gib"] > 110)

    ok = all(p for _, p in CHECKS)
    for name, passed in CHECKS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'all passed' if ok else 'FAILURES'} ({sum(p for _, p in CHECKS)}/{len(CHECKS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
