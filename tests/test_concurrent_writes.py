"""Regression tests for ticket #42: full-object writes clobbering concurrent
field writes (lost updates).

A writer that reads a whole ticket, mutates one field, and writes the whole
object back will silently discard anything another writer (a sub-agent's comment,
question, or commitGate) wrote in the interim. These tests inject a concurrent
on-disk write into that window and assert it survives.
"""

import json
import os

import pytest

import kanban_server as ks
import orchestrator_core as oc
import orchestrator as orch


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path, task):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f)


def _inject_comment(path, message, writer="Sub"):
    """Simulate a concurrent writer appending a comment to the ticket on disk."""
    t = _read(path)
    t.setdefault("comments", []).append(
        {"writer": writer, "message": message, "timestamp": "z"})
    _write(path, t)


# --- server: add_comment / update_task_status -------------------------------

@pytest.fixture
def board(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    return kanban


def test_add_comment_preserves_concurrent_comment(board, monkeypatch):
    p = os.path.join(board, "demo", "1.json")
    fired = {"done": False}

    def patched_now():
        # Fire once, AFTER add_comment's initial read but before its write —
        # standing in for a sub-agent writing a comment concurrently.
        if not fired["done"]:
            fired["done"] = True
            _inject_comment(p, "concurrent sub-agent note")
        return "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(ks, "now_iso", patched_now)
    result, status = ks.add_comment("demo", "1", {"writer": "Me", "message": "mine"})
    assert status == 201
    msgs = [c["message"] for c in _read(p).get("comments", [])]
    assert "concurrent sub-agent note" in msgs
    assert "mine" in msgs


def test_update_status_preserves_concurrent_comment(board, monkeypatch):
    p = os.path.join(board, "demo", "1.json")
    fired = {"done": False}

    def patched_now():
        if not fired["done"]:
            fired["done"] = True
            _inject_comment(p, "concurrent sub-agent note")
        return "2026-01-01T00:00:00+00:00"

    monkeypatch.setattr(ks, "now_iso", patched_now)
    result, status = ks.update_task_status("demo", "1", "in_progress")
    assert status == 200
    after = _read(p)
    assert after["status"] == "in_progress"
    msgs = [c["message"] for c in after.get("comments", [])]
    assert "concurrent sub-agent note" in msgs


# --- orchestrator: per-tick writes ------------------------------------------

def _inflight(path, pid=123):
    t = _read(path)
    t["status"] = "in_progress"
    t["orchestrator"] = {
        "state": "dispatched", "pid": pid, "killRequested": False,
        "dispatchedAt": oc.now_iso(),
        "logFile": ".kanban/_orchestrator/runs/x.log",
    }
    _write(path, t)


def test_running_tick_preserves_concurrent_comment(kanban, monkeypatch):
    """A 'running' agent's tick must NOT rewrite the ticket from the stale
    snapshot — a comment the sub-agent wrote mid-tick has to survive."""
    p = os.path.join(kanban, "demo", "1.json")
    _inflight(p)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)

    def fake_reap(task, **kwargs):
        _inject_comment(p, "live progress note")  # concurrent sub-agent write
        return {"action": "running"}

    monkeypatch.setattr(oc, "reap_decision", fake_reap)
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    msgs = [c["message"] for c in _read(p).get("comments", [])]
    assert "live progress note" in msgs


def test_completed_tick_preserves_concurrent_commit_gate(kanban, monkeypatch):
    """When reaping a completed agent, a commitGate/comment the sub-agent wrote
    after the tick snapshot must survive the orchestrator's status write."""
    p = os.path.join(kanban, "demo", "1.json")
    _inflight(p)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    monkeypatch.setattr(orch, "_process_alive", lambda pid: False)
    monkeypatch.setattr(orch, "_finish_completion", lambda *a, **k: None)

    def fake_reap(task, **kwargs):
        t = _read(p)
        t["commitGate"] = {"requirementsMet": True, "summary": "ran tests"}
        t.setdefault("comments", []).append(
            {"writer": "Sub", "message": "done summary", "timestamp": "z"})
        _write(p, t)
        return {"action": "completed"}

    monkeypatch.setattr(oc, "reap_decision", fake_reap)
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    after = _read(p)
    assert after["status"] == "completed"
    assert after.get("commitGate", {}).get("requirementsMet") is True
    msgs = [c["message"] for c in after.get("comments", [])]
    assert "done summary" in msgs
