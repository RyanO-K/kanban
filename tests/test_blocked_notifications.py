"""Tests for ticket #97: blocked-ticket notification lifecycle.

When a blocked ticket (with an unanswered orchestrator.question) is moved out of
blocked by the user via PATCH /api/board/<slug>/task/<id>, the notification
(orchestrator.question) must be cleared so the bell stops alerting.

When a ticket re-blocks (question is re-set by an agent), the existing mechanism
already works — these tests verify the clear-on-unblock path.
"""
import json
import os
import threading
import http.client

import pytest

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


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(data) if data else None)


def _make_blocked_ticket(kanban, unanswered=True):
    """Write ticket 1 as blocked with an orchestrator.question."""
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "blocked"
    # Ticket #100: ensure ticket has a model set so it can move to ready
    t["model"] = "claude-opus-4-8"
    q = {
        "id": "q-1",
        "type": "input",
        "format": "text",
        "prompt": "Which approach should I take?",
        "answer": None if unanswered else {"value": "go with A", "notes": ""},
        "answeredAt": None,
    }
    t["orchestrator"] = {"state": "blocked", "question": q}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    return p


# ---------------------------------------------------------------------------
# Moving OUT of blocked clears the notification (orchestrator.question)
# ---------------------------------------------------------------------------

def test_unblock_clears_unanswered_question(server, kanban):
    """Moving a blocked ticket to ready must clear orchestrator.question
    so the notification bell stops showing it."""
    p = _make_blocked_ticket(kanban, unanswered=True)

    status, body = _req(server, "PATCH", "/api/board/demo/task/1",
                        {"column": "ready"})
    assert status == 200, f"Expected 200, got {status}: {body}"

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)

    orch = saved.get("orchestrator") or {}
    assert orch.get("question") is None, (
        "orchestrator.question should be cleared when a blocked ticket is moved out of blocked"
    )


def test_unblock_to_todo_clears_question(server, kanban):
    """Moving blocked → todo also clears the question."""
    p = _make_blocked_ticket(kanban, unanswered=True)

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "todo"})

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)

    orch = saved.get("orchestrator") or {}
    assert orch.get("question") is None, (
        "orchestrator.question should be cleared when moving blocked → todo"
    )


def test_unblock_clears_answered_question_too(server, kanban):
    """Even an already-answered question is cleared when moving out of blocked,
    so stale answered notifications don't linger on re-displayed tickets."""
    p = _make_blocked_ticket(kanban, unanswered=False)

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "in_progress"})

    import orchestrator as orch
    # stub kill/summarize for the in_progress path
    import kanban_server as ks2
    original_kill = ks2._ui_kill_session
    original_dispatch = ks2._ui_dispatch_session

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)

    orch2 = saved.get("orchestrator") or {}
    assert orch2.get("question") is None, (
        "orchestrator.question should be cleared even if already answered"
    )


def test_non_blocked_move_does_not_clear_question(server, kanban):
    """Moving from todo → ready (not from blocked) must NOT touch orchestrator.question
    — the clear is scoped to transitions OUT of the blocked column only."""
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "todo"
    # Ticket #100: ensure ticket has a model set so it can move to ready
    t["model"] = "claude-opus-4-8"
    # Give it a question even though it's not blocked (shouldn't happen in practice,
    # but the server must not clear it if the source status wasn't blocked).
    t["orchestrator"] = {
        "state": "idle",
        "question": {
            "id": "q-x",
            "type": "input",
            "prompt": "test question",
            "answer": None,
            "answeredAt": None,
        },
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "ready"})

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)

    orch = saved.get("orchestrator") or {}
    assert orch.get("question") is not None, (
        "orchestrator.question should NOT be cleared for a non-blocked transition"
    )


def test_unblock_preserves_other_orchestrator_fields(server, kanban):
    """Clearing the question must not disturb other orchestrator fields (pid, logFile, etc.)."""
    p = _make_blocked_ticket(kanban, unanswered=True)
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["orchestrator"]["pid"] = 12345
    t["orchestrator"]["logFile"] = ".kanban/_orchestrator/runs/1-test.log"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    _req(server, "PATCH", "/api/board/demo/task/1", {"column": "ready"})

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)

    orch = saved.get("orchestrator") or {}
    assert orch.get("question") is None
    assert orch.get("pid") == 12345, "pid must survive the question clear"
    assert orch.get("logFile") is not None, "logFile must survive the question clear"


def test_unblock_no_orchestrator_block_is_noop(server, kanban):
    """Moving a blocked ticket that has no orchestrator block at all is safe (no KeyError)."""
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "blocked"
    # Ticket #100: ensure ticket has a model set so it can move to ready
    t["model"] = "claude-opus-4-8"
    t.pop("orchestrator", None)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)

    status, body = _req(server, "PATCH", "/api/board/demo/task/1", {"column": "ready"})
    assert status == 200, f"Expected 200 but got {status}: {body}"

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["status"] == "ready"


def test_unblock_clears_question_direct_call(kanban, monkeypatch):
    """Unit-test update_task_status() directly (no HTTP) to verify the clear logic."""
    import kanban_server as ks2
    monkeypatch.setattr(ks2, "KANBAN_DIR", kanban)

    p = _make_blocked_ticket(kanban, unanswered=True)

    result, status = ks2.update_task_status("demo", "1", "ready")
    assert status == 200
    assert result.get("ok") is True

    with open(p, "r", encoding="utf-8") as f:
        saved = json.load(f)

    orch = saved.get("orchestrator") or {}
    assert orch.get("question") is None, (
        "update_task_status must clear orchestrator.question when moving out of blocked"
    )
