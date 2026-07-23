"""Ticket #89: inline title and description editing.

update_task_fields() persists title/detail edits made from the sidebar."""

import json
import os

import pytest

import kanban_server as ks


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


def test_update_title(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    result, status = ks.update_task_fields("demo", "1", "New Title", None)
    assert status == 200
    assert _read(p)["title"] == "New Title"


def test_update_detail(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    result, status = ks.update_task_fields("demo", "1", None, "A new description")
    assert status == 200
    assert _read(p)["detail"] == "A new description"


def test_update_both(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    result, status = ks.update_task_fields("demo", "1", "My Title", "My Detail")
    assert status == 200
    t = _read(p)
    assert t["title"] == "My Title"
    assert t["detail"] == "My Detail"


def test_clear_detail(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    ks.update_task_fields("demo", "1", None, "Some detail")
    result, status = ks.update_task_fields("demo", "1", None, "")
    assert status == 200
    assert "detail" not in _read(p)


def test_empty_title_rejected(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    orig_title = _read(p)["title"]
    result, status = ks.update_task_fields("demo", "1", "   ", None)
    assert status == 400
    assert _read(p)["title"] == orig_title


def test_title_stripped(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    result, status = ks.update_task_fields("demo", "1", "  Spaced Title  ", None)
    assert status == 200
    assert _read(p)["title"] == "Spaced Title"


def test_board_not_found(board):
    result, status = ks.update_task_fields("nonexistent", "1", "X", None)
    assert status == 404


def test_task_not_found(board):
    result, status = ks.update_task_fields("demo", "9999", "X", None)
    assert status == 404


def test_write_succeeds_alongside_concurrent_disk_activity(board):
    """update_task_fields succeeds even when write_ticket is called concurrently."""
    p = os.path.join(board, "boards", "demo", "1.json")
    orig_write = ks.write_ticket

    def injecting_write(path, task):
        # Simulate concurrent disk activity then proceed normally.
        disk = _read(path)
        disk.setdefault("comments", []).append(
            {"writer": "Agent", "message": "concurrent note", "timestamp": "z"})
        _write(path, disk)
        ks.write_ticket = orig_write
        return orig_write(path, task)

    ks.write_ticket = injecting_write
    try:
        result, status = ks.update_task_fields("demo", "1", "Updated Title", None)
    finally:
        ks.write_ticket = orig_write

    assert status == 200
    assert _read(p)["title"] == "Updated Title"


# --- mergeBranch field (ticket #109) ----------------------------------------

def test_set_merge_branch(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    result, status = ks.update_task_fields("demo", "1", None, None, "release-v2")
    assert status == 200
    assert _read(p)["mergeBranch"] == "release-v2"


def test_clear_merge_branch(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    ks.update_task_fields("demo", "1", None, None, "main")
    result, status = ks.update_task_fields("demo", "1", None, None, "")
    assert status == 200
    assert "mergeBranch" not in _read(p)


def test_merge_branch_stripped(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    result, status = ks.update_task_fields("demo", "1", None, None, "  feature/foo  ")
    assert status == 200
    assert _read(p)["mergeBranch"] == "feature/foo"


def test_merge_branch_independent_of_other_fields(board):
    p = os.path.join(board, "boards", "demo", "1.json")
    ks.update_task_fields("demo", "1", "Keep Title", None, None)
    result, status = ks.update_task_fields("demo", "1", None, None, "main")
    assert status == 200
    t = _read(p)
    assert t["title"] == "Keep Title"
    assert t["mergeBranch"] == "main"
