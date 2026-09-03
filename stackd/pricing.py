"""Cost-savings tally — prices local token usage against what commercial APIs
would have charged, net of measured local energy. Config is `config/pricing.json`
(optional; sane defaults). Ported from a prior usage ledger,
trimmed to a flat per-dimension breakdown."""

from __future__ import annotations

import json
import pathlib

from stackd.store import Store

_DEFAULTS = {
    "electricity_price_per_kwh": 0.15,
    "host_baseline_w": 90,
    "hardware_cost": 0,
    "tiers": [
        {"label": "midtier", "ref": "anthropic/claude-3.5-haiku"},
        {"label": "frontier", "ref": "anthropic/claude-sonnet-4"},
    ],
    "manual_prices": [],
    "exclude_models": [],
}


def load_pricing(path: str | pathlib.Path | None) -> dict:
    cfg = dict(_DEFAULTS)
    if path and pathlib.Path(path).is_file():
        cfg.update(json.loads(pathlib.Path(path).read_text()))
    return cfg


def sync_manual_prices(store: Store, cfg: dict) -> int:
    n = 0
    for mp in cfg.get("manual_prices", []):
        store.set_price(
            mp["ref"], mp.get("from", "1970-01-01"),
            input_mtok=mp["input_mtok"], output_mtok=mp["output_mtok"],
            cache_read_mtok=mp.get("cache_read_mtok", 0.0),
        )
        n += 1
    return n


def _gross_for(rows: list[dict], store: Store, ref: str) -> float:
    total = 0.0
    for r in rows:
        p = store.price_on(ref, r["day"])
        if not p:
            continue
        total += r["prompt_tokens"] / 1e6 * p["input_mtok"]
        total += r["completion_tokens"] / 1e6 * p["output_mtok"]
        # Anthropic-style: cached input billed at the cache-read rate, not full input
        total -= r["cached_tokens"] / 1e6 * (p["input_mtok"] - p["cache_read_mtok"])
    return round(total, 4)


def _group(rows: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.get(key) or "?", []).append(r)
    return out


def savings(store: Store, cfg: dict, *, from_day: str | None = None,
            to_day: str | None = None) -> dict:
    rows = [r for r in store.usage_rows(from_day, to_day)
            if r["requested_model"] not in set(cfg.get("exclude_models", []))]
    days = sorted({r["day"] for r in rows})
    energy = store.energy_between(days[0], days[-1]) if days else {"gpu_kwh": 0.0, "host_kwh": 0.0}
    kwh = energy["gpu_kwh"] + energy["host_kwh"]
    energy_cost = round(kwh * cfg["electricity_price_per_kwh"], 4)

    tok_total = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows) or 1

    def tier_block(tier: dict) -> dict:
        gross = _gross_for(rows, store, tier["ref"])
        by = {
            dim: {
                k: {
                    "gross": _gross_for(g, store, tier["ref"]),
                    "prompt_tokens": sum(x["prompt_tokens"] for x in g),
                    "completion_tokens": sum(x["completion_tokens"] for x in g),
                    "reqs": sum(x["reqs"] for x in g),
                }
                for k, g in _group(rows, dim).items()
            }
            for dim in ("day", "user_email", "requested_model", "served_profile")
        }
        return {
            "label": tier["label"], "ref": tier["ref"],
            "gross": gross, "net": round(gross - energy_cost, 4),
            "by": by,
        }

    return {
        "from": days[0] if days else None, "to": days[-1] if days else None,
        "reqs": sum(r["reqs"] for r in rows),
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
        "energy": {**energy, "kwh": round(kwh, 4), "cost": energy_cost,
                   "price_per_kwh": cfg["electricity_price_per_kwh"]},
        "tiers": [tier_block(t) for t in cfg.get("tiers", [])],
        "hardware_cost": cfg.get("hardware_cost", 0),
        "token_total_for_share": tok_total,
    }


# -- OpenRouter price pull (manual; called by `stackctl prices --refresh`) --------


def refresh_openrouter(store: Store, cfg: dict, *, today: str, fetch=None) -> int:
    """Pull current prices for every tier ref from OpenRouter; write a new
    effective-dated point only when it changed. `fetch` overridable for tests."""
    import urllib.request

    def _default_fetch() -> dict:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models", headers={"accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    data = (fetch or _default_fetch)()
    want = {t["ref"] for t in cfg.get("tiers", [])}
    by_id = {m["id"]: m for m in data.get("data", [])}
    n = 0
    for ref in want:
        m = by_id.get(ref)
        if not m:
            continue
        pr = m.get("pricing", {})
        inp = float(pr.get("prompt", 0)) * 1e6
        out = float(pr.get("completion", 0)) * 1e6
        cache = float(pr.get("input_cache_read", 0) or 0) * 1e6
        cur = store.price_on(ref, today)
        if cur and abs(cur["input_mtok"] - inp) < 1e-6 and abs(cur["output_mtok"] - out) < 1e-6:
            continue
        store.set_price(ref, today, input_mtok=round(inp, 4), output_mtok=round(out, 4),
                        cache_read_mtok=round(cache, 4))
        n += 1
    store.set_meta("last_openrouter_fetch", today)
    return n
