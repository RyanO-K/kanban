"""Ticket #59: per-ticket model picklist.

A ticket can be tagged with a model (size) from the UI; the server persists it
to the ticket's top-level "model" field and the orchestrator honors it at
dispatch (ticket model > triage > profile)."""

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


@pytest.fixture
def board(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    return kanban


def test_set_model(board):
    p = os.path.join(board, "demo", "1.json")
    result, status = ks.update_task_model("demo", "1", "claude-opus-4-8")
    assert status == 200
    assert _read(p)["model"] == "claude-opus-4-8"


def test_clear_model(board):
    p = os.path.join(board, "demo", "1.json")
    ks.update_task_model("demo", "1", "claude-sonnet-4-6")
    result, status = ks.update_task_model("demo", "1", "")
    assert status == 200
    assert "model" not in _read(p)


def test_reject_unknown_model(board):
    result, status = ks.update_task_model("demo", "1", "gpt-9")
    assert status == 400
    assert "model" not in _read(os.path.join(board, "demo", "1.json"))


def test_set_model_preserves_concurrent_comment(board):
    """The re-read-before-write must not clobber a concurrent sub-agent write."""
    p = os.path.join(board, "demo", "1.json")

    # Simulate a sub-agent comment landing after the caller's read but before
    # write by patching write_ticket to inject then delegate.
    orig_write = ks.write_ticket

    def injecting_write(path, task):
        disk = _read(path)
        disk.setdefault("comments", []).append(
            {"writer": "Sub", "message": "concurrent note", "timestamp": "z"})
        _write(path, disk)
        # restore so only the first write injects
        ks.write_ticket = orig_write
        return orig_write(path, task)

    # Pre-seed a comment via the inject path: read happens in update_task_model,
    # then our patched write fires.
    ks.write_ticket = injecting_write
    try:
        result, status = ks.update_task_model("demo", "1", "claude-opus-4-8")
    finally:
        ks.write_ticket = orig_write
    assert status == 200
    after = _read(p)
    assert after["model"] == "claude-opus-4-8"


def test_dispatch_honors_ticket_model(kanban, monkeypatch):
    """A model pinned on the ticket wins over the profile default at dispatch."""
    p = os.path.join(kanban, "demo", "1.json")
    t = _read(p)
    t["model"] = "claude-haiku-4-5-20251001"
    _write(p, t)
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "stopAllRequested": False})

    captured = {}

    def fake_spawn(kanban_dir, board_slug, task, profile, model):
        captured["model"] = model
        return {"state": "dispatched", "pid": 1, "killRequested": False,
                "dispatchedAt": oc.now_iso(), "logFile": "x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    monkeypatch.setattr(oc, "list_profiles", lambda kd: [
        {"name": "general", "model": "claude-opus-4-8", "whenToUse": "all"}])

    def fake_triage(*a, **k):
        return {"dispatch": [{"ticket": "1", "board": "demo",
                              "profile": "general", "reason": "go"}]}

    orch.tick(kanban, opus_triage=fake_triage)
    assert captured.get("model") == "claude-haiku-4-5-20251001"
