"""Ticket #39: find the agent's repo to commit even when the working tree is clean.

Sub-agents are told to create a ticket branch and COMMIT their work before they
finish (CLAUDE.md git workflow + the dispatch's commit requirements). Once they
commit, the working tree is CLEAN, so `git status --porcelain` (and therefore
`discover_changed_paths`) reports nothing. The old fallback then handed git the
non-repo workspace root and the publish died with
"fatal: not a git repository" — on nearly every non-kanban ticket.

The fix: when discovery can't name the repo, locate it by the ticket's committed
branch, and publish that branch as-is instead of re-cutting a fresh one from the
default base (which would discard the agent's commits).
"""

import os
import subprocess

import pytest

import orchestrator_core as oc
import orchestrator as orch


def _mark_repo(path):
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)


# --- locating the repo by the ticket's committed branch ---

def test_ticket_branch_in_repo_matches_agent_branch(monkeypatch):
    """The agent's branch (title-case `<id>-Feature`) is found via `git branch --list`."""
    def fake_git(cmd, *, cwd):
        assert cmd[0] == "branch"
        return "  7-Stripe-Api-Version\n"

    monkeypatch.setattr(orch, "_run_git", fake_git)
    task = {"id": "7", "title": "Stripe api version", "_board": "demo"}
    assert orch._ticket_branch_in_repo("/some/repo", task) == "7-Stripe-Api-Version"


def test_ticket_branch_in_repo_none_when_absent(monkeypatch):
    monkeypatch.setattr(orch, "_run_git", lambda cmd, *, cwd: "")
    task = {"id": "7", "title": "x", "_board": "demo"}
    assert orch._ticket_branch_in_repo("/some/repo", task) is None


def test_commit_cwd_recovers_repo_by_branch_when_tree_clean(kanban, monkeypatch):
    """Clean tree → discovery empty → repo_dir_for_paths returns the non-repo root.
    commit_cwd_for_task must recover the repo that holds the ticket's branch."""
    root = os.path.dirname(os.path.abspath(kanban))
    src = os.path.join(root, "subrepo")
    other = os.path.join(root, "B2-SF")
    _mark_repo(src)
    _mark_repo(other)
    # Agent already committed → nothing uncommitted to discover.
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: [])

    def fake_git(cmd, *, cwd):
        # Only the repo that did the work carries the ticket branch.
        if cmd[0] == "branch" and os.path.abspath(cwd) == src:
            return "7-Stripe-Fix\n"
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)
    task = {"id": "7", "title": "Stripe fix", "_board": "demo"}
    assert orch.commit_cwd_for_task(kanban, task, {}) == src


def test_publish_pushes_existing_committed_branch_as_is(kanban, monkeypatch):
    """When the agent already committed a ticket branch in the repo, publish must
    push THAT branch — never `checkout -B` a fresh one from the default base, which
    would discard the agent's commits."""
    root = os.path.dirname(os.path.abspath(kanban))
    src = os.path.join(root, "subrepo")
    _mark_repo(src)
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: [])
    monkeypatch.setattr(orch, "_default_branch_ref", lambda cwd: "main")

    calls = []

    def fake_git(cmd, *, cwd):
        calls.append(cmd)
        if cmd[0] == "branch" and os.path.abspath(cwd) == src:
            return "7-Stripe-Fix\n"
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)
    task = {"id": "7", "title": "Stripe fix", "_board": "demo"}
    result = orch.publish_output_branch(kanban, task, "ticket/7-stripe-fix")

    assert result["pushed"] is True
    assert result["branch"] == "7-Stripe-Fix", "publish the agent's committed branch"
    assert not any(c[:2] == ["checkout", "-B"] for c in calls), \
        "must NOT re-cut the branch from base — that discards the agent's commits"
    assert ["push", "-u", "origin", "7-Stripe-Fix"] in calls


# --- end-to-end: the pushed branch must actually carry the agent's commit (#46) ---

def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    """A real git repo with one commit on a `main` branch and isolated identity."""
    os.makedirs(path, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "t@t.t"], path)
    _git(["config", "user.name", "t"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    with open(os.path.join(path, "README"), "w", encoding="utf-8") as f:
        f.write("base\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "base"], path)


def test_publish_real_repo_keeps_agent_commit(tmp_path, kanban, monkeypatch):
    """Simulate an agent that branches off main, commits its work, and leaves a CLEAN
    tree. publish_output_branch must push that branch to the remote WITH the commit —
    the in-place re-cut bug (#46) would push an empty branch off base instead."""
    if not shutil_which("git"):
        pytest.skip("git not available")
    remote = str(tmp_path / "remote.git")
    src = str(tmp_path / "src")
    subprocess.run(["git", "init", "--bare", "-b", "main", remote],
                   check=True, capture_output=True, text=True)
    _init_repo(src)
    _git(["remote", "add", "origin", remote], src)
    _git(["push", "-u", "origin", "main"], src)

    # The agent branches and commits its real work, then leaves a clean tree.
    _git(["checkout", "-b", "46-Publish-Fix"], src)
    with open(os.path.join(src, "feature.txt"), "w", encoding="utf-8") as f:
        f.write("agent work\n")
    _git(["add", "-A"], src)
    _git(["commit", "-m", "agent: real work"], src)

    monkeypatch.setattr(orch, "commit_cwd_for_task", lambda kd, t, bm: src)
    task = {"id": "46", "title": "Publish fix", "_board": "demo"}
    result = orch.publish_output_branch(kanban, task, "ticket/46-publish-fix")

    assert result["pushed"] is True
    assert result["branch"] == "46-Publish-Fix"
    # The agent's file must exist on the pushed remote branch — proof the commit
    # (not an empty re-cut off main) is what got published.
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "origin/46-Publish-Fix"],
        cwd=src, check=True, capture_output=True, text=True).stdout
    assert "feature.txt" in listing


def shutil_which(name):
    import shutil
    return shutil.which(name)
