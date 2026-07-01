"""Ticket #40: per-project worktree toggle + worktree-aware commit/publish.

Two parts:
  1. Worktrees become a per-project setting (`useWorktrees` on `_meta.json`,
     toggled from the Project Settings page). When on, sub-agents work in a git
     worktree; when off, they work in place on a branch.
  2. The orchestrator must use the project's `directory` as the repo root and
     discover + publish changes made INSIDE a worktree to the right repo/branch.
"""

import json
import os

import orchestrator_core as oc
import orchestrator as orch


# --- orchestrator_core.use_worktrees (the per-project flag) ---

def test_use_worktrees_defaults_false():
    assert oc.use_worktrees({}) is False
    assert oc.use_worktrees(None) is False


def test_use_worktrees_true_when_set():
    assert oc.use_worktrees({"useWorktrees": True}) is True


def test_use_worktrees_false_when_explicitly_off():
    assert oc.use_worktrees({"useWorktrees": False}) is False


def test_use_worktrees_tolerates_stringified_booleans():
    assert oc.use_worktrees({"useWorktrees": "true"}) is True
    assert oc.use_worktrees({"useWorktrees": "false"}) is False


# --- repo root honors the project Directory ---

def test_repo_root_for_board_uses_directory(kanban, tmp_path):
    proj = tmp_path / "my-repo"
    proj.mkdir()
    meta = {"directory": str(proj)}
    assert orch.repo_root_for_board(kanban, meta) == os.path.abspath(str(proj))


def test_repo_root_for_board_falls_back_to_workspace_root(kanban):
    # No directory configured → the workspace root (parent of .AI-kanban), as before.
    assert orch.repo_root_for_board(kanban, {}) == orch._repo_root(kanban)


def test_repo_root_for_board_ignores_nonexistent_directory(kanban):
    meta = {"directory": os.path.join(kanban, "does", "not", "exist")}
    assert orch.repo_root_for_board(kanban, meta) == orch._repo_root(kanban)


# --- worktree path + detection ---

def test_worktree_path_under_repo_root(tmp_path):
    root = str(tmp_path / "repo")
    wt = orch.worktree_path(root, {"id": "40"})
    assert wt == os.path.join(os.path.abspath(root), ".claude", "worktrees", "ticket-40")


def test_is_worktree_dir_true_for_ticket_worktree():
    assert orch._is_worktree_dir(
        os.path.join("repo", ".claude", "worktrees", "ticket-40")) is True


def test_is_worktree_dir_false_for_plain_repo():
    assert orch._is_worktree_dir(os.path.join("repo", "subrepo")) is False


# --- commit cwd selection ---

def test_commit_cwd_for_task_prefers_existing_worktree(kanban, tmp_path):
    proj = tmp_path / "repo"
    wt = proj / ".claude" / "worktrees" / "ticket-7"
    wt.mkdir(parents=True)
    meta = {"directory": str(proj), "useWorktrees": True}
    task = {"id": "7", "_board": "demo"}
    assert orch.commit_cwd_for_task(kanban, task, meta) == os.path.abspath(str(wt))


def test_commit_cwd_for_task_uses_directory_when_no_worktree(kanban, tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    meta = {"directory": str(proj)}  # worktrees off
    task = {"id": "7", "_board": "demo"}
    assert orch.commit_cwd_for_task(kanban, task, meta) == os.path.abspath(str(proj))


def test_commit_cwd_for_task_falls_back_to_discovery(kanban, monkeypatch):
    # No directory + no worktree → legacy discover/repo_dir_for_paths pipeline.
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: [".AI-kanban/demo/1.json"])
    task = {"id": "1", "_board": "demo"}
    assert orch.commit_cwd_for_task(kanban, task, {}) == os.path.abspath(kanban)


# --- worktree publish: push the worktree's branch, never re-cut it ---

