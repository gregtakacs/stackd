from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

from stackd.config.loader import load_config
from stackd.manager import Manager, ManagerError
from stackd.planner import plan_transition
from stackd.runner import FakeRunner, default_runner
from stackd.state import default_state_path
from stackd.validator import FitReport, validate_profile

DEFAULT_CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config"


def _env_or_file(name: str) -> str | None:
    """`<name>` or the contents of the file at `<name>_FILE` (Docker secrets)."""
    fp = os.environ.get(f"{name}_FILE")
    if fp and pathlib.Path(fp).is_file():
        return pathlib.Path(fp).read_text().strip()
    return os.environ.get(name)


def _fmt_report(r: FitReport) -> str:
    out = [f"profile {r.profile}: {'OK' if r.ok else 'FAIL'}"]
    out.append(f"  resident : {', '.join(r.resident) or '(none)'}")
    out.append("  pools:")
    for p in r.pools:
        mark = "ok  " if p.ok else "FAIL"
        out.append(
            f"    [{mark}] {p.pool:<13} {p.used_gib:>7.1f} / {p.limit_gib:<6.1f} GiB"
            f"   headroom {p.headroom_gib:>7.1f}"
        )
        for k, v in p.breakdown.items():
            out.append(f"           · {k:<22} {v:>7.1f}")
    out.append("  devices:")
    for d in r.devices:
        mark = "ok  " if d.ok else "FAIL"
        out.append(
            f"    [{mark}] {d.device:<13} {d.used_gib:>7.1f} / {d.budget_gib:<6.1f} GiB"
            f"   headroom {d.headroom_gib:>7.1f}   [{', '.join(d.models) or '-'}]"
        )
    if r.placement:
        out.append("  placement:")
        for name in r.resident:
            out.append(f"    {r.sources.get(name, 'declared'):<9} {name:<24} -> {r.placement[name]}")
    if r.unplaced:
        out.append(f"  unplaced : {', '.join(r.unplaced)}")
    if r.flags:
        out.append("  flags:")
        out += [f"    ! {f}" for f in r.flags]
    return "\n".join(out)


def _fmt_status(s: dict) -> str:
    out = [f"active   : {s['active_profile']}" + ("  (pinned)" if s["pinned"] else "")]
    if s["missing"]:
        out.append(f"missing  : {', '.join(s['missing'])}")
    out.append("stacks:")
    if not s["stacks"]:
        out.append("  (none running)")
    for n, st in s["stacks"].items():
        loc = f":{st['port']}" if st["port"] else (st["container"] or "")
        rs = f"  restarts={st['restarts']}" if st["restarts"] else ""
        out.append(f"  {st['state']:<8} {n:<24} {st['kind']:<9} {loc}{rs}")
    out.append("pools:")
    for p in s["pools"]:
        mark = "ok  " if p["ok"] else "FAIL"
        out.append(f"  [{mark}] {p['pool']:<13} {p['used']:>7.1f} / {p['limit']:<6.1f} GiB")
    for f in s["flags"]:
        out.append(f"  ! {f}")
    return "\n".join(out)


