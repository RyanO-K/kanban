"""Tests for ticket #44: UI-based session management.

Dragging a ticket to in_progress via PATCH /api/board/<slug>/task/<id>
should spawn a sub-agent session (just like the orchestrator would).
Dragging it out of in_progress should kill the session and write a summary.
"""
import json
import os
import threading
import http.client
import time

import pytest

import sys
KANBAN_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KANBAN_SRC)

import kanban_server as ks


@pytest.fixture
def server(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    import orchestrator_core as oc
    monkeypatch.setattr(oc, "KANBAN_DIR", kanban, raising=False)
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


@pytest.fixture
def board_with_profile(kanban):
    """Add a ready ticket and a profile to the temp kanban dir."""
    import orchestrator_core as oc
    board = os.path.join(kanban, "boards", "demo")
    # Move ticket 1 to ready so it can be dispatched.
    p = os.path.join(board, "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "ready"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    # Write a profile.
    oc.write_profile(kanban, {
        "name": "general",
        "whenToUse": "general work",
        "model": "claude-sonnet-4-6",
        "allowedTools": ["Read", "Edit"],
        "systemPrompt": "You are a general agent.",
    })
    return kanban


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(data) if data else None)


# ---------------------------------------------------------------------------
# Moving TO in_progress spawns a session
# ---------------------------------------------------------------------------

def test_drag_to_in_progress_spawns_agent(server, board_with_profile, monkeypatch):
    """PATCH column=in_progress on a ready ticket should spawn an agent."""
    spawned = []

    import orchestrator as orch
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **kw: spawned.append(a) or {
                            "state": "dispatched",
                            "profile": "general",
                            "model": "claude-sonnet-4-6",
                            "pid": 12345,
                            "sessionId": "test-session-id",
                            "dispatchedAt": "2026-06-29T12:00:00+00:00",
                            "killRequested": False,
                            "logFile": ".kanban/_orchestrator/runs/1-test.log",
                        })

    status, body = _req(server, "PATCH", "/api/board/demo/task/1",
                        {"column": "in_progress"})
    assert status == 200
    assert body.get("ok") is True
    assert len(spawned) == 1, "spawn_agent should have been called once"


def test_drag_to_in_progress_writes_marker_and_session(server, board_with_profile, monkeypatch):
    """After spawn, the ticket should have an orchestrator marker and claudeSessionId."""
    import orchestrator as orch
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **kw: {
                            "state": "dispatched",
                            "profile": "general",
                            "model": "claude-sonnet-4-6",
                            "pid": 12345,
                            "sessionId": "ui-session-xyz",
                            "dispatchedAt": "2026-06-29T12:00:00+00:00",
                            "killRequested": False,
                            "logFile": ".kanban/_orchestrator/runs/1-test.log",
                        })

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "in_progress"})

    p = os.path.join(board_with_profile, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)

    assert t.get("claudeSessionId") == "ui-session-xyz"
    assert t.get("orchestrator", {}).get("state") == "dispatched"
    assert t.get("orchestrator", {}).get("pid") == 12345


def test_drag_to_in_progress_no_profiles_still_moves(server, kanban, monkeypatch):
    """With no profiles, dragging to in_progress still moves the status (no spawn)."""
    # No profiles in kanban fixture.
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "ready"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    spawned = []
    import orchestrator as orch
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **kw: spawned.append(1) or {})

    status, body = _req(server, "PATCH", "/api/board/demo/task/1",
                        {"column": "in_progress"})
    assert status == 200
    assert body.get("ok") is True
    assert len(spawned) == 0, "spawn should not be called when there are no profiles"

    with open(p, "r", encoding="utf-8") as f:
        updated = json.load(f)
    assert updated["status"] == "in_progress"


def test_drag_to_in_progress_records_activity(server, board_with_profile, monkeypatch):
    """A UI-triggered dispatch should append a 'dispatch' entry to the activity feed."""
    import orchestrator as orch
    import orchestrator_core as oc

    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **kw: {
                            "state": "dispatched",
                            "profile": "general",
                            "model": "claude-sonnet-4-6",
                            "pid": 99999,
                            "sessionId": "s1",
                            "dispatchedAt": "2026-06-29T12:00:00+00:00",
                            "killRequested": False,
                            "logFile": ".kanban/_orchestrator/runs/1-test.log",
                        })

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "in_progress"})

    entries = oc.read_activity(board_with_profile)
    kinds = [e["kind"] for e in entries]
    assert "dispatch" in kinds, f"Expected 'dispatch' in activity feed, got: {kinds}"


