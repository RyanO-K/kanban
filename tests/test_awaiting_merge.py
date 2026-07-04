"""Ticket #19: awaiting_merge status.

Tickets done but not yet merged into release/main can be set to
`awaiting_merge`. The status maps to its own column so it shows on the board,
and GET /api/board/<slug>/awaiting-merge returns only those tickets.
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
    # Seed a ticket in awaiting_merge status.
    p = os.path.join(kanban, "demo", "3.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"id": "3", "title": "Awaiting", "status": "awaiting_merge"}, f)
    return kanban


@pytest.fixture
def server(board):
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("GET", path)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(data) if data else None)


# --- STATUS_MAP / column mapping -------------------------------------------

def test_awaiting_merge_maps_to_own_column():
    assert ks.get_task_column("awaiting_merge") == "awaiting_merge"


def test_awaiting_merge_not_falling_back_to_todo():
    col = ks.get_task_column("awaiting_merge")
    assert col != "todo"


def test_awaiting_merge_in_column_status():
    assert "awaiting_merge" in ks.COLUMN_STATUS
    assert ks.COLUMN_STATUS["awaiting_merge"] == "awaiting_merge"


def test_awaiting_merge_column_in_columns_list():
    keys = [c["key"] for c in ks.COLUMNS]
    assert "awaiting_merge" in keys


# --- list_awaiting_merge (unit) --------------------------------------------

def test_list_awaiting_merge_returns_matching_tickets(board):
    result, status = ks.list_awaiting_merge("demo")
    assert status == 200
    assert isinstance(result["tasks"], list)
    ids = [t["id"] for t in result["tasks"]]
    assert "3" in ids


def test_list_awaiting_merge_excludes_other_statuses(board):
    result, _ = ks.list_awaiting_merge("demo")
    for t in result["tasks"]:
        assert t["status"] == "awaiting_merge"


def test_list_awaiting_merge_unknown_board_is_404(board):
    _, status = ks.list_awaiting_merge("no-such-board")
    assert status == 404


def test_list_awaiting_merge_all_boards(board):
    result, status = ks.list_awaiting_merge("__all__")
    assert status == 200
    ids = [t["id"] for t in result["tasks"]]
    assert "3" in ids


# --- load_board includes column -------------------------------------------

def test_load_board_assigns_awaiting_merge_column(board):
    data, status = ks.load_board("demo")
    assert status == 200
    task3 = next(t for t in data["tasks"] if t["id"] == "3")
    assert task3["_column"] == "awaiting_merge"


# --- update_task_status can move to awaiting_merge ------------------------

def test_move_ticket_to_awaiting_merge(board):
    result, status = ks.update_task_status("demo", "1", "awaiting_merge")
    assert status == 200
    assert result["newStatus"] == "awaiting_merge"
    p = os.path.join(board, "demo", "1.json")
    with open(p, encoding="utf-8") as f:
        task = json.load(f)
    assert task["status"] == "awaiting_merge"


# --- HTTP route GET /api/board/<slug>/awaiting-merge ----------------------

def test_http_awaiting_merge_route(server):
    status, body = _get(server, "/api/board/demo/awaiting-merge")
    assert status == 200
    ids = [t["id"] for t in body["tasks"]]
    assert "3" in ids


def test_http_awaiting_merge_all_boards(server):
    status, body = _get(server, "/api/board/__all__/awaiting-merge")
    assert status == 200
    ids = [t["id"] for t in body["tasks"]]
    assert "3" in ids


def test_http_awaiting_merge_unknown_board_404(server):
    status, _ = _get(server, "/api/board/no-such-board/awaiting-merge")
    assert status == 404