def _emit_events(events) -> None:
    for e in events:
        detail = f" — {e.detail}" if getattr(e, "detail", "") else ""
        print(f"  {e.action:<10} {e.stack}{detail}")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stackctl", description="two-device model stack/profile orchestrator"
    )
    ap.add_argument("-C", "--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    ap.add_argument("-O", "--config-overlay", default=os.environ.get("STACKD_CONFIG_OVERLAY"),
                    help="a 2nd config dir that wins per-file (private pools/models/catalog "
                         "on top of the generic shipped config)")
    ap.add_argument("--state", type=pathlib.Path, default=None, help="runtime state file")
    ap.add_argument("--models-dir", default="/models")
    ap.add_argument("--fake", action="store_true", help="in-memory runner (no real processes)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="parse the config and print a summary")

    v = sub.add_parser("validate", help="check a profile (or all) fits both memory pools")
    v.add_argument("profile", nargs="?")
    v.add_argument("--catalog", type=pathlib.Path, default=None,
                   help="measured footprint curves dir (default: <repo>/catalog if present)")
    v.add_argument("--strict", action="store_true",
                   help="exit non-zero if any resident stack has no measured curve")

    pl = sub.add_parser("plan", help="delta reconciliation between two profiles")
    pl.add_argument("frm", metavar="FROM")
    pl.add_argument("to", metavar="TO")

    pr = sub.add_parser("probe", help="read real device + host-memory totals from the box")
    pr.add_argument("--json", action="store_true")

    bn = sub.add_parser("bench", help="measure a stack's VRAM+RAM footprint curve")
    bn.add_argument("stack")
    bn.add_argument("--points", default="131072,262144,393216",
                    help="comma-separated context points")
    bn.add_argument("--catalog", type=pathlib.Path, default=None)
    bn.add_argument("--ingest", type=pathlib.Path, default=None,
                    help='JSON {"vram":[[ctx,gib]...],"ram":[[ctx,gib]...]} — skip spawning')
    bn.add_argument("--verify", metavar="PROFILE", default=None,
                    help="spawn PROFILE and diff measured pool totals vs the catalog")

    so = sub.add_parser("solve", help="max context for a budget, or footprint at a context")
    so.add_argument("stack")
    so.add_argument("--catalog", type=pathlib.Path, default=None)
    so.add_argument("--budget-gib", type=float, default=None)
    so.add_argument("--at-ctx", type=int, default=None)

    u = sub.add_parser("use", help="activate a profile (converge to it)")
    u.add_argument("profile")
    sub.add_parser("pin", help="hold the active profile up (disable idle self-evict)")
    sub.add_parser("unpin", help="release a pin")
    sub.add_parser("evict", help="force the active profile down to the default")
    sub.add_parser("status", help="what is running right now")
    r = sub.add_parser("route", help="resolve an api model name (may trigger a profile entry)")
    r.add_argument("api_name")
    sub.add_parser("tick", help="one supervisor pass: health, crash-restart, idle-evict")
    run = sub.add_parser("run", help="foreground supervisor loop (tick only, no HTTP)")
    run.add_argument("--interval", type=float, default=20.0)

    srv = sub.add_parser("serve", help="OpenAI-compatible HTTP front + supervisor (the daemon)")
    srv.add_argument("--host", default=os.environ.get("STACKD_HOST", "0.0.0.0"))
    srv.add_argument("--port", type=int, default=int(os.environ.get("STACKD_PORT", "11444")))
    srv.add_argument("--api-key", default=_env_or_file("STACKD_API_KEY"))
    srv.add_argument("--tick-interval", type=float, default=20.0)
    srv.add_argument("--warm-wait", type=float, default=120.0,
                     help="seconds to hold a request while its stack warms before 503")
    srv.add_argument("--db", default=os.environ.get("STACKD_DB"),
                     help="SQLite ledger + user-key store (default: XDG state dir)")
    srv.add_argument("--no-db", action="store_true", help="run without the datastore")
    srv.add_argument("--owu-url", default=os.environ.get("OPENWEBUI_BASE_URL"),
                     help="Open WebUI base URL — needed for /register key validation")
    srv.add_argument("--pricing", type=pathlib.Path, default=None,
                     help="pricing.json (default: <config>/pricing.json if present)")

    us = sub.add_parser("users", help="manage registered Open WebUI keys (needs --db)")
    us.add_argument("action", choices=["list", "reveal", "delete"])
    us.add_argument("email", nargs="?")
    us.add_argument("--db", default=os.environ.get("STACKD_DB"))

    sv = sub.add_parser("savings", help="cost-savings tally from the ledger (needs --db)")
    sv.add_argument("--db", default=os.environ.get("STACKD_DB"))
    sv.add_argument("--from", dest="from_day", default=None)
    sv.add_argument("--to", dest="to_day", default=None)
    sv.add_argument("--pricing", type=pathlib.Path, default=None)
    sv.add_argument("--json", action="store_true")

    mg = sub.add_parser("migrate", help="import a prior proxy's user_keys.json export into the DB")
    mg.add_argument("--db", default=os.environ.get("STACKD_DB"))
    mg.add_argument("--keys", type=pathlib.Path, required=True)

    pc = sub.add_parser("prices", help="refresh tier prices from OpenRouter (needs --db)")
    pc.add_argument("--db", default=os.environ.get("STACKD_DB"))
    pc.add_argument("--pricing", type=pathlib.Path, default=None)

    bd = sub.add_parser("build", help="build a model's local image (container.build) via the Docker API")
    bd.add_argument("models", nargs="*", help="model names; default: all with a build recipe")
    bd.add_argument("--all", action="store_true", help="build every model that declares container.build")
    bd.add_argument("--docker-url", default=os.environ.get("DOCKER_API_URL"),
                    help="Docker Engine API base (default: $DOCKER_API_URL)")

    sub.add_parser("init", help="build every local image, then validate all profiles")

    rl = sub.add_parser("reload", help="tell the running daemon to re-read config + .env and re-converge")
    rl.add_argument("--host", default=os.environ.get("STACKD_HOST", "127.0.0.1"))
    rl.add_argument("--port", type=int, default=int(os.environ.get("STACKD_PORT", "11444")))
    rl.add_argument("--api-key", default=_env_or_file("STACKD_API_KEY"))
    return ap


def _db_path(args) -> pathlib.Path:
    if getattr(args, "db", None):
        return pathlib.Path(args.db)
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return pathlib.Path(base) / "stackd" / "stackd.db"


def _pricing_path(args) -> pathlib.Path | None:
    if getattr(args, "pricing", None):
        return args.pricing
    p = args.config.parent / "pricing.json"
    return p if p.is_file() else None


def _cmd_build(args) -> int:
    """`build` / `init` — produce every (or the named) model's `container.build`
    image via the Docker API. `init` then validates all profiles."""
    from stackd.builder import build_image

    try:
        cfg = load_config(args.config)
    except Exception as e:  # noqa: BLE001
        print(f"config error: {e}", file=sys.stderr)
        return 2

    buildable = {
        n: m for n, m in cfg.models.items() if m.engine.container.build is not None
    }
    if args.cmd == "build" and getattr(args, "models", None):
        missing = [n for n in args.models if n not in buildable]
        if missing:
            print(f"no build recipe for: {', '.join(missing)}", file=sys.stderr)
            return 2
        buildable = {n: buildable[n] for n in args.models}

    url = getattr(args, "docker_url", None) or os.environ.get("DOCKER_API_URL")
    if not url:
        print("need --docker-url or $DOCKER_API_URL", file=sys.stderr)
        return 2

    if not buildable:
        print("no models declare container.build — nothing to build")
    for name, m in buildable.items():
        c = m.engine.container
        tag = c.image or f"stackd-{name}:local"
        print(f"=== build {name} -> {tag} ===")
        try:
            build_image(url, tag, c.build.context, dockerfile=c.build.dockerfile,
                        buildargs=c.build.args)
        except Exception as e:  # noqa: BLE001
            print(f"build {name} FAILED: {e}", file=sys.stderr)
            return 1

    if args.cmd == "init":
        from stackd.validator import validate_profile
        from stackd.catalog import Catalog
        cdir = args.config / "catalog"
        catalog = Catalog.load(cdir) if cdir.is_dir() else Catalog()
        bad = 0
        for pname in cfg.profiles:
            rep = validate_profile(cfg, pname, catalog)
            print(f"  {'OK  ' if rep.ok else 'FAIL'}  {pname}")
            bad += 0 if rep.ok else 1
        return 1 if bad else 0
    return 0


def _cmd_reload(args) -> int:
    """POST /reload to the RUNNING daemon (not a fresh Manager — that would be a
    split-brain like `stackctl use` via exec). See stackctl-use-vs-daemon notes."""
    import json as _json
    import urllib.error
    import urllib.request

    url = f"http://{args.host}:{args.port}/reload"
    headers = {"content-type": "application/json"}
    if args.api_key:
        headers["authorization"] = f"Bearer {args.api_key}"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            body = _json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = _json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        print(f"reload failed (HTTP {e.code}): {msg}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"reload failed: {e} — is `stackctl serve` running on {args.host}:{args.port}?",
              file=sys.stderr)
        return 1
    for line in body.get("reloaded", []):
        print(f"  {line}")
    for ev in body.get("events", []):
        d = f" — {ev['detail']}" if ev.get("detail") else ""
        print(f"  {ev['action']:<10} {ev['stack']}{d}")
    return 0


def _manager(args) -> Manager:
    state = pathlib.Path(args.state or default_state_path())
    runner = (
        FakeRunner(ready_after=2, persist_path=state.with_suffix(".fake.json"))
        if args.fake
        else default_runner()
    )
    return Manager(args.config, state, runner, models_dir=args.models_dir)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.config_overlay:      # so load_config() / Manager / serve all see it
        os.environ["STACKD_CONFIG_OVERLAY"] = str(args.config_overlay)

    if args.cmd in ("build", "init"):
        return _cmd_build(args)

    if args.cmd == "reload":
        return _cmd_reload(args)

    # config-only commands don't need a runner/state
    if args.cmd in ("show", "validate", "plan", "probe", "bench", "solve"):
        if args.cmd == "probe":
            from stackd.probe import probe, suggest
            import json as _json
            res = probe()
            print(_json.dumps(res, indent=2) if args.json else suggest(res))
            return 0

        try:
            cfg = load_config(args.config)
        except Exception as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2

        def _load_catalog(explicit):
            from stackd.catalog import Catalog
            d = explicit or (args.config / "catalog")
            return Catalog.load(d) if pathlib.Path(d).is_dir() else Catalog()

        if args.cmd == "show":
            print(f"config   : {args.config}")
            print(f"  pools    : {', '.join(cfg.pools)}")
            print(
                "  devices  : "
                + ", ".join(f"{d.name}({d.backend.value}->{d.vram_pool})" for d in cfg.devices.values())
            )
            print(f"  models   : {', '.join(cfg.models)}")
            print("  profiles :")
            for p in sorted(cfg.profiles.values(), key=lambda x: -x.priority):
                bits = [f"pri {p.priority}"]
                if p.default:
                    bits.append("default")
                if p.idle_evict:
                    bits.append(f"idle_evict {p.idle_evict}")
                print(f"    {p.profile:<14} {', '.join(bits)}")
                print(f"      models (priority order): {', '.join(p.models) or '-'}")
            return 0

        if args.cmd == "validate":
            catalog = _load_catalog(args.catalog)
            names = [args.profile] if args.profile else list(cfg.profiles)
            rc = 0
            for i, n in enumerate(names):
                if i:
                    print()
                try:
                    report = validate_profile(cfg, n, catalog=catalog)
                except KeyError:
                    print(f"unknown profile: {n}", file=sys.stderr)
                    rc = max(rc, 2)
                    continue
                print(_fmt_report(report))
                if not report.ok:
                    rc = max(rc, 1)
                if args.strict and (report.unplaced or [m for m,s in report.sources.items() if s!="measured"]):
                    rc = max(rc, 2)
            return rc

        if args.cmd == "plan":
            plan = plan_transition(cfg, args.frm, args.to)
            print(f"{plan.frm} -> {plan.to}")
            print(f"  keep     : {', '.join(plan.keep) or '-'}")
            print(f"  reload   : {', '.join(plan.reload) or '-'}")
            print(f"  spawn    : {', '.join(plan.spawn) or '-'}")
            print(f"  teardown : {', '.join(plan.teardown) or '-'}")
            return 0

        if args.cmd == "solve":
            from stackd.catalog import curve_key
            catalog = _load_catalog(args.catalog)
            if args.stack not in cfg.models:
                print(f"unknown stack: {args.stack}", file=sys.stderr)
                return 2
            cur = catalog.for_stack(cfg, args.stack)
            if cur is None:
                print(f"no curve for {args.stack} ({curve_key(cfg, args.stack)}) — run `stackctl bench`",
                      file=sys.stderr)
                return 2
            if args.at_ctx is not None:
                v, r = cur.estimate(args.at_ctx)
                print(f"{args.stack} @ ctx {args.at_ctx}: vram {v} GiB, ram {r} GiB  [{cur.source}]")
            if args.budget_gib is not None:
                mc = cur.vram.max_x(args.budget_gib)
                print(f"{args.stack} max ctx for {args.budget_gib} GiB VRAM: "
                      + (f"{int(mc):,}" if mc is not None else "unbounded (flat curve)"))
            if args.at_ctx is None and args.budget_gib is None:
                print(f"{args.stack}: vram = {cur.vram.intercept:.2f} + {cur.vram.slope:.3e}*ctx, "
                      f"ram = {cur.ram.intercept:.2f} + {cur.ram.slope:.3e}*ctx  [{cur.source}]")
            return 0

        # bench
        from stackd.bench import bench_stack, ingest
        from stackd.catalog import Catalog
        import json as _json
        cat_dir = args.catalog or (args.config / "catalog")
        catalog = Catalog.load(cat_dir)
        catalog.path = pathlib.Path(cat_dir)
        if args.stack not in cfg.models:
            print(f"unknown stack: {args.stack}", file=sys.stderr)
            return 2
        if args.ingest:
            pts = _json.loads(pathlib.Path(args.ingest).read_text())
            out = ingest(cfg, args.stack, catalog, pts)
            print(f"wrote {out}")
            return 0
        runner = FakeRunner(ready_after=1) if args.fake else default_runner()
        sampler_cls = None
        if args.fake:
            from stackd.bench import FakeSampler
            sampler = FakeSampler()
        else:
            from stackd.bench import NvidiaProcSampler
            sampler = NvidiaProcSampler()
        points = [int(x) for x in args.points.split(",") if x.strip()]
        out = bench_stack(cfg, args.stack, runner, sampler, catalog,
                          ctx_points=points, models_dir=args.models_dir,
                          source="estimate" if args.fake else "measured")
        print(f"wrote {out}")
        cur = catalog.for_stack(cfg, args.stack)
        print(f"  vram = {cur.vram.intercept:.2f} + {cur.vram.slope:.3e}*ctx")
        print(f"  ram  = {cur.ram.intercept:.2f} + {cur.ram.slope:.3e}*ctx")
        return 0

    # datastore commands (no Manager)
    if args.cmd in ("users", "savings", "migrate", "prices"):
        from stackd.store import Store
        db = _db_path(args)
        if args.cmd == "migrate":
            import json as _json
            store = Store.open(str(db))
            n = store.import_user_keys_json(_json.loads(args.keys.read_text()))
            print(f"imported {n} keys into {db}")
            return 0
        if not db.exists() and args.cmd != "users":
            print(f"no datastore at {db} — start `stackd serve` once, or `migrate`", file=sys.stderr)
        store = Store.open(str(db))
        if args.cmd == "users":
            if args.action == "list":
                for u in store.list_users():
                    print(f"  {u['email']:<32} {u['key_prefix']}…  reg {u['registered_at']}")
            elif args.action == "reveal":
                print(store.resolve_key(args.email) or "(not found)")
            elif args.action == "delete":
                print("deleted" if store.delete_user(args.email) else "(not found)")
            return 0
        from stackd.pricing import load_pricing, savings, sync_manual_prices, refresh_openrouter
        pcfg = load_pricing(_pricing_path(args))
        sync_manual_prices(store, pcfg)
        if args.cmd == "prices":
            import datetime as _dt
            n = refresh_openrouter(store, pcfg,
                                   today=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))
            print(f"updated {n} price point(s)")
            return 0
        out = savings(store, pcfg, from_day=args.from_day, to_day=args.to_day)
        if args.json:
            import json as _json
            print(_json.dumps(out, indent=2))
        else:
            print(f"{out['from']} → {out['to']}   {out['reqs']} reqs   "
                  f"{out['prompt_tokens']:,} in / {out['completion_tokens']:,} out tok")
            print(f"energy: {out['energy']['kwh']:.3f} kWh  ${out['energy']['cost']:.2f}")
            for t in out["tiers"]:
                print(f"  vs {t['label']:<10} gross ${t['gross']:.2f}   net ${t['net']:.2f}")
        return 0

    # runtime commands
    try:
        mgr = _manager(args)
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "use":
            print(f"use {args.profile}:")
            _emit_events(mgr.use(args.profile))
            print(_fmt_status(mgr.status()))
        elif args.cmd == "pin":
            mgr.pin()
            print(f"pinned {mgr.state.active_profile}")
        elif args.cmd == "unpin":
            mgr.unpin()
            print(f"unpinned {mgr.state.active_profile}")
        elif args.cmd == "evict":
            _emit_events(mgr.evict())
            print(f"active: {mgr.state.active_profile}")
        elif args.cmd == "status":
            print(_fmt_status(mgr.status()))
        elif args.cmd == "route":
            res = mgr.route(args.api_name)
            line = f"{res.status:<9} {res.model}"
            if res.endpoint:
                line += f" -> {res.endpoint}"
            if res.profile:
                line += f"  [{res.profile}]"
            print(line)
            if res.note:
                print(f"  {res.note}")
            return 0 if res.status in ("ok", "warming") else 1
        elif args.cmd == "tick":
            events = mgr.tick()
            if events:
                _emit_events(events)
            else:
                print("no change")
        elif args.cmd == "run":
            print(f"supervisor loop, interval {args.interval}s — Ctrl-C to stop")
            try:
                while True:
                    for e in mgr.tick():
                        detail = f" — {e.detail}" if getattr(e, "detail", "") else ""
                        print(f"[{time.strftime('%H:%M:%S')}] {e.action} {e.stack}{detail}")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nstopped")
        elif args.cmd == "serve":
            from stackd.serve import serve
            from stackd.store import Store
            from stackd.pricing import load_pricing, sync_manual_prices

            store = None
            ppath = _pricing_path(args)
            baseline_w = load_pricing(ppath).get("host_baseline_w", 90)
            if not args.no_db:
                db = _db_path(args)
                db.parent.mkdir(parents=True, exist_ok=True)
                store = Store.open(str(db))
                sync_manual_prices(store, load_pricing(ppath))
                print(f"ledger + user keys: {db}")
            serve(mgr, args.host, args.port, args.api_key,
                  tick_interval=args.tick_interval, warm_wait_s=args.warm_wait,
                  store=store, owu_base_url=args.owu_url,
                  pricing_path=str(ppath) if ppath else None, host_baseline_w=baseline_w)
    except ManagerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