# ---------------------------------------------------------------------------
# Moving OUT of in_progress kills the session and writes a summary
# ---------------------------------------------------------------------------

def test_drag_out_of_in_progress_kills_agent(server, kanban, monkeypatch):
    """PATCH to a non-in_progress column for an in-flight ticket should kill the agent."""
    # Set up an in-flight ticket.
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "in_progress"
    t["orchestrator"] = {
        "state": "dispatched",
        "pid": 77777,
        "killRequested": False,
        "logFile": ".kanban/_orchestrator/runs/1-test.log",
        "dispatchedAt": "2026-06-29T12:00:00+00:00",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    killed = []
    import orchestrator as orch
    monkeypatch.setattr(orch, "kill_pid", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    # Stub summarizer so no real subprocess runs.
    monkeypatch.setattr(ks, "_ui_summarize_progress",
                        lambda *a, **kw: "CHECKPOINT: did A\nNEXT STEPS: do B",
                        raising=False)

    status, body = _req(server, "PATCH", "/api/board/demo/task/1", {"column": "todo"})
    assert status == 200
    assert body.get("ok") is True
    assert 77777 in killed, "kill_pid should have been called for the in-flight PID"


def test_drag_out_of_in_progress_writes_summary_comment(server, kanban, monkeypatch):
    """Moving an in-flight ticket out of in_progress should leave a summary comment."""
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "in_progress"
    t["orchestrator"] = {
        "state": "dispatched",
        "pid": 88888,
        "killRequested": False,
        "logFile": ".kanban/_orchestrator/runs/1-test.log",
        "dispatchedAt": "2026-06-29T12:00:00+00:00",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    import orchestrator as orch
    monkeypatch.setattr(orch, "kill_pid", lambda pid: True)
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    monkeypatch.setattr(ks, "_ui_summarize_progress",
                        lambda *a, **kw: "CHECKPOINT: did X\nNEXT STEPS: finish Y",
                        raising=False)

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "todo"})

    with open(p, "r", encoding="utf-8") as f:
        updated = json.load(f)

    comments = updated.get("comments", [])
    assert any("CHECKPOINT" in c.get("message", "") for c in comments), (
        "Expected a summary comment after killing the in-flight agent"
    )


def test_drag_out_of_in_progress_clears_marker(server, kanban, monkeypatch):
    """Moving out of in_progress should clear the orchestrator marker."""
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "in_progress"
    t["orchestrator"] = {
        "state": "dispatched",
        "pid": 55555,
        "killRequested": False,
        "logFile": ".kanban/_orchestrator/runs/1-test.log",
        "dispatchedAt": "2026-06-29T12:00:00+00:00",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    import orchestrator as orch
    monkeypatch.setattr(orch, "kill_pid", lambda pid: True)
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    monkeypatch.setattr(ks, "_ui_summarize_progress",
                        lambda *a, **kw: "summary",
                        raising=False)

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "todo"})

    with open(p, "r", encoding="utf-8") as f:
        updated = json.load(f)

    assert updated.get("orchestrator") is None, (
        "orchestrator marker should be cleared after UI kill"
    )


def test_drag_out_of_in_progress_no_marker_is_noop(server, kanban, monkeypatch):
    """Moving a ticket out of in_progress with no orchestrator marker is a no-op kill."""
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "in_progress"
    # No orchestrator marker.
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    killed = []
    import orchestrator as orch
    monkeypatch.setattr(orch, "kill_pid", lambda pid: killed.append(pid) or True)

    status, body = _req(server, "PATCH", "/api/board/demo/task/1", {"column": "ready"})
    assert status == 200
    assert body.get("ok") is True
    assert len(killed) == 0, "kill_pid must not be called when there is no marker"


def test_drag_within_in_progress_no_spawn(server, board_with_profile, monkeypatch):
    """PATCH column=in_progress when already in_progress should NOT spawn again."""
    # Set up an already in-flight ticket.
    p = os.path.join(board_with_profile, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "in_progress"
    t["orchestrator"] = {
        "state": "dispatched",
        "pid": 33333,
        "killRequested": False,
        "logFile": ".kanban/_orchestrator/runs/1-test.log",
        "dispatchedAt": "2026-06-29T12:00:00+00:00",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    spawned = []
    import orchestrator as orch
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **kw: spawned.append(1) or {})

    status, _ = _req(server, "PATCH", "/api/board/demo/task/1",
                     {"column": "in_progress"})
    assert status == 200
    assert len(spawned) == 0, "spawn_agent must not be called for an already in-flight ticket"
