"""The live tok/s rates must not conflate request phases: the slot's context
position advances during BOTH prefill and decode, so the generation rate has
to come from a decode-only signal (n_decoded) and the prompt rate from a
prefill-only signal (n_prompt_tokens_processed). Before this, context loading
showed up as generation tok/s."""
from stackd.telemetry import _slot_decoded, _wall_rate, _win_rate


def test_slot_decoded_top_level():
    assert _slot_decoded({"n_decoded": 42}) == 42


def test_slot_decoded_nested_next_token():
    # newer llama.cpp builds nest the counter in the slot's next_token list
    s = {"next_token": [{"n_decoded": 39, "n_remain": 41}]}
    assert _slot_decoded(s) == 39


def test_slot_decoded_absent():
    assert _slot_decoded({"is_processing": True}) == 0
    assert _slot_decoded({"next_token": []}) == 0


def test_wall_rate_basic():
    a = {"ts": 0.0, "dec": 100.0}
    b = {"ts": 2.0, "dec": 300.0}
    assert _wall_rate(a, b, "dec") == 100.0


def test_wall_rate_needs_a_window():
    a = {"ts": 0.0, "dec": 100.0}
    b = {"ts": 0.4, "dec": 300.0}
    assert _wall_rate(a, b, "dec") is None


def test_wall_rate_flat_is_none():
    # during prefill the decode counter is flat: no live generation rate
    a = {"ts": 0.0, "dec": 0.0}
    b = {"ts": 2.0, "dec": 0.0}
    assert _wall_rate(a, b, "dec") is None


def test_wall_rate_regression_is_none():
    # a request boundary inside the window resets the per-slot counters
    a = {"ts": 0.0, "dec": 500.0}
    b = {"ts": 2.0, "dec": 10.0}
    assert _wall_rate(a, b, "dec") is None


def test_win_rate_prefill_counters():
    a = {"prompt_tok": 1000.0, "prompt_s": 10.0}
    b = {"prompt_tok": 16000.0, "prompt_s": 130.0}
    assert _win_rate(a, b, "prompt_tok", "prompt_s") == 125.0
