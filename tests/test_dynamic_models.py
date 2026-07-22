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


# --- Ticket #86: HTML fModel select must not have hardcoded claude options ---

import os
import re


def _read_kanban_html():
    """Read kanban.html from the path the server actually serves."""
    return open(ks.HTML_PATH, encoding="utf-8").read()


def _read_kanban_js():
    """Read kanban.js from the path the server actually serves."""
    return open(ks.JS_PATH, encoding="utf-8").read()


def test_fmodel_select_has_no_hardcoded_claude_options():
    """kanban.html's fModel <select> must contain only the (default) option.

    Model options are populated at runtime by loadModelOptions() in kanban.js
    so the picklist reflects the live Anthropic catalog, not a hand-maintained
    static list. Any hardcoded claude-* <option> inside fModel is a regression.
    """
    html = _read_kanban_html()
    # Extract the fModel select block
    m = re.search(
        r'id=["\']fModel["\'][^>]*>(.*?)</select>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    assert m, "fModel select not found in kanban.html"
    inner = m.group(1)
    # There must be no hardcoded claude-* option values inside fModel
    assert not re.search(r'value=["\']claude-', inner, re.IGNORECASE), (
        "fModel select has hardcoded claude-* options; "
        "these must be populated dynamically by loadModelOptions() in kanban.js"
    )


def test_load_model_options_populates_fmodel_select():
    """kanban.js's loadModelOptions must update the fModel select element.

    After fetching /api/models, the JS must repopulate both the in-memory
    MODEL_OPTIONS array and the fModel select DOM element so the Create Task
    modal reflects the live model catalog.
    """
    js = _read_kanban_js()
    # The loadModelOptions IIFE must reference the fModel element
    load_fn_match = re.search(
        r'async function loadModelOptions\(\).*?}\s*\)\(\)',
        js,
        re.DOTALL,
    )
    assert load_fn_match, "loadModelOptions function not found in kanban.js"
    fn_body = load_fn_match.group(0)
    assert "fModel" in fn_body, (
        "loadModelOptions() does not populate the fModel select element; "
        "add DOM update logic to keep the Create Task modal in sync"
    )


# --- Ticket #100: prevent moving to ready without a real model ---


def test_update_status_ready_requires_real_model(board, monkeypatch):
    """Moving to 'ready' must fail if the model field is empty (displays as '(default)').

    Ticket #100: prevent confusion where tickets without explicit models get stuck
    in an ambiguous "(default)" state. The ready column should only contain tickets
    that can actually dispatch with a defined model.
    """
    import os
    monkeypatch.setattr(ks, "KANBAN_DIR", board)
    # Modify the existing demo ticket 1 to have no model
    p = os.path.join(board, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        task = json.load(f)
    task.pop("model", None)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(task, f)

    # Try to move it to ready — should fail
    result, status = ks.update_task_status("demo", "1", "ready")
    assert status == 400
    assert "model" in result.get("error", "").lower()


def test_update_status_ready_succeeds_with_real_model(board, monkeypatch):
    """Moving to 'ready' succeeds if the model field is set to a real model."""
    import os
    monkeypatch.setattr(ks, "KANBAN_DIR", board)
    # Set the existing demo ticket 1 to have a real model
    p = os.path.join(board, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        task = json.load(f)
    task["model"] = "claude-opus-4-8"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(task, f)

    # Move it to ready — should succeed
    result, status = ks.update_task_status("demo", "1", "ready")
    assert status == 200
    assert result["newStatus"] == "ready"


def test_update_status_ready_with_whitespace_only_model_fails(board, monkeypatch):
    """Moving to 'ready' fails if the model field contains only whitespace."""
    import os
    monkeypatch.setattr(ks, "KANBAN_DIR", board)
    # Set the existing demo ticket 1 to have only whitespace for model
    p = os.path.join(board, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        task = json.load(f)
    task["model"] = "   "
    with open(p, "w", encoding="utf-8") as f:
        json.dump(task, f)

    # Try to move it to ready — should fail
    result, status = ks.update_task_status("demo", "1", "ready")
    assert status == 400
    assert "model" in result.get("error", "").lower()


def test_moving_to_other_columns_ignores_model(board, monkeypatch):
    """Moving to columns other than 'ready' should succeed regardless of model."""
    import os
    monkeypatch.setattr(ks, "KANBAN_DIR", board)
    # Set the existing demo ticket 1 to have no model
    p = os.path.join(board, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        task = json.load(f)
    task.pop("model", None)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(task, f)

    # Move to todo — should succeed (model check only for ready)
    result, status = ks.update_task_status("demo", "1", "todo")
    assert status == 200

    # Move to blocked — should succeed (model check only for ready)
    result, status = ks.update_task_status("demo", "1", "blocked")
    assert status == 200

    # Move to done — should succeed (model check only for ready)
    result, status = ks.update_task_status("demo", "1", "done")
    assert status == 200


# --- Ticket #107: Add Fable to default models ---


def test_fable_5_in_default_models():
    """claude-fable-5 must be in DEFAULT_MODELS so it is always available as a fallback
    even when live API discovery fails or ANTHROPIC_API_KEY is absent."""
    values = [m["value"] for m in ks.DEFAULT_MODELS]
    assert "claude-fable-5" in values, "Fable 5 missing from DEFAULT_MODELS"


def test_discover_models_tries_auth_token_when_api_key_absent(monkeypatch):
    """discover_models should fall back to ANTHROPIC_AUTH_TOKEN when ANTHROPIC_API_KEY
    is absent. In Claude Code OAuth environments, only ANTHROPIC_AUTH_TOKEN may be set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-auth-token")

    payload = {"data": [
        {"id": "claude-fable-5", "display_name": "Fable 5"},
        {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
    ]}

    def fake_opener(req, timeout=None):
        return _FakeResponse(payload)

    models = ks.discover_models(opener=fake_opener)
    ids = [m["value"] for m in models]
    assert "claude-fable-5" in ids
    assert "claude-opus-4-8" in ids
