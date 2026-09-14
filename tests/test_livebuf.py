"""The daemon's 1 s live ring buffer trims each source to just the numbers the
dashboard's live charts draw, so the buffer and the /live replays stay small and
a backfilled point feeds feedPoint() with the same shape as a live one. One
non-chart field is kept on purpose: engine `active`, which is what tells
/live's history derivation ("was a request generating?") from "what did the
engine's session gauges stick at after it finished"."""
from stackd.serve import _live_trim


def test_trim_keeps_only_chart_fields():
    gpu = {"cuda0": {"util_pct": 90.0, "power_w": 570.8, "vram_used_gib": 80.7,
                     "temp_c": 77, "fan_pct": 0},
           "igpu0": {"util_pct": 0.0, "power_w": 39.1, "vram_used_gib": 23.1}}
    host = {"cpu_pct": 7.4, "loadavg": [1.92, 1.93, 2.56], "mem_total_gib": 250}
    engs = {"chat": {"gen_tok_s": 150.2, "prompt_tok_s": 3340.0, "active": True,
                     "mtp_accept_pct": 80.8, "kv_pct": 12.5, "ctx_tokens": 12345,
                     "slots": [1, 2, 3]}}
    out = _live_trim(gpu, host, engs)
    assert out["gpu"]["cuda0"] == {"util_pct": 90.0, "power_w": 570.8,
                                   "vram_used_gib": 80.7}
    assert out["host"] == {"cpu_pct": 7.4, "load1": 1.92}
    # inflight defaults to [] with no `inflight` arg passed in — see
    # test_trim_carries_inflight below for the populated case.
    assert out["eng"]["chat"] == {"active": True, "gen_tok_s": 150.2, "prompt_tok_s": 3340.0,
                                  "mtp_accept_pct": 80.8, "kv_pct": 12.5,
                                  "ctx_tokens": 12345, "inflight": []}


def test_trim_tolerates_dead_sources():
    # A source that timed out or errored must degrade to nulls, not raise — the
    # dashboard reads nulls as "no fresh measurement" and applies its own
    # per-line rule (daemon lines hold, engine lines gap).
    out = _live_trim({}, {}, {})
    assert out["gpu"] == {"cuda0": None, "igpu0": None}
    assert out["host"] == {"cpu_pct": None, "load1": None}
    assert out["eng"] == {}


def test_trim_handles_error_shaped_sources():
    # _gpu_stats/_host_stats return an error dict on failure — device keys are
    # simply absent, which trims to None the same way.
    out = _live_trim({"cuda0": None, "igpu0": None, "error": "boom"},
                     {"error": "boom"}, {"chat": {"gen_tok_s": None}})
    assert out["gpu"] == {"cuda0": None, "igpu0": None}
    assert out["host"] == {"cpu_pct": None, "load1": None}
    # No `active` key at all is as falsy as active=False, so the idle 0/100
    # defaults apply here too — gen_tok_s/kv_pct/ctx_tokens are 0 (a real
    # fact: nothing is running) rather than None (a missing measurement).
    # `active` itself is passed through as-is (None, not coerced to False).
    assert out["eng"] == {"chat": {"active": None, "gen_tok_s": 0, "prompt_tok_s": None,
                                   "mtp_accept_pct": 100, "kv_pct": 0,
                                   "ctx_tokens": 0, "inflight": []}}


def test_trim_carries_inflight():
    # The per-slot stacked chart replays history from /live, so each engine's
    # inflight snapshot at sample time has to ride along inside _live_trim's
    # output, not just the instantaneous /engine response.
    inflight = {"chat": [{"slot": 0, "ctx_tokens": 512}]}
    out = _live_trim({}, {}, {"chat": {"active": True, "gen_tok_s": 10.0}}, inflight)
    assert out["eng"]["chat"]["inflight"] == [{"slot": 0, "ctx_tokens": 512}]