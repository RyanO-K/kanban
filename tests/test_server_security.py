"""Ticket #43 — bind to loopback, tighten CORS, require auth for mutations.

The server exposes destructive endpoints (perf_kill, server/restart, DELETE
task, ...). It must not be reachable from arbitrary LAN hosts, and a website the
user merely visits must not be able to drive state-changing requests via the
browser. These tests pin:

  * the default bind host is loopback (127.0.0.1), not 0.0.0.0;
  * CORS echoes only same-origin (localhost/127.0.0.1) Origins, never "*";
  * state-changing requests carrying a foreign browser Origin are rejected
    unless they present the local auth token.
"""
import json
import threading
import http.client

import pytest

import kanban_server as ks


@pytest.fixture
def server(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    monkeypatch.setattr(ks, "AUTH_TOKEN", "secret-test-token")
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
    acao = r.getheader("Access-Control-Allow-Origin")
    conn.close()
    return r.status, (json.loads(data) if data else None), acao


# --- bind host --------------------------------------------------------------

def test_default_host_is_loopback():
    assert ks.HOST == "127.0.0.1", "server must default to loopback, not 0.0.0.0"


# --- CORS -------------------------------------------------------------------

def test_cors_echoes_same_origin(server):
    origin = "http://localhost:%d" % server
    status, _, acao = _req(server, "GET", "/api/files", headers={"Origin": origin})
    assert status == 200
    assert acao == origin, "same-origin request should get its Origin echoed back"


def test_cors_never_wildcards(server):
    status, _, acao = _req(server, "GET", "/api/files",
                           headers={"Origin": "http://evil.example.com"})
    # The read still succeeds for a non-browser client, but the browser must not
    # be told it may read the response: no wildcard, no foreign-origin echo.
    assert acao != "*"
    assert acao != "http://evil.example.com"


# --- auth on state-changing endpoints --------------------------------------

def test_mutation_from_foreign_origin_rejected(server):
    status, _, _ = _req(server, "DELETE", "/api/board/demo/task/1",
                        headers={"Origin": "http://evil.example.com"})
    assert status == 403, "cross-origin mutation without token must be rejected"


def test_mutation_from_foreign_origin_allowed_with_token(server):
    status, body, _ = _req(server, "DELETE", "/api/board/demo/task/1",
                           headers={"Origin": "http://evil.example.com",
                                    "X-Kanban-Token": "secret-test-token"})
    assert status == 200
    assert body["ok"] is True


def test_mutation_without_origin_still_allowed(server):
    # Non-browser clients (CLI tools, the orchestrator, tests) send no Origin and
    # reach the server only over loopback — they remain unauthenticated-friendly.
    status, body, _ = _req(server, "DELETE", "/api/board/demo/task/2")
    assert status == 200
    assert body["ok"] is True


def test_get_not_blocked_by_auth(server):
    # Reads are not state-changing; a foreign Origin may still issue them (it just
    # cannot read the response — see test_cors_never_wildcards).
    status, _, _ = _req(server, "GET", "/api/files",
                        headers={"Origin": "http://evil.example.com"})
    assert status == 200
