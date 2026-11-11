"""Prefill speed counts only prompt tokens the engine computed: prefix-cache hits restored
from a cache tier made SGLang report tens of thousands of tok/s (owner, 2026-11-11). Days
recorded on the old basis can't be split after the fact, so their prefill totals are zeroed
once; decode and context stay."""
from stackd.store import Store
from stackd.telemetry import _computed_prompt_tokens

SG = """# TYPE sglang:realtime_tokens_total counter
sglang:realtime_tokens_total{mode="prefill_compute",model_name="m"} 1200
sglang:realtime_tokens_total{mode="prefill_cache",model_name="m"} 98000
sglang:realtime_tokens_total{mode="decode",model_name="m"} 5000
sglang:prompt_tokens_total{model_name="m"} 99200
sglang:cached_tokens_total{model_name="m"} 98000
"""


def test_sglang_counts_compute_only():
    assert _computed_prompt_tokens(SG, "sglang") == 1200


def test_sglang_fallback_subtracts_cached():
    t = 'sglang:prompt_tokens_total{model_name="m"} 99200\nsglang:cached_tokens_total{model_name="m"} 98000\n'
    assert _computed_prompt_tokens(t, "sglang") == 1200


def test_sglang_fallback_without_cache_counter():
    assert _computed_prompt_tokens('sglang:prompt_tokens_total{model_name="m"} 500\n', "sglang") == 500


def test_vllm_prefers_kv_computed_sum():
    t = ('vllm:request_prefill_kv_computed_tokens_sum{model_name="m"} 300\n'
         'vllm:prompt_tokens_total{model_name="m"} 9000\n')
    assert _computed_prompt_tokens(t, "vllm") == 300


def test_vllm_fallback_subtracts_prefix_hits():
    t = 'vllm:prompt_tokens_total{model_name="m"} 9000\nvllm:prefix_cache_hits_total{model_name="m"} 8700\n'
    assert _computed_prompt_tokens(t, "vllm") == 300


def test_unknown_engine_and_no_counters():
    assert _computed_prompt_tokens("", "sglang") is None
    assert _computed_prompt_tokens(SG, "llamacpp") is None


def test_old_prefill_rows_zeroed_once(tmp_path):
    db = str(tmp_path / "s.db")
    s = Store.open(db)
    s.add_engine_sample(served_stack="coding", served_model="m", served_profile="p",
                        decode_tok=1000, decode_ms=20000, prefill_tok=99000, prefill_ms=2000)
    # pretend this DB predates v11: no basis flag yet
    s.conn.execute("DELETE FROM meta WHERE key='prefill_basis'")
    s.close()
    s = Store.open(db)
    r = s.conn.execute("SELECT decode_tok, decode_ms, prefill_tok, prefill_ms FROM engine_daily").fetchone()
    assert (r["decode_tok"], r["decode_ms"], r["prefill_tok"], r["prefill_ms"]) == (1000, 20000, 0, 0)
    # new samples after the wipe are kept, and reopening doesn't wipe again
    s.add_engine_sample(served_stack="coding", served_model="m", served_profile="p",
                        prefill_tok=1200, prefill_ms=2000)
    s.close()
    s = Store.open(db)
    r = s.conn.execute("SELECT prefill_tok, prefill_ms FROM engine_daily").fetchone()
    assert (r["prefill_tok"], r["prefill_ms"]) == (1200, 2000)
