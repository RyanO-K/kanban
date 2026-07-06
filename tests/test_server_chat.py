"""Tests for POST /api/orchestrator/chat/<board>/<id> (agent chat inbox).

Spec: docs/specs/2026-07-03-agent-chat-design.md, Component 2. The endpoint
appends one raw {"message","writer","ts"} JSONL line to the ticket's inbox
file; writer-attribution wrapping is the pump's job, not the server's.
"""
import json
import os
import threading
import http.client

import pytest

import kanban_server as ks
import orchestrator_core as oc


@pytest.fixture
def server(kanban, monkeypatch):
    # Point the server AND the chat dir at the temp kanban tree.
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    monkeypatch.setattr(oc, "KANBAN_DIR", kanban, raising=False)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, json.dumps(body) if body is not None else None, hdrs)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(data) if data else None)


def _make_running(kanban, tid="1"):
    """Mark a ticket as a live run: dispatched marker + in_progress status."""
    p = os.path.join(kanban, "boards", "demo", f"{tid}.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "in_progress"
    t["orchestrator"] = {"state": "dispatched", "pid": 4242,
                         "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    return p


def test_chat_appends_inbox_line(server, kanban):
    _make_running(kanban)
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                        {"message": "hello agent", "writer": "ryan"})
    assert status == 200
    assert body == {"ok": True}
    inbox = oc.chat_inbox_path("demo", "1")
    assert os.path.isfile(inbox)
    lines = open(inbox, encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    # Raw fields stored — no writer-attribution wrapping at the server.
    assert obj["message"] == "hello agent"
    assert obj["writer"] == "ryan"
    assert obj["ts"], "ts must be stamped"


def test_chat_appends_in_order(server, kanban):
    _make_running(kanban)
    _req(server, "POST", "/api/orchestrator/chat/demo/1",
         {"message": "first", "writer": "ryan"})
    _req(server, "POST", "/api/orchestrator/chat/demo/1",
         {"message": "second", "writer": "ryan"})
    lines = open(oc.chat_inbox_path("demo", "1"), encoding="utf-8").read().splitlines()
    assert [json.loads(l)["message"] for l in lines] == ["first", "second"]


def test_chat_404_unknown_ticket(server):
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/999",
                        {"message": "hi", "writer": "ryan"})
    assert status == 404
    assert body == {"error": "not found"}


def test_chat_400_empty_or_non_string_message(server, kanban):
    _make_running(kanban)
    for bad in ({"message": "", "writer": "r"},
                {"message": "   ", "writer": "r"},
                {"message": 42, "writer": "r"},
                {"writer": "r"}):
        status, _ = _req(server, "POST", "/api/orchestrator/chat/demo/1", bad)
        assert status == 400, f"payload {bad!r} must be rejected with 400"
    assert not os.path.exists(oc.chat_inbox_path("demo", "1")), \
        "rejected messages must not touch the inbox"


def test_chat_409_when_not_running(server, kanban):
    # Default fixture ticket: status todo, no orchestrator marker.
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                        {"message": "hi", "writer": "ryan"})
    assert status == 409
    assert body == {"error": "not running"}

    # Dispatched marker but wrong status (e.g. blocked) is also "not running".
    p = os.path.join(kanban, "boards", "demo", "2.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "blocked"
    t["orchestrator"] = {"state": "dispatched", "pid": 1}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/2",
                        {"message": "hi", "writer": "ryan"})
    assert status == 409
    assert body == {"error": "not running"}


def test_chat_409_when_chat_disabled(server, kanban, monkeypatch):
    _make_running(kanban)
    monkeypatch.setattr(oc, "CHAT_ENABLED", False)
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                        {"message": "hi", "writer": "ryan"})
    assert status == 409
    assert body == {"error": "chat disabled"}
    assert not os.path.exists(oc.chat_inbox_path("demo", "1"))


def test_chat_cross_origin_requires_token(server, kanban):
    # Same _authorized() gate as every other state-changing route.
    # Connection: close avoids a Windows TCP abort (WinError 10053) that
    # occurs when the server sends 403 before reading the POST body.
    _make_running(kanban)
    status, _ = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                     {"message": "hi", "writer": "ryan"},
                     headers={"Origin": "http://evil.example.com",
                              "Connection": "close"})
    assert status == 403
    assert not os.path.exists(oc.chat_inbox_path("demo", "1"))
