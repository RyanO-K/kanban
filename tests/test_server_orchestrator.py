import json
import os
import sys
import threading
import http.client

import pytest

import kanban_server as ks


@pytest.fixture
def server(kanban, monkeypatch):
    # Point the server at the temp kanban dir.
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    import orchestrator_core as oc
    monkeypatch.setattr(oc, "KANBAN_DIR", kanban, raising=False)
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(data) if data else None)


def test_put_then_get_profile(server):
    status, _ = _req(server, "PUT", "/api/profiles/frontend",
                     {"whenToUse": "UI work", "model": "claude-opus-4-8"})
    assert status == 200
    status, body = _req(server, "GET", "/api/profiles/frontend")
    assert status == 200
    assert body["whenToUse"] == "UI work"
    assert body["name"] == "frontend"


def test_list_profiles(server):
    _req(server, "PUT", "/api/profiles/a", {"whenToUse": "x"})
    _req(server, "PUT", "/api/profiles/b", {"whenToUse": "y"})
    status, body = _req(server, "GET", "/api/profiles")
    assert status == 200
    assert {p["name"] for p in body["profiles"]} == {"a", "b"}


def test_delete_profile(server):
    _req(server, "PUT", "/api/profiles/temp", {"whenToUse": "x"})
    status, _ = _req(server, "DELETE", "/api/profiles/temp")
    assert status == 200
    status, _ = _req(server, "GET", "/api/profiles/temp")
    assert status == 404


# --- Task 6: orchestrator state ---

def test_get_default_state(server):
    status, body = _req(server, "GET", "/api/orchestrator/state")
    assert status == 200
    assert body == {"enabled": False, "concurrencyCap": 3,
                    "stopAllRequested": False, "idleSeconds": 600,
                    "tickSeconds": 60, "maxAgentSeconds": 0, "triageTimeoutSeconds": 120,
                    "triageModel": "claude-opus-4-8",
                    "summarizerModel": "claude-opus-4-8",
                    "autoCommit": True, "autoPush": True}


def test_state_surfaces_usage_pause(server, kanban):
    """Ticket #60: when the orchestrator is parked by a usage limit, the live
    status endpoint reports it (pausedUntil + remainingSeconds) so the UI can show
    'usage limited'. Absent when there is no pause (covered by default-state test)."""
    import time
    import orchestrator_core as oc
    oc.set_usage_pause(kanban, reset_at=time.time() + 1200,
                       now_ts=time.time(), reason="ticket 9 hit a usage limit")
    status, body = _req(server, "GET", "/api/orchestrator/state")
    assert status == 200
    pause = body.get("usagePause")
    assert pause is not None, "active usage pause must be surfaced on the state"
    assert pause["pausedUntil"] > 0
    assert pause["remainingSeconds"] > 0
    assert pause["reason"] == "ticket 9 hit a usage limit"


def test_put_state_merges(server):
    status, body = _req(server, "PUT", "/api/orchestrator/state", {"enabled": True})
    assert status == 200
    assert body["enabled"] is True
    assert body["concurrencyCap"] == 3
    # Persisted.
    status, body2 = _req(server, "GET", "/api/orchestrator/state")
    assert body2["enabled"] is True


# --- Server restart endpoint ---

def test_server_restart_schedules_reexec(server, monkeypatch):
    calls = {"exec": 0, "spawn": 0, "exit": 0, "stop": 0, "timers": []}

    # Stub every path the re-exec could take so running fn() never actually
    # replaces/kills the test process, on either platform.
    monkeypatch.setattr(ks.os, "execv",
                        lambda *a: calls.__setitem__("exec", calls["exec"] + 1))
    monkeypatch.setattr(ks.subprocess, "Popen",
                        lambda *a, **k: calls.__setitem__("spawn", calls["spawn"] + 1))
    monkeypatch.setattr(ks.os, "_exit",
                        lambda *a: calls.__setitem__("exit", calls["exit"] + 1))
    monkeypatch.setattr(ks, "stop_orchestrator",
                        lambda: calls.__setitem__("stop", calls["stop"] + 1))

    real_timer = ks.threading.Timer

    def fake_timer(delay, fn, *a, **k):
        calls["timers"].append((delay, fn))
        return real_timer(delay, fn, *a, **k)

    monkeypatch.setattr(ks.threading, "Timer", fake_timer)

    status, body = _req(server, "POST", "/api/server/restart")
    assert status == 200
    assert body == {"ok": True}
    assert calls["stop"] == 1            # lock released before re-exec
    assert len(calls["timers"]) == 1     # re-exec scheduled on a Timer
    _, fn = calls["timers"][0]
    fn()
    if sys.platform == "win32":
        # Windows can't os.execv a spaced interpreter path — spawn + exit instead.
        assert calls["spawn"] == 1 and calls["exit"] == 1
        assert calls["exec"] == 0
    else:
        assert calls["exec"] == 1
        assert calls["spawn"] == 0 and calls["exit"] == 0


