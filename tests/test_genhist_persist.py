"""The dashboard's 10-minute request averages (GENHIST) used to exist only in one
browser tab's memory, so a reload — or a daemon restart — blanked every `avg` to
"–" while the sticky "last" number beside it survived (last_gen/<stack> in meta).
The series behind the average now lives in meta too: one append per completed
request, pruned to the window the corner averages, replayed to any tab via /live."""
import os
import tempfile
import threading
import time

from stackd import serve
from stackd.store import Store

BUF = (b'data: {"id":1,"timings":{"predicted_per_second":123.4,'
       b'"prompt_per_second":5678.0,"prompt_n":100,"predicted_n":50,'
       b'"draft_n":10,"draft_n_accepted":8}}\n\n')


def _store() -> Store:
    return Store.open(os.path.join(tempfile.mkdtemp(), "t.db"))


def _reset(*stacks):
    for s in stacks:
        serve._GENHIST.pop(s, None)
        serve._GENHIST_SEEDED.discard(s)
        serve._LAST_GEN.pop(s, None)


def test_capture_gen_appends_the_ring_and_it_survives_a_restart():
    st, lock = _store(), threading.Lock()
    _reset("chat")
    serve._capture_gen("chat", BUF, st, lock)
    ring = serve._meta_get(st, "genhist/chat", lock)
    assert ring and ring[-1]["gen"] == 123.4 and ring[-1]["prompt"] == 5678.0
    assert ring[-1]["mtp"] == 80.0
    # the daemon restarts: memory gone, store survives
    _reset("chat")
    replayed = serve._genhist_seed(st, ["chat"], lock)
    assert len(replayed["chat"]) == 1 and replayed["chat"][-1]["gen"] == 123.4


def test_point_timestamp_matches_the_sticky_hint_the_page_dedupes_on():
    st, lock = _store(), threading.Lock()
    _reset("chat")
    serve._capture_gen("chat", BUF, st, lock)
    ring = serve._meta_get(st, "genhist/chat", lock)
    # The client dedupes the replay against /engine's `last_request.at` — which is
    # this same completion. If the two timestamps ever drift apart, every seeded
    # point reads as a new request and the averages double-count.
    assert ring[-1]["t"] == serve._LAST_GEN["chat"]["at"]


def test_seed_replays_only_the_window_the_corner_averages():
    st, lock = _store(), threading.Lock()
    now = time.time()
    serve._meta_set(st, "genhist/vision",
                    [{"t": now - 4000, "gen": 1.0}, {"t": now - 1200, "gen": 2.0},
                     {"t": now - 60, "gen": 3.0}], lock)
    _reset("vision")
    out = serve._genhist_seed(st, ["vision"], lock)
    assert [p["gen"] for p in out["vision"]] == [3.0]        # window is _GENHIST_WINDOW_S


def test_seed_is_once_per_stack_and_read_only():
    st, lock = _store(), threading.Lock()
    serve._meta_set(st, "genhist/vision", [{"t": time.time(), "gen": 9.0}], lock)
    _reset("vision")
    assert serve._genhist_seed(st, ["vision"], lock)["vision"][-1]["gen"] == 9.0
    # new traffic in meta is NOT re-read once the ring is live: the append path owns
    # it now, and re-reading would resurrect points the window already dropped
    serve._meta_set(st, "genhist/vision", [{"t": time.time(), "gen": 1.0}], lock)
    assert serve._genhist_seed(st, ["vision"], lock)["vision"][-1]["gen"] == 9.0
    assert "vision" not in serve._LAST_GEN                   # replay never fakes a "last"


def test_ring_is_capped_and_keeps_the_newest():
    now = time.time()
    pts = [{"t": now - i, "gen": float(i)} for i in range(400)]      # i=0 is the newest
    ring = serve._genhist_prune(pts)
    assert len(ring) == serve._GENHIST_MAX_PTS
    # oldest first: the client appends live points to the tail and averages by
    # timestamp, so a ring shipped out of order would put a stale value last
    assert [p["t"] for p in ring] == sorted(p["t"] for p in ring)
    assert ring[-1]["t"] == now                                # the latest request survives
    assert ring[0]["t"] > now - serve._GENHIST_WINDOW_S         # trimmed ones are the oldest
    assert ring[0]["gen"] == float(serve._GENHIST_MAX_PTS - 1)


def test_live_buffer_backs_the_corner_for_engines_without_timings():
    """vLLM/SGLang never produce a `timings` block, so their ring stays empty —
    their numbers always came from the dashboard's active-poll fallback. The
    daemon derives the same series from its own 1 s live buffer."""
    now = time.time()
    saved = list(serve._LIVE_BUF)
    try:
        serve._LIVE_BUF.clear()
        for k in range(60):                      # oldest first, 1 s apart
            serve._LIVE_BUF.append({"ts": now - (59 - k),
                                    "eng": {"chat": {"active": True,
                                                     "gen_tok_s": 30.0 + k,
                                                     "prompt_tok_s": 900.0,
                                                     "mtp_accept_pct": None}}})
        out = serve._genhist_from_live(["chat"], {"chat": "vllm-cuda"}, {})
        assert len(out["chat"]) == 12                 # stepped at 5 s, not one per sample
        assert [p["t"] for p in out["chat"]] == sorted(p["t"] for p in out["chat"])
        assert out["chat"][0]["gen"] == 30.0 and out["chat"][0]["prompt"] == 900.0
        # a stack that reports real per-request timings keeps its exact ring
        assert serve._genhist_from_live(["chat"], {"chat": "vllm-cuda"},
                                        {"chat": [{"t": now, "gen": 1.0}]}) == {}
        assert serve._genhist_from_live(["chat"], {"chat": "llamacpp-cuda"}, {}) == {}
        # THE idle trap: an engine keeps its session average on the gauges after
        # the request finishes. Sampling those would hand the corner brand-new
        # points forever, so its idle cutoff never fires and the average reads
        # warm long after the traffic stopped.
        serve._LIVE_BUF.clear()
        for k in range(30):
            serve._LIVE_BUF.append({"ts": now - k,
                                    "eng": {"chat": {"active": False, "gen_tok_s": 163.0,
                                                     "prompt_tok_s": 30209.7,
                                                     "mtp_accept_pct": 35.0}}})
        assert serve._genhist_from_live(["chat"], {"chat": "vllm-cuda"}, {}) == {}
    finally:
        serve._LIVE_BUF.clear()
        serve._LIVE_BUF.extend(saved)


def test_prune_and_seed_tolerate_garbage():
    now = time.time()
    assert serve._genhist_prune([None, {"gen": 5}, {"t": "x"}, {"t": now, "gen": 1.0}], now) \
        == [{"t": now, "gen": 1.0}]
    # no store at all (ledger-less config) must not raise
    _reset("chat")
    assert serve._genhist_seed(None, ["chat"], threading.Lock()) == {}
    serve._genhist_append("chat", {"t": now, "gen": 1.0})     # store=None: memory only
    assert serve._GENHIST["chat"][-1]["gen"] == 1.0
