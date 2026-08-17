import json
import os
import threading
import http.client

import pytest

import kanban_server as ks


@pytest.fixture
def board_meta(kanban, monkeypatch):
    """Point the server at the temp kanban tree."""
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    return kanban


@pytest.fixture
def server(board_meta):
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(data) if data else None)


# --- update_board_meta (unit) ----------------------------------------------

def test_update_sets_commit_requirements(board_meta):
    result, status = ks.update_board_meta(
        "demo", {"commitRequirements": "All tests must pass before committing."}
    )
    assert status == 200
    assert result["ok"] is True
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["commitRequirements"] == "All tests must pass before committing."


def test_update_bumps_updated_date(board_meta):
    ks.update_board_meta("demo", {"commitRequirements": "x"})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["updated"] == ks.date.today().isoformat()


def test_update_preserves_existing_fields(board_meta):
    # Seed an existing field that update must not clobber.
    meta_path = os.path.join(board_meta, "boards", "demo", "_meta.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    meta["openQuestions"] = ["Q?"]
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)

    ks.update_board_meta("demo", {"commitRequirements": "y"})
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["project"] == "Demo"
    assert meta["openQuestions"] == ["Q?"]
    assert meta["commitRequirements"] == "y"


def test_update_ignores_unknown_fields(board_meta):
    ks.update_board_meta("demo", {"bogus": "nope", "commitRequirements": "ok"})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert "bogus" not in meta
    assert meta["commitRequirements"] == "ok"


def test_update_clears_commit_requirements_with_empty_string(board_meta):
    ks.update_board_meta("demo", {"commitRequirements": "to be removed"})
    ks.update_board_meta("demo", {"commitRequirements": ""})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert "commitRequirements" not in meta


def test_update_unknown_board_is_404(board_meta):
    _, status = ks.update_board_meta("does-not-exist", {"commitRequirements": "x"})
    assert status == 404


# --- useWorktrees per-project toggle (ticket #40) ---------------------------

def test_update_sets_use_worktrees_true(board_meta):
    result, status = ks.update_board_meta("demo", {"useWorktrees": True})
    assert status == 200
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["useWorktrees"] is True


def test_update_persists_use_worktrees_false(board_meta):
    # An explicit OFF must persist as False (not be dropped), so reload keeps it off.
    ks.update_board_meta("demo", {"useWorktrees": True})
    ks.update_board_meta("demo", {"useWorktrees": False})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["useWorktrees"] is False


def test_load_board_exposes_use_worktrees(board_meta):
    ks.update_board_meta("demo", {"useWorktrees": True})
    data, status = ks.load_board("demo")
    assert status == 200
    assert data["useWorktrees"] is True


def test_put_meta_use_worktrees_survives_reload(server):
    status, body = _req(server, "PUT", "/api/board/demo/meta", {"useWorktrees": True})
    assert status == 200
    status, board = _req(server, "GET", "/api/board/demo")
    assert board["useWorktrees"] is True


# --- load_board passthrough -------------------------------------------------

def test_load_board_exposes_commit_requirements(board_meta):
    ks.update_board_meta("demo", {"commitRequirements": "Run pytest first."})
    data, status = ks.load_board("demo")
    assert status == 200
    assert data["commitRequirements"] == "Run pytest first."


# --- HTTP route -------------------------------------------------------------

def test_put_meta_route(server):
    status, body = _req(
        server, "PUT", "/api/board/demo/meta",
        {"commitRequirements": "tests green before commit"},
    )
    assert status == 200
    assert body["ok"] is True
    status, board = _req(server, "GET", "/api/board/demo")
    assert board["commitRequirements"] == "tests green before commit"


def test_put_meta_renames_project(server):
    # The UI's Board Settings form also edits the project name.
    status, _ = _req(server, "PUT", "/api/board/demo/meta", {"project": "Renamed"})
    assert status == 200
    status, files = _req(server, "GET", "/api/files")
    demo = next(f for f in files if f["filename"] == "demo")
    assert demo["project"] == "Renamed"


# --- useDocker optional per-board (ticket #87) --------------------------------

def test_disabling_use_docker_clears_env_vars(board_meta):
    # Seed envVars on a Docker-enabled board, then turn Docker off.
    ks.update_board_meta("demo", {"useDocker": True, "envVars": {"FOO": "bar"}})
    ks.update_board_meta("demo", {"useDocker": False})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert "envVars" not in meta


def test_disabling_use_docker_clears_passthrough_env(board_meta):
    ks.update_board_meta("demo", {"useDocker": True, "passthroughEnv": ["GITHUB_TOKEN"]})
    ks.update_board_meta("demo", {"useDocker": False})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert "passthroughEnv" not in meta


def test_enabling_use_docker_preserves_env_vars(board_meta):
    # Turning Docker ON (or back ON) does not clear the container config.
    ks.update_board_meta("demo", {"useDocker": True, "envVars": {"FOO": "bar"}})
    ks.update_board_meta("demo", {"useDocker": True})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta.get("envVars") == {"FOO": "bar"}


def test_disabling_use_docker_via_http_clears_container_config(server):
    # End-to-end: the PUT route also clears container config when Docker is turned off.
    _req(server, "PUT", "/api/board/demo/meta",
         {"useDocker": True, "envVars": {"K": "v"}, "passthroughEnv": ["TOKEN"]})
    status, body = _req(server, "PUT", "/api/board/demo/meta", {"useDocker": False})
    assert status == 200
    status, board = _req(server, "GET", "/api/board/demo")
    assert board.get("useDocker") is False
    assert "envVars" not in board
    assert "passthroughEnv" not in board


# --- showMergeBranch per-board toggle (ticket #109) --------------------------

def test_update_sets_show_merge_branch_true(board_meta):
    result, status = ks.update_board_meta("demo", {"showMergeBranch": True})
    assert status == 200
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["showMergeBranch"] is True


def test_update_sets_show_merge_branch_false(board_meta):
    ks.update_board_meta("demo", {"showMergeBranch": True})
    ks.update_board_meta("demo", {"showMergeBranch": False})
    with open(os.path.join(board_meta, "boards", "demo", "_meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["showMergeBranch"] is False


def test_load_board_exposes_show_merge_branch(board_meta):
    ks.update_board_meta("demo", {"showMergeBranch": True})
    data, status = ks.load_board("demo")
    assert status == 200
    assert data["showMergeBranch"] is True


def test_put_meta_show_merge_branch_survives_reload(server):
    status, body = _req(server, "PUT", "/api/board/demo/meta", {"showMergeBranch": True})
    assert status == 200
    status, board = _req(server, "GET", "/api/board/demo")
    assert board["showMergeBranch"] is True
