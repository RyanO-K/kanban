"""Ticket #55: global auto-commit / auto-push kill switch.

`_finish_completion` unconditionally stages+commits (and pushes source work) on
every completed ticket; the only gate was per-board `commitRequirements`. This
adds two global switches on the orchestrator state (`autoCommit`, `autoPush`,
both default true) so a user can run "dispatch but never auto-commit/push" and
review diffs first. When `autoCommit` is off the orchestrator records the pending
output in a comment but skips all git mutation; when `autoPush` is off it still
commits locally but never pushes.
"""

import json
import os

import orchestrator_core as oc
import orchestrator as orch


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- orchestrator_core: state defaults + coercion ---

def test_default_state_has_switches_on():
    assert oc.DEFAULT_STATE["autoCommit"] is True
    assert oc.DEFAULT_STATE["autoPush"] is True


def test_read_state_defaults_switches_on(kanban):
    state = oc.read_state(kanban)
    assert state["autoCommit"] is True
    assert state["autoPush"] is True


def test_auto_commit_enabled_default_true():
    assert oc.auto_commit_enabled({}) is True
    assert oc.auto_commit_enabled(None) is True


def test_auto_commit_enabled_respects_false():
    assert oc.auto_commit_enabled({"autoCommit": False}) is False


def test_auto_commit_enabled_coerces_stringy_false():
    # A form control may submit the stringified bool.
    assert oc.auto_commit_enabled({"autoCommit": "false"}) is False
    assert oc.auto_commit_enabled({"autoCommit": "off"}) is False
    assert oc.auto_commit_enabled({"autoCommit": "true"}) is True


def test_auto_push_enabled_default_true_and_coerces():
    assert oc.auto_push_enabled({}) is True
    assert oc.auto_push_enabled({"autoPush": False}) is False
    assert oc.auto_push_enabled({"autoPush": "no"}) is False


# --- _finish_completion honours the autoCommit kill switch ---

def _completed_task(kanban):
    return {"id": "1", "title": "First", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}


def test_finish_completion_skips_all_git_when_autocommit_off(kanban, monkeypatch):
    oc.write_state(kanban, dict(oc.DEFAULT_STATE, autoCommit=False))
    monkeypatch.setattr(orch, "discover_changed_paths",
                        lambda kd: [".kanban/demo/1.json", "src/app.py"])
    committed, published = [], []
    monkeypatch.setattr(orch, "commit_to_master",
                        lambda *a, **k: committed.append(a) or {"committed": True, "detail": ""})
    monkeypatch.setattr(orch, "publish_output_branch",
                        lambda *a, **k: published.append(a) or
                        {"branch": "x", "pushed": True, "detail": ""})
    task = _completed_task(kanban)

    orch._finish_completion(kanban, task)

    assert committed == [], "auto-commit off must skip commit_to_master"
    assert published == [], "auto-commit off must skip branch publish/push"
    msgs = " ".join(c["message"] for c in task.get("comments", []))
    assert "src/app.py" in msgs, "the pending diff must be recorded for review"
    assert "disabled" in msgs.lower()


def test_finish_completion_commits_when_autocommit_on(kanban, monkeypatch):
    """Regression: the default (switch on) keeps the existing commit behaviour."""
    oc.write_state(kanban, dict(oc.DEFAULT_STATE))  # both on
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: [".kanban/demo/1.json"])
    committed = []
    monkeypatch.setattr(orch, "commit_to_master",
                        lambda kd, task, summary: committed.append(summary) or
                        {"committed": True, "detail": "committed to master"})
    task = _completed_task(kanban)

    orch._finish_completion(kanban, task)
    assert committed, "auto-commit on must commit kanban-only work to master"


def test_finish_completion_passes_push_flag_to_publish(kanban, monkeypatch):
    """autoPush off → source work still publishes the branch but without pushing."""
    oc.write_state(kanban, dict(oc.DEFAULT_STATE, autoPush=False))
    monkeypatch.setattr(orch, "discover_changed_paths",
                        lambda kd: ["src/app.py"])  # non-kanban → branch publish
    seen = {}
    monkeypatch.setattr(orch, "publish_output_branch",
                        lambda kd, task, branch, **k: seen.update(k) or
                        {"branch": branch, "pushed": False, "detail": "not pushed"})
    task = _completed_task(kanban)

    orch._finish_completion(kanban, task)
    assert seen.get("push") is False, "autoPush off must forward push=False to publish"


# --- publish_output_branch honours push=False (commit locally, no push) ---

def test_publish_output_branch_no_push_commits_locally(kanban, monkeypatch):
    cmds = []

    def fake_git(cmd, **kwargs):
        cmds.append(cmd)
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)
    monkeypatch.setattr(orch, "_default_branch_ref", lambda cwd: "main")
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: ["src/app.py"])
    monkeypatch.setattr(orch, "_is_worktree_dir", lambda cwd: False)
    monkeypatch.setattr(orch, "_ticket_branch_in_repo", lambda cwd, task: None)
    task = {"id": "1", "title": "First", "_board": "demo"}

    result = orch.publish_output_branch(kanban, task, "ticket/1-first", push=False)

    assert not any(c[:1] == ["push"] for c in cmds), "push=False must skip git push"
    assert any(c[:1] == ["commit"] for c in cmds), "it must still commit locally"
    assert result["pushed"] is False
    assert result["branch"] == "ticket/1-first"


# --- server accepts the new keys ---

def test_orch_state_put_accepts_switches(kanban, monkeypatch):
    import importlib
    monkeypatch.setenv("KANBAN_DIR", kanban)
    server = importlib.import_module("kanban_server")
    monkeypatch.setattr(server, "KANBAN_DIR", kanban)
    monkeypatch.setattr(server, "ensure_orchestrator_running", lambda: None)

    state, code = server.orch_state_put({"autoCommit": False, "autoPush": False})
    assert code == 200
    assert state["autoCommit"] is False
    assert state["autoPush"] is False
    assert oc.read_state(kanban)["autoCommit"] is False
