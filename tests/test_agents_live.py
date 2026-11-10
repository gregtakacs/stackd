"""Live agent sessions on the dashboard (POST /agents/progress, GET /agents/live).

An agent harness (delegate) posts compact progress snapshots with its own inference key
while a worker round runs; the dashboard reads them with an admin key. Display-only,
in-memory, keyed by (reporting key, task), expiring on its own."""
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_api_keys import ADMIN, _Srv  # noqa: E402


def snap(task="repo-feat", **kw):
    b = {"task": task, "round": 1, "repo": "/r", "branch": "delegate/feat", "model": "m",
         "phase": "worker", "started_at": time.time() - 30, "steps": 3, "tool_calls": 4,
         "tool_errors": 0, "output_tokens": 900, "context_tokens": 12000,
         "todos": [{"content": "write tests", "status": "in_progress"}],
         "recent": [{"t": time.time(), "kind": "bash", "text": "npm test", "ok": True}]}
    b.update(kw)
    return b


def test_inference_key_reports_admin_reads():
    with _Srv() as s:
        k = s.mint("delegate-x")
        assert s.req("POST", "/agents/progress", k, snap())[0] == 200
        assert s.req("GET", "/agents/live", k)[0] in (401, 403)     # an agent cannot read the board
        code, body = s.req("GET", "/agents/live", ADMIN)
        assert code == 200 and len(body["sessions"]) == 1
        r = body["sessions"][0]
        assert r["task"] == "repo-feat" and r["reporter"] == "delegate-x" and r["todos"][0]["status"] == "in_progress"


def test_other_scopes_and_anonymous_refused():
    with _Srv() as s:
        b = s.mint("sx", scope="brave")
        m = s.mint("mx", scope="mint")
        assert s.req("POST", "/agents/progress", None, snap())[0] == 401
        assert s.req("POST", "/agents/progress", b, snap())[0] == 401
        assert s.req("POST", "/agents/progress", m, snap())[0] == 401
        assert s.req("GET", "/agents/live", ADMIN)[1]["sessions"] == []


def test_updates_replace_and_keys_do_not_collide():
    with _Srv() as s:
        a, b = s.mint("laptop"), s.mint("docker-boy")
        s.req("POST", "/agents/progress", a, snap(steps=1))
        s.req("POST", "/agents/progress", a, snap(steps=9))
        s.req("POST", "/agents/progress", b, snap(steps=2))         # same task name, other key
        rows = s.req("GET", "/agents/live", ADMIN)[1]["sessions"]
        assert sorted((r["reporter"], r["steps"]) for r in rows) == [("docker-boy", 2), ("laptop", 9)]


def test_sanitized_and_clipped():
    with _Srv() as s:
        k = s.mint("noisy")
        big = snap(task="t" * 500, recent=[{"t": 1, "kind": "bash", "text": "x" * 2000}] * 60,
                   todos=[{"content": "c" * 999, "status": "pending"}] * 50, steps="nine",
                   evil={"nested": "ignored"})
        assert s.req("POST", "/agents/progress", k, big)[0] == 200
        r = s.req("GET", "/agents/live", ADMIN)[1]["sessions"][0]
        assert len(r["task"]) == 120 and len(r["recent"]) == 30 and len(r["recent"][0]["text"]) == 300
        assert len(r["todos"]) == 30 and len(r["todos"][0]["content"]) == 200
        assert r["steps"] is None and "evil" not in r
        assert s.req("POST", "/agents/progress", k, {"round": 1})[0] == 400     # no task
        huge = snap(recent=[{"t": 1, "kind": "bash", "text": "x" * 9000}] * 100)
        assert s.req("POST", "/agents/progress", k, huge)[0] == 413            # > 768 KB


def test_expiry_finished_and_stale():
    with _Srv() as s:
        k = s.mint("ttl")
        s.req("POST", "/agents/progress", k, snap(task="done", final=True, status="done"))
        s.req("POST", "/agents/progress", k, snap(task="dead"))
        s.req("POST", "/agents/progress", k, snap(task="alive"))
        H = s.handler
        with H._agents_lock:
            H._agents[("ttl", "done")]["updated_at"] -= H.AGENT_LINGER_S + 1
            H._agents[("ttl", "dead")]["updated_at"] -= H.AGENT_STALE_S + 1
            H._agents[("ttl", "alive")]["updated_at"] -= H.AGENT_STALE_S - 10    # running, heard from recently
        rows = s.req("GET", "/agents/live", ADMIN)[1]["sessions"]
        assert [r["task"] for r in rows] == ["alive"]


def test_session_cap():
    with _Srv() as s:
        k = s.mint("flood")
        H = s.handler
        for i in range(H.AGENT_MAX):
            assert s.req("POST", "/agents/progress", k, snap(task=f"t{i}"))[0] == 200
        assert s.req("POST", "/agents/progress", k, snap(task="one-too-many"))[0] == 429
        assert s.req("POST", "/agents/progress", k, snap(task="t0", steps=5))[0] == 200   # updates still fine



def test_claude_agents_kind_and_longer_quiet_window():
    with _Srv() as s:
        k = s.mint("claude-agents")
        s.req("POST", "/agents/progress", k, snap(task="G1 agent", kind="claude", steps=None, round=None))
        s.req("POST", "/agents/progress", k, snap(task="worker", kind="bogus"))
        H = s.handler
        rows = {r["task"]: r for r in s.req("GET", "/agents/live", ADMIN)[1]["sessions"]}
        assert rows["G1 agent"]["kind"] == "claude" and rows["worker"]["kind"] == "worker"
        with H._agents_lock:
            for r in H._agents.values():
                r["updated_at"] -= H.AGENT_STALE_S + 60          # past a worker's window
        rows = [r["task"] for r in s.req("GET", "/agents/live", ADMIN)[1]["sessions"]]
        assert rows == ["G1 agent"]



def test_waiting_claude_agent_outlives_the_working_window():
    with _Srv() as s:
        k = s.mint("claude-agents")
        s.req("POST", "/agents/progress", k, snap(task="W4 lead", kind="claude", phase="waiting", round=None))
        s.req("POST", "/agents/progress", k, snap(task="W5 lead", kind="claude", phase="working", round=None))
        H = s.handler
        with H._agents_lock:
            for r in H._agents.values():
                r["updated_at"] -= H.AGENT_STALE_CLAUDE_S + 60     # quiet past the working window
        rows = {r["task"]: r for r in s.req("GET", "/agents/live", ADMIN)[1]["sessions"]}
        assert list(rows) == ["W4 lead"] and rows["W4 lead"]["final"] is False   # still live, no dismiss


def test_prompt_and_report_kept_beside_the_card():
    with _Srv() as s:
        k = s.mint("delegate-docs")
        t0 = time.time() - 30
        code, r = s.req("POST", "/agents/progress", k, snap(started_at=t0))
        assert code == 200 and r["need_prompt"] is True                    # nothing sent yet: ask for it
        prompt = "P" * 150_000
        r = s.req("POST", "/agents/progress", k, snap(started_at=t0, prompt=prompt))[1]
        assert r["need_prompt"] is False
        s.req("POST", "/agents/progress", k, snap(started_at=t0, steps=7))     # a light heartbeat keeps it
        row = s.req("GET", "/agents/live", ADMIN)[1]["sessions"][0]
        assert row["has_prompt"] is True and row["has_report"] is False
        assert "prompt" not in row and "report" not in row                  # the poll stays light
        q = "/agents/doc?reporter=delegate-docs&task=repo-feat&which="
        code, d = s.req("GET", q + "prompt", ADMIN)
        assert code == 200 and d["text"] == prompt
        assert s.req("GET", q + "report", ADMIN)[0] == 404
        assert s.req("GET", q + "prompt", k)[0] in (401, 403)               # an agent cannot read texts
        assert s.req("GET", q + "secrets", ADMIN)[0] == 400
        s.req("POST", "/agents/progress", k, snap(started_at=t0, final=True, status="done", report="R" * 300_000))
        d = s.req("GET", q + "report", ADMIN)[1]
        assert d["text"] == "R" * 200_000                                   # capped
        assert s.req("GET", q + "prompt", ADMIN)[1]["text"] == prompt       # still there at the end
        # the next round of the same task starts without the old texts
        r = s.req("POST", "/agents/progress", k, snap(started_at=time.time(), round=2))[1]
        assert r["need_prompt"] is True and s.req("GET", q + "report", ADMIN)[0] == 404


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)


def test_mode_kept_and_sanitized():
    with _Srv() as s:
        k = s.mint("delegate-m")
        s.req("POST", "/agents/progress", k, snap(task="a", mode={
            "runner": "docker", "here": False, "raw": True, "egress": "filtered", "orb": "",
            "service": "postgres", "chain": "h1" * 100, "evil": 1}))
        s.req("POST", "/agents/progress", k, snap(task="b", mode={"here": "yes", "runner": ["x"]}))
        s.req("POST", "/agents/progress", k, snap(task="c"))
        rows = {r["task"]: r for r in s.req("GET", "/agents/live", ADMIN)[1]["sessions"]}
        a = rows["a"]["mode"]
        assert a["runner"] == "docker" and a["raw"] is True and a["here"] is False
        assert a["service"] == "postgres" and len(a["chain"]) == 120 and "evil" not in a and a["orb"] is None
        assert rows["b"]["mode"]["here"] is False and rows["b"]["mode"]["runner"] is None
        assert rows["c"]["mode"] is None
