"""Clear-done: hide finished tickets without touching their files.

POST /api/board/<slug>/clear-done stamps `cleared: true` on every done ticket
in place — no file is moved or deleted, so history/comments and id numbering
survive. Board payloads (single board and __all__) skip tickets that are
cleared AND done; a cleared ticket whose status later goes active reappears
on its own.
"""

import json
import os
import threading
import http.client

import pytest

import kanban_server as ks


@pytest.fixture
def board(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    return kanban


def _ticket_path(board, tid):
    return os.path.join(board, "boards", "demo", f"{tid}.json")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write(path, task):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f)


def _mark_done(board, tid):
    p = _ticket_path(board, tid)
    task = _read(p)
    task["status"] = "completed"
    _write(p, task)


# --- clear_done_tasks (unit) -------------------------------------------------

def test_clear_stamps_done_tickets_only(board):
    _mark_done(board, "1")  # ticket 2 stays todo
    result, status = ks.clear_done_tasks("demo")
    assert status == 200
    assert result == {"ok": True, "cleared": 1}
    done = _read(_ticket_path(board, "1"))
    assert done["cleared"] is True and done["clearedAt"]
    assert "cleared" not in _read(_ticket_path(board, "2"))


def test_clear_leaves_files_in_place(board):
    _mark_done(board, "1")
    before = set(os.listdir(os.path.join(board, "boards", "demo")))
    ks.clear_done_tasks("demo")
    after = set(os.listdir(os.path.join(board, "boards", "demo")))
    assert before == after  # nothing moved, nothing deleted


def test_clear_is_idempotent(board):
    _mark_done(board, "1")
    ks.clear_done_tasks("demo")
    result, _ = ks.clear_done_tasks("demo")
    assert result["cleared"] == 0


def test_clear_nothing_done(board):
    result, status = ks.clear_done_tasks("demo")
    assert status == 200 and result["cleared"] == 0


def test_clear_unknown_board_404(board):
    _result, status = ks.clear_done_tasks("ghost")
    assert status == 404


def test_clear_preserves_history_and_comments(board):
    p = _ticket_path(board, "1")
    task = _read(p)
    task["status"] = "completed"
    task["history"] = [{"action": "status_change", "from": "todo", "to": "completed"}]
    task["comments"] = [{"writer": "Claude", "message": "did the thing"}]
    _write(p, task)
    ks.clear_done_tasks("demo")
    after = _read(p)
    assert after["history"] == task["history"]
    assert after["comments"] == task["comments"]


# --- board payload filtering -------------------------------------------------

def test_cleared_done_ticket_hidden_from_board(board):
    _mark_done(board, "1")
    ks.clear_done_tasks("demo")
    data, _ = ks.load_board("demo")
    assert [t["id"] for t in data["tasks"]] == ["2"]


def test_cleared_done_ticket_hidden_from_all_view(board):
    _mark_done(board, "1")
    ks.clear_done_tasks("demo")
    data, _ = ks.load_board(ks.ALL_SLUG)
    assert "1" not in [t["id"] for t in data["tasks"]]


def test_revived_cleared_ticket_reappears(board):
    _mark_done(board, "1")
    ks.clear_done_tasks("demo")
    p = _ticket_path(board, "1")
    task = _read(p)
    task["status"] = "in_progress"  # an agent picks it back up
    _write(p, task)
    data, _ = ks.load_board("demo")
    assert "1" in [t["id"] for t in data["tasks"]]


def test_uncleared_done_ticket_still_shows(board):
    _mark_done(board, "1")
    data, _ = ks.load_board("demo")
    assert "1" in [t["id"] for t in data["tasks"]]


def test_id_numbering_unaffected_by_clear(board):
    _mark_done(board, "2")
    ks.clear_done_tasks("demo")
    result, status = ks.create_task("demo", {"title": "next"})
    assert status in (200, 201)
    assert result["task"]["id"] == "3" if "task" in result else result["id"] == "3"


# --- HTTP route ----------------------------------------------------------------

@pytest.fixture
def server(board):
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _req(port, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(method, path)
    r = conn.getresponse()
    raw = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(raw) if raw else None)


def test_route_clear_done(board, server):
    _mark_done(board, "1")
    status, body = _req(server, "POST", "/api/board/demo/clear-done")
    assert status == 200
    assert body == {"ok": True, "cleared": 1}


def test_route_clear_done_unknown_board(server):
    status, _body = _req(server, "POST", "/api/board/ghost/clear-done")
    assert status == 404