def test_put_state_persists_idle_seconds(server):
    status, body = _req(server, "PUT", "/api/orchestrator/state", {"idleSeconds": 300})
    assert status == 200
    assert body["idleSeconds"] == 300
    status, body2 = _req(server, "GET", "/api/orchestrator/state")
    assert body2["idleSeconds"] == 300


# --- Task 7: activity, kill, answer ---

import orchestrator_core as oc2


def test_activity_feed(server, kanban):
    oc2.append_activity(kanban, {"ts": oc2.now_iso(), "kind": "dispatch", "ticket": "1"})
    status, body = _req(server, "GET", "/api/orchestrator/activity")
    assert status == 200
    assert body["entries"][-1]["kind"] == "dispatch"


def test_kill_queues_when_unreachable(server, kanban, monkeypatch):
    # Put an in-flight marker on ticket 1 with a bogus pid.
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["orchestrator"] = {"state": "dispatched", "pid": 999999, "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    import orchestrator as orch
    monkeypatch.setattr(orch, "_process_alive", lambda pid: False)
    status, body = _req(server, "POST", "/api/orchestrator/kill/demo/1")
    assert status == 200
    assert body["queued"] is True
    with open(p, "r", encoding="utf-8") as f:
        assert json.load(f)["orchestrator"]["killRequested"] is True


def test_answer_written(server, kanban):
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "blocked"
    t["orchestrator"] = {"state": "blocked",
                         "question": {"id": "q1", "type": "input", "prompt": "?",
                                      "answer": None, "answeredAt": None}}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    status, _ = _req(server, "POST", "/api/orchestrator/answer/demo/1",
                     {"value": "yes", "notes": "go ahead"})
    assert status == 200
    with open(p, "r", encoding="utf-8") as f:
        q = json.load(f)["orchestrator"]["question"]
    assert q["answer"] == {"value": "yes", "notes": "go ahead"}


# --- C1: narrow atomic write tests ---

def test_answer_preserves_comments_and_no_tmp_lingering(server, kanban):
    """orch_answer must:
    1. Preserve a pre-existing comments list (a concurrent sub-agent wrote it).
    2. Not leave a .tmp file behind (atomic write via os.replace).

    RED signal: if answer re-reads then atomic-writes only the narrow
    orchestrator.question field, comments survive.  If it reads once at
    request start and clobbers, comments may be lost and a .tmp may linger.
    """
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "blocked"
    t["orchestrator"] = {
        "state": "blocked",
        "question": {"id": "q1", "type": "input", "prompt": "colour?",
                     "answer": None, "answeredAt": None},
    }
    t["comments"] = [{"writer": "SubAgent", "message": "I added this while blocked",
                      "timestamp": "2026-01-01T00:00:00+00:00"}]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    status, _ = _req(server, "POST", "/api/orchestrator/answer/demo/1",
                     {"value": "blue", "notes": "use brand blue"})
    assert status == 200

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)

    # The answer must be recorded.
    q = saved["orchestrator"]["question"]
    assert q["answer"] == {"value": "blue", "notes": "use brand blue"}, (
        "Answer was not written"
    )

    # Comments added by a concurrent sub-agent must survive.
    assert saved.get("comments"), "comments were lost after orch_answer (clobber bug)"
    assert saved["comments"][0]["writer"] == "SubAgent", (
        "Sub-agent comment was clobbered by orch_answer"
    )

    # No lingering .tmp file.
    tmp_path = p + ".tmp"
    assert not os.path.exists(tmp_path), (
        f".tmp file still present after orch_answer — write was not atomic: {tmp_path}"
    )


def test_direct_kill_does_not_rewrite_file(server, kanban, monkeypatch):
    """When orch_kill kills directly (process is alive), it must NOT rewrite
    the ticket file — rewriting risks clobbering concurrent sub-agent writes.

    RED: current code always rewrites, so mtime will change even on direct kill.
    """
    import orchestrator as orch
    import time as _time

    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["orchestrator"] = {"state": "dispatched", "pid": 54321, "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    # Record mtime AFTER writing — so any re-write would produce a newer mtime.
    _time.sleep(0.05)  # small pause so OS mtime granularity doesn't mask a rewrite
    mtime_before = os.path.getmtime(p)

    # Monkeypatch: process is alive → direct kill path.
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    killed = []
    monkeypatch.setattr(orch, "kill_pid", lambda pid: killed.append(pid) or True)

    status, body = _req(server, "POST", "/api/orchestrator/kill/demo/1")
    assert status == 200
    assert body["queued"] is False, "Expected direct kill (queued=False) for alive process"
    assert killed == [54321], "kill_pid should have been called"

    mtime_after = os.path.getmtime(p)
    assert mtime_after == mtime_before, (
        f"File was rewritten on direct kill (mtime changed: {mtime_before} -> {mtime_after}). "
        "Direct kill should NOT touch the file."
    )
