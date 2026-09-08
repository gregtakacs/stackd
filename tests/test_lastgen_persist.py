"""The "last session" numbers on the engine cards used to live only in RAM
(_LAST_GEN + telemetry._eng_state["last_sess"]), so every daemon restart blanked
the idle cards to "–" until the next generation. They are now mirrored to the
store's meta table and rehydrated on the first /engine poll after a boot."""
import os
import tempfile
import threading

from stackd import serve, telemetry
from stackd.store import Store

BUF = (b'data: {"id":1,"timings":{"predicted_per_second":123.4,'
       b'"prompt_per_second":5678.0,"prompt_n":100,"predicted_n":50,'
       b'"draft_n":10,"draft_n_accepted":8}}\n\n')


def _store() -> Store:
    return Store.open(os.path.join(tempfile.mkdtemp(), "t.db"))


def test_capture_gen_persists_across_restart():
    st, lock = _store(), threading.Lock()
    serve._LAST_GEN.pop("chat", None)
    serve._capture_gen("chat", BUF, st, lock)
    assert serve._LAST_GEN["chat"]["gen_tok_s"] == 123.4
    # simulate the daemon restart: memory gone, store survives
    serve._LAST_GEN.pop("chat", None)
    hg = serve._meta_get(st, "last_gen/chat", lock)
    assert hg and hg["gen_tok_s"] == 123.4 and hg["prompt_tok_s"] == 5678.0
    assert hg["ctx_tokens"] == 150 and hg["mtp_accept_pct"] == 80.0


def test_capture_gen_without_store_still_works():
    serve._LAST_GEN.pop("chat", None)
    serve._capture_gen("chat", BUF)              # ledger-less config: must not raise
    assert serve._LAST_GEN["chat"]["gen_tok_s"] == 123.4


def test_capture_gen_persists_usage_ctx_for_engines_without_timings():
    st, lock = _store(), threading.Lock()
    serve._LAST_CTX.pop("vchunk", None)
    buf = b'data: {"usage":{"prompt_tokens":900,"completion_tokens":100}}\n\n'
    serve._capture_gen("vchunk", buf, st, lock)
    lc = serve._meta_get(st, "last_ctx/vchunk", lock)
    assert lc and lc["ctx_tokens"] == 1000


def test_seed_last_session_fills_empty_slot_but_never_clobbers():
    ep = "http://engine.test:9999"
    telemetry._eng_state.pop(ep, None)
    assert not telemetry.has_session(ep)
    telemetry.seed_last_session(ep, {"gen_tok_s": 100.0, "ctx_peak": 5000})
    assert telemetry.has_session(ep)
    telemetry.seed_last_session(ep, {"gen_tok_s": 7.0})   # real session wins
    assert telemetry._eng_state[ep]["last_sess"]["gen_tok_s"] == 100.0


def test_meta_helpers_tolerate_absent_store():
    assert serve._meta_get(None, "x") is None
    serve._meta_set(None, "x", {"a": 1})                   # no-op, no raise