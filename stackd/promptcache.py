"""Synthetic commercial-prefix-cache estimator.

The savings ledger's ``cached_tokens`` is a model of what an Anthropic/OpenAI
prefix cache WOULD have discounted for a given traffic pattern — engine
independent. It is deliberately NOT the local backend's own cache reuse: that
varies wildly by engine (llama.cpp slot reuse vs vLLM APC vs SGLang radix vs
nothing on a hybrid) and isn't what the ROI comparison is about. A frontier API
prefix-caches on the request prefix regardless of the local engine, so that is
what we estimate here.

Method (matches the original llama-priority-proxy ``_measure_prompt_cache``):
break each request into the units a commercial cache matches at — the system
prompt, the tools block, then each message in order — hash them, and for a new
request find the longest byte-identical leading run against a short rolling
history of recent requests under the same key, within a TTL window. The cached
token count is prompt_tokens scaled by the matched fraction, gated by a minimum
prefix length.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque


def pc_units(body: dict) -> list[tuple[bytes, int]]:
    """Ordered ``[(sha1, char_len)]`` at the granularity a commercial prefix
    cache matches: the system prompt, then the tools block, then each chat
    message in order (OpenAI puts the system turn in ``messages[0]``; the
    ``system``/``tools`` top-level keys cover Anthropic-style bodies too)."""
    units: list[tuple[bytes, int]] = []

    def add(obj) -> None:
        s = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        units.append((hashlib.sha1(s.encode("utf-8", "replace")).digest(), len(s)))

    if not isinstance(body, dict):
        return units
    if body.get("system"):
        add({"s": body["system"]})
    if body.get("tools"):
        add({"t": body["tools"]})
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            add(m)
    elif body.get("prompt") is not None:            # legacy /v1/completions
        add({"p": body["prompt"]})
    return units


def _prefix_chars(cur, prev) -> int:
    """Cumulative char length of the leading units that are byte-identical."""
    chars = 0
    for a, b in zip(cur, prev):
        if a[0] != b[0]:
            break
        chars += a[1]
    return chars


class PromptCacheModel:
    """Rolling per-key request history -> longest TTL-windowed prefix match.

    ``cfg`` is ``pricing.json``'s ``prompt_cache`` block; an absent/empty block
    disables the model (``measure`` returns 0), which is also the safe default
    for tests and deployments that never configured it.
    """

    def __init__(self, cfg: dict | None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg)
        self.ttl_s = float(cfg.get("ttl_s", 300) or 0)
        self.min_prefix_tokens = int(cfg.get("min_prefix_tokens", 1024) or 0)
        self.history_per_key = max(2, int(cfg.get("history_per_model", 24) or 24))
        self._hist: dict[str, deque] = {}

    def measure(self, key: str, units, prompt_tokens: int, *, now: float | None = None) -> int:
        """Tokens a commercial prefix cache would have served from cache for this
        request, and record it so the NEXT request under ``key`` can match it."""
        if not self.enabled or not prompt_tokens or not units:
            return 0
        now = time.time() if now is None else now
        h = self._hist.setdefault(key, deque(maxlen=self.history_per_key))
        total = sum(cl for _, cl in units) or 1
        best = 0
        for prev_units, prev_ts in h:
            if self.ttl_s and now - prev_ts > self.ttl_s:
                continue
            m = _prefix_chars(units, prev_units)
            if m > best:
                best = m
        h.append((units, now))
        cached = min(int(prompt_tokens), round(prompt_tokens * best / total))
        return cached if cached >= self.min_prefix_tokens else 0
