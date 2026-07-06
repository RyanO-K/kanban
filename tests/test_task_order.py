"""Ticket #47: Ready list ordering.

update_task_order persists an integer `order` field on a ticket so the
front-end can sort the Ready column and the orchestrator can prioritize
dispatch."""

import json
import os

import pytest

import kanban_server as ks
import orchestrator_core as oc


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def board(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    return kanban


def test_set_order(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    result, status = ks.update_task_order("demo", "1", 3)
    assert status == 200
    assert _read(p)["order"] == 3


def test_update_order(board):
    ks.update_task_order("demo", "1", 5)
    result, status = ks.update_task_order("demo", "1", 1)
    assert status == 200
    assert _read(os.path.join(board, "boards", "demo", "1.json"))["order"] == 1


def test_clear_order(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    ks.update_task_order("demo", "1", 2)
    result, status = ks.update_task_order("demo", "1", None)
    assert status == 200
    assert "order" not in _read(p)


def test_invalid_order_rejected(board):
    result, status = ks.update_task_order("demo", "1", "banana")
    assert status == 400
    assert "order" not in _read(os.path.join(board, "boards", "demo", "1.json"))


def test_unknown_board_is_404(board):
    result, status = ks.update_task_order("no-such-board", "1", 0)
    assert status == 404


def test_unknown_ticket_is_404(board):
    result, status = ks.update_task_order("demo", "99", 0)
    assert status == 404


def test_order_preserves_other_fields(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    ks.update_task_order("demo", "1", 7)
    after = _read(p)
    assert after["title"] == "First"
    assert after["status"] == "todo"
    assert after["order"] == 7


def test_load_board_includes_board_slug(board):
    """load_board must set _board on each task so the UI can correctly identify
    cross-board tickets when viewing a single board."""
    data, status = ks.load_board("demo")
    assert status == 200
    for task in data["tasks"]:
        assert task.get("_board") == "demo", f"task {task['id']} missing _board"


def test_eligible_tickets_sorted_by_order():
    """eligible_tickets returns ready tickets sorted by order asc so the
    orchestrator's backfill dispatches the highest-priority ticket first."""
    tasks = [
        {"id": "3", "status": "ready", "order": 2},
        {"id": "1", "status": "ready", "order": 0},
        {"id": "2", "status": "ready", "order": 1},
    ]
    ids = [t["id"] for t in oc.eligible_tickets(tasks)]
    assert ids == ["1", "2", "3"]


def test_eligible_tickets_unordered_after_ordered():
    """Tickets without an order field sort after those with one."""
    tasks = [
        {"id": "2", "status": "ready"},
        {"id": "1", "status": "ready", "order": 0},
    ]
    ids = [t["id"] for t in oc.eligible_tickets(tasks)]
    assert ids == ["1", "2"]


def test_eligible_tickets_answered_question_not_sorted_by_order():
    """Blocked tickets re-dispatching via answered question are not Ready tickets;
    they appear after ordered-ready tickets but before unordered ones — or simply
    at the end as long as they're present."""
    tasks = [
        {"id": "2", "status": "ready", "order": 0},
        {"id": "1", "status": "blocked",
         "orchestrator": {"state": "blocked",
                          "question": {"id": "q1", "answer": {"value": "x", "notes": ""}}}},
    ]
    ids = [t["id"] for t in oc.eligible_tickets(tasks)]
    assert "1" in ids and "2" in ids
    assert ids[0] == "2"  # ordered ready ticket comes first