def test_publish_output_branch_in_worktree_pushes_current_branch(kanban, tmp_path, monkeypatch):
    proj = tmp_path / "repo"
    wt = proj / ".claude" / "worktrees" / "ticket-40"
    wt.mkdir(parents=True)
    meta = {"directory": str(proj), "useWorktrees": True}

    calls = []

    def fake_git(cmd, *, cwd):
        calls.append((cmd, cwd))
        if cmd[:2] == ["rev-parse", "--abbrev-ref"]:
            return "40-Worktree-Fix"
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)
    task = {"id": "40", "title": "Fix worktrees", "_board": "demo"}
    result = orch.publish_output_branch(kanban, task, "ticket/40-fix-worktrees", meta)

    assert result["pushed"] is True
    assert result["branch"] == "40-Worktree-Fix", "must publish the worktree's own branch"
    # git ran inside the worktree, and NEVER re-cut the branch (that would nuke work).
    assert all(cwd == os.path.abspath(str(wt)) for _, cwd in calls)
    assert not any(cmd[:2] == ["checkout", "-B"] for cmd, _ in calls), \
        "publish in a worktree must not checkout -B (the branch is already checked out)"
    assert any(cmd[:1] == ["push"] for cmd, _ in calls)


def test_publish_output_branch_non_worktree_still_cuts_branch(kanban, monkeypatch):
    # Regression: the in-place (non-worktree) flow still cuts the ticket branch.
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: ["subrepo/x.cls"])
    monkeypatch.setattr(orch, "_default_branch_ref", lambda cwd: "main")
    cmds = []
    monkeypatch.setattr(orch, "_run_git", lambda cmd, *, cwd: cmds.append(cmd) or "")
    task = {"id": "1", "title": "First", "_board": "demo"}
    orch.publish_output_branch(kanban, task, "ticket/1-first")
    assert any(c[:2] == ["checkout", "-B"] for c in cmds)


# --- _finish_completion routes worktree work to a branch publish ---

def test_finish_completion_worktree_publishes_branch(kanban, tmp_path, monkeypatch):
    proj = tmp_path / "repo"
    (proj / ".claude" / "worktrees" / "ticket-1").mkdir(parents=True)
    meta_path = os.path.join(kanban, "demo", "_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"project": "Demo", "useWorktrees": True, "directory": str(proj)}, f)

    published = []
    monkeypatch.setattr(orch, "publish_output_branch",
                        lambda kd, task, branch, *a, **k: published.append(branch) or
                        {"branch": branch, "pushed": True, "detail": "pushed"})
    committed = []
    monkeypatch.setattr(orch, "commit_to_master",
                        lambda *a, **k: committed.append(a) or {"committed": True, "detail": ""})

    task = {"id": "1", "title": "First", "_board": "demo", "comments": []}
    orch._finish_completion(kanban, task)

    assert published and published[0].startswith("ticket/1-"), \
        "worktree work must be published to its ticket branch"
    assert committed == [], "worktree (source) work must not auto-commit to master"
    assert task.get("outputBranch", "").startswith("ticket/1-")


# --- the dispatch prompt reflects the per-project worktree flag ---

def test_agent_prompt_worktrees_on_tells_agent_to_use_worktree():
    task = {"id": "40", "title": "Fix", "detail": "x", "_path": ".AI-kanban/demo/40.json"}
    profile = {"name": "backend", "systemPrompt": "be a dev"}
    prompt = orch._build_agent_prompt(task, profile, {"useWorktrees": True})
    low = prompt.lower()
    assert "worktree" in low
    assert "absolute" in low, "must tell the agent to use the absolute board path"


def test_agent_prompt_worktrees_off_tells_agent_to_work_in_place():
    task = {"id": "40", "title": "Fix", "detail": "x", "_path": ".AI-kanban/demo/40.json"}
    profile = {"name": "backend", "systemPrompt": "be a dev"}
    prompt = orch._build_agent_prompt(task, profile, {"useWorktrees": False})
    low = prompt.lower()
    assert "in place" in low or "does not use worktrees" in low
    assert "absolute" in low


def test_agent_prompt_default_no_board_meta_is_in_place():
    # Back-compat: the 2-arg call (no board_meta) must still build a valid prompt.
    task = {"id": "40", "title": "Fix", "detail": "x", "_path": ".AI-kanban/demo/40.json"}
    prompt = orch._build_agent_prompt(task, {"name": "backend", "systemPrompt": "dev"})
    assert "commitGate" in prompt  # existing guidance still present
