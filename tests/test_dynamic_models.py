"""Ticket #11: dynamic model list.

The ticket model picklist (`MODEL_VALUES`/`MODEL_LIST`) used to be a hardcoded
constant, duplicated by hand in kanban.js. Instead, the server discovers the
live model catalog from the Anthropic API (`GET /v1/models`) on startup —
falling back to a static default list whenever no API key is configured or
the request fails, so a server with no network/key still boots and validates
normally."""

import json
import threading

import pytest

import kanban_server as ks


@pytest.fixture
def board(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    return kanban


@pytest.fixture
def server(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def test_discover_models_without_api_key_returns_defaults(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    models = ks.discover_models()
    assert models == ks.DEFAULT_MODELS


def test_discover_models_fetches_live_catalog():
    payload = {"data": [
        {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
        {"id": "claude-haiku-4-5-20251001", "display_name": "Haiku 4.5"},
        {"id": "not-a-claude-model", "display_name": "should be filtered"},
    ]}

    def fake_opener(req, timeout=None):
        assert req.get_header("X-api-key") == "sk-test"
        return _FakeResponse(payload)

    models = ks.discover_models(api_key="sk-test", opener=fake_opener)
    assert models == [
        {"value": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
        {"value": "claude-opus-4-8", "label": "Opus 4.8"},
    ]


def test_discover_models_falls_back_on_request_failure():
    def fake_opener(req, timeout=None):
        raise OSError("network unreachable")

    models = ks.discover_models(api_key="sk-test", opener=fake_opener)
    assert models == ks.DEFAULT_MODELS


def test_discover_models_falls_back_on_malformed_response():
    def fake_opener(req, timeout=None):
        return _FakeResponse({"unexpected": "shape"})

    models = ks.discover_models(api_key="sk-test", opener=fake_opener)
    assert models == ks.DEFAULT_MODELS


def test_discover_models_falls_back_when_no_claude_models_present():
    def fake_opener(req, timeout=None):
        return _FakeResponse({"data": [{"id": "gpt-9", "display_name": "x"}]})

    models = ks.discover_models(api_key="sk-test", opener=fake_opener)
    assert models == ks.DEFAULT_MODELS


def test_refresh_models_updates_module_globals(monkeypatch):
    monkeypatch.setattr(ks, "MODEL_LIST", list(ks.DEFAULT_MODELS))
    monkeypatch.setattr(ks, "MODEL_VALUES", {m["value"] for m in ks.DEFAULT_MODELS})

    def fake_discover():
        return [{"value": "claude-new-model", "label": "New Model"}]

    monkeypatch.setattr(ks, "discover_models", fake_discover)
    result = ks.refresh_models()
    assert result == [{"value": "claude-new-model", "label": "New Model"}]
    assert ks.MODEL_LIST == [{"value": "claude-new-model", "label": "New Model"}]
    assert ks.MODEL_VALUES == {"claude-new-model"}


def test_update_task_model_honors_refreshed_model_values(board, monkeypatch):
    monkeypatch.setattr(ks, "MODEL_VALUES", {"claude-new-model"})
    result, status = ks.update_task_model("demo", "1", "claude-new-model")
    assert status == 200
    result, status = ks.update_task_model("demo", "1", "claude-opus-4-8")
    assert status == 400


def test_models_list_returns_current_model_list(monkeypatch):
    monkeypatch.setattr(ks, "MODEL_LIST", [{"value": "claude-x", "label": "X"}])
    result, status = ks.models_list()
    assert status == 200
    assert result == {"models": [{"value": "claude-x", "label": "X"}]}


def test_get_api_models(server):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server)
    conn.request("GET", "/api/models")
    r = conn.getresponse()
    body = json.loads(r.read().decode("utf-8"))
    conn.close()
    assert r.status == 200
    assert "models" in body
    assert isinstance(body["models"], list)
