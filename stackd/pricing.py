"""Cost-savings tally — prices local token usage against what commercial APIs
would have charged, net of measured local energy. Config is `config/pricing.json`
(optional; sane defaults). Ported from a prior usage ledger,
trimmed to a flat per-dimension breakdown."""

from __future__ import annotations

import datetime
import json
import pathlib

from stackd.store import Store


def _d(iso: str) -> datetime.date:
    return datetime.date.fromisoformat(iso)

_DEFAULTS = {
    "currency": "USD",
    "electricity_price_per_kwh": 0.15,
    "host_baseline_w": 90,
    "hardware_cost": 0,
    "payback_tier": None,          # tier label whose net drives the payback %
    "tiers": [
        {"label": "midtier", "ref": "anthropic/claude-3.5-haiku"},
        {"label": "frontier", "ref": "anthropic/claude-sonnet-4"},
    ],
    "manual_prices": [],
    "exclude_models": [],
    # frontier prefix-cache estimate for the ledger's cached_tokens — models what
    # an Anthropic/OpenAI cache would discount (ttl_s = Anthropic's 5-min default,
    # min_prefix_tokens = OpenAI's threshold). See promptcache.py.
    "prompt_cache": {"ttl_s": 300, "min_prefix_tokens": 1024, "history_per_model": 24},
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


def _tier_rows(rows: list[dict], tier: dict) -> list[dict]:
    """A tier may scope to `served_models` — only price rows served by one of
    those. stackd only keeps `requested_model` at day grain, so match loosely
    (exact or substring); an unscoped tier prices everything."""
    sm = tier.get("served_models")
    if not sm:
        return rows
    return [r for r in rows if any(
        s == (r["requested_model"] or "") or s in (r["requested_model"] or "") for s in sm)]


def savings(store: Store, cfg: dict, *, from_day: str | None = None,
            to_day: str | None = None) -> dict:
    rows = [r for r in store.usage_rows(from_day, to_day)
            if r["requested_model"] not in set(cfg.get("exclude_models", []))]
    days = sorted({r["day"] for r in rows})
    energy = store.energy_between(days[0], days[-1]) if days else {"gpu_kwh": 0.0, "host_kwh": 0.0}
    kwh = energy["gpu_kwh"] + energy["host_kwh"]
    energy_cost = round(kwh * cfg["electricity_price_per_kwh"], 4)
    span_days = ((_d(days[-1]) - _d(days[0])).days + 1) if days else 0

    tok_total = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows) or 1
    tier_refs = [t["ref"] for t in cfg.get("tiers", [])]
    priced_refs = {ref for ref in tier_refs if any(store.price_on(ref, d) for d in (days or []))}
    pricing_stale = bool(tier_refs) and not all(r in priced_refs for r in tier_refs)

    def tier_block(tier: dict) -> dict:
        trows = _tier_rows(rows, tier)
        gross = _gross_for(trows, store, tier["ref"])
        # apportion energy to a breakdown key by its share of this tier's gross
        def _net(g_gross: float) -> float:
            share = (g_gross / gross) if gross else 0.0
            return round(g_gross - energy_cost * share, 4)
        by = {
            dim: {
                k: {
                    "gross": (kg := _gross_for(g, store, tier["ref"])),
                    "net": _net(kg),
                    "prompt_tokens": sum(x["prompt_tokens"] for x in g),
                    "completion_tokens": sum(x["completion_tokens"] for x in g),
                    "reqs": sum(x["reqs"] for x in g),
                }
                for k, g in _group(trows, dim).items()
            }
            for dim in ("day", "user_email", "requested_model", "served_profile")
        }
        net = round(gross - energy_cost, 4)
        price = store.price_on(tier["ref"], days[-1]) if days else None
        return {
            "label": tier["label"], "ref": tier["ref"],
            "scoped": bool(tier.get("served_models")),
            "served_models": tier.get("served_models") or [],
            "price": price,
            "gross": gross, "net": net,
            "annualized": round(net / span_days * 365, 2) if span_days else 0.0,
            "by": by,
        }

    blocks = [tier_block(t) for t in cfg.get("tiers", [])]
    hw = float(cfg.get("hardware_cost", 0) or 0)
    pay_label = cfg.get("payback_tier") or (blocks[-1]["label"] if blocks else None)
    pay = next((b for b in blocks if b["label"] == pay_label), blocks[-1] if blocks else None)
    payback_pct = round(max(0.0, pay["net"]) / hw * 100, 2) if (hw > 0 and pay) else None

    _pfm = sum(r.get("prefill_ms") or 0 for r in rows)
    _pft = sum(r.get("prefill_tok") or 0 for r in rows)
    _dcm = sum(r.get("decode_ms") or 0 for r in rows)
    _dct = sum(r.get("decode_tok") or 0 for r in rows)
    _cxn = sum(r.get("ctx_n") or 0 for r in rows)
    return {
        "from": days[0] if days else None, "to": days[-1] if days else None,
        "span_days": span_days,
        "currency": cfg.get("currency", "USD"),
        "reqs": sum(r["reqs"] for r in rows),
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
        # token-weighted throughput + context size over the range (timed rows only)
        "throughput": {
            "decode_tps": round(_dct * 1000 / _dcm, 1) if _dcm else 0.0,
            "prefill_tps": round(_pft * 1000 / _pfm, 1) if _pfm else 0.0,
            "ctx_avg": round(sum(r.get("ctx_tokens_sum") or 0 for r in rows) / _cxn) if _cxn else 0,
            "ctx_max": max((r.get("ctx_tokens_max") or 0 for r in rows), default=0),
        },
        "energy": {**energy, "kwh": round(kwh, 4), "cost": energy_cost,
                   "price_per_kwh": cfg["electricity_price_per_kwh"]},
        "tiers": blocks,
        "payback_tier": pay_label,
        "payback_pct": payback_pct,
        "hardware_cost": hw or None,
        "pricing_stale": pricing_stale,
        "last_openrouter_fetch": store.get_meta("last_openrouter_fetch"),
        "token_total_for_share": tok_total,
    }


def savings_facts(store: Store, cfg: dict, *, from_day: str | None = None,
                  to_day: str | None = None) -> dict:
    """Priced fact table at finest grain, for the dashboard's interactive
    drill-down. One entry per distinct
    ``(day, user_email, requested_model, served_stack, served_model, served_profile)``
    in range, with per-tier gross. ``served_stack`` is the stackd unit (``coding``);
    ``served_model`` is the checkpoint on disk (``Qwen3.8-Flash-Next-NVFP4``). Net is
    left to the caller and apportioned exactly as ``savings()`` does::

        net(subset) = gross(subset) - energy_cost * gross(subset) / tier.total_gross
    """
    excl = set(cfg.get("exclude_models", []))
    rows = [r for r in store.usage_rows(from_day, to_day)
            if r["requested_model"] not in excl]
    days = sorted({r["day"] for r in rows})
    energy = (store.energy_between(days[0], days[-1]) if days
              else {"gpu_kwh": 0.0, "host_kwh": 0.0})
    energy_cost = round((energy["gpu_kwh"] + energy["host_kwh"])
                        * cfg["electricity_price_per_kwh"], 4)

    tiers = cfg.get("tiers", [])
    tier_meta = [{"label": t["label"], "ref": t["ref"],
                  "total_gross": _gross_for(_tier_rows(rows, t), store, t["ref"])}
                 for t in tiers]

    keys = ("day", "user_email", "requested_model",
            "served_stack", "served_model", "served_profile")
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)

    facts = []
    for key, g in groups.items():
        rec = dict(zip(keys, key))
        for col in ("reqs", "prompt_tokens", "completion_tokens", "cached_tokens",
                    "prefill_ms", "prefill_tok", "decode_ms", "decode_tok",
                    "ctx_tokens_sum", "ctx_n"):
            rec[col] = sum(x.get(col) or 0 for x in g)
        rec["ctx_tokens_max"] = max((x.get("ctx_tokens_max") or 0 for x in g), default=0)
        rec["gross"] = {t["label"]: _gross_for(_tier_rows(g, t), store, t["ref"])
                        for t in tiers}
        facts.append(rec)

    return {
        "from": days[0] if days else None, "to": days[-1] if days else None,
        "currency": cfg.get("currency", "USD"),
        "energy_cost": energy_cost,
        "tiers": tier_meta,
        "facts": facts,
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
