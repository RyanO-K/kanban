"""Ticket #36: auto-commit kanban-only work to master when requirements pass.

Kanban-board development is done directly on master (its tickets only touch
.AI-kanban/ files). When such a ticket completes and the board's natural-language
`commitRequirements` are satisfied, the orchestrator should commit the work to
the current branch (master) with a summary — instead of cutting an isolated
ticket branch. Non-kanban work keeps the existing branch-publish flow.
"""

import json
import os

import orchestrator_core as oc
import orchestrator as orch


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- orchestrator_core.read_board_meta ---

def test_read_board_meta_returns_meta(kanban):
    meta = oc.read_board_meta(kanban, "demo")
    assert meta.get("project") == "Demo"


def test_read_board_meta_missing_board_returns_empty(kanban):
    assert oc.read_board_meta(kanban, "no-such-board") == {}


# --- orchestrator_core.commit_requirements_met ---

def test_commit_requirements_met_no_requirements_passes():
    ok, reason = oc.commit_requirements_met({}, {"project": "X"})
    assert ok is True
    assert "no commit requirements" in reason.lower()


def test_commit_requirements_met_gate_satisfied():
    task = {"commitGate": {"requirementsMet": True, "summary": "all tests green"}}
    meta = {"commitRequirements": "all tests must pass"}
    ok, reason = oc.commit_requirements_met(task, meta)
    assert ok is True
    assert "all tests green" in reason


def test_commit_requirements_met_gate_not_satisfied():
    task = {"commitGate": {"requirementsMet": False, "summary": "2 tests failing"}}
    meta = {"commitRequirements": "all tests must pass"}
    ok, reason = oc.commit_requirements_met(task, meta)
    assert ok is False
    assert "2 tests failing" in reason


def test_commit_requirements_met_requirement_set_but_no_gate_reported():
    """Requirements exist but the agent never reported a commitGate → do NOT commit."""
    meta = {"commitRequirements": "all tests must pass"}
    ok, reason = oc.commit_requirements_met({}, meta)
    assert ok is False
    assert reason  # explains the missing report


# --- orchestrator.changes_are_kanban_only ---

def test_changes_are_kanban_only_true():
    paths = [".AI-kanban/kanban-dev/36.json", ".AI-kanban/orchestrator.py"]
    assert orch.changes_are_kanban_only(paths) is True


def test_changes_are_kanban_only_mixed_false():
    paths = [".AI-kanban/orchestrator.py", "src/app.py"]
    assert orch.changes_are_kanban_only(paths) is False


def test_changes_are_kanban_only_empty_false():
    assert orch.changes_are_kanban_only([]) is False


# --- orchestrator.commit_to_master (best-effort, never raises) ---

def test_commit_to_master_success(kanban, monkeypatch):
    calls = []

    def fake_git(cmd, **kwargs):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)
    task = {"id": "36", "title": "Kanban auto commit", "_board": "kanban-dev",
            "_path": os.path.join(kanban, "demo", "1.json")}
    result = orch.commit_to_master(kanban, task, "did the thing")
    assert result["committed"] is True
    # It staged and committed (no branch checkout — stays on current branch/master).
    assert ["add", "-A"] in calls
    assert any(c[:1] == ["commit"] for c in calls)
    assert not any(c[:2] == ["checkout", "-B"] for c in calls), \
        "commit_to_master must NOT cut a branch — it commits on the current branch"


def test_commit_to_master_is_best_effort_without_git(kanban, monkeypatch):
    def boom(cmd, **kwargs):
        raise orch.subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(orch, "_run_git", boom)
    task = {"id": "36", "title": "Kanban auto commit", "_board": "kanban-dev",
            "_path": os.path.join(kanban, "demo", "1.json")}
    result = orch.commit_to_master(kanban, task, "summary")
    assert result["committed"] is False
    assert result["detail"]  # non-empty explanation


# --- tick completed path: kanban-only work commits to master ---

def _set_dispatched(kanban, ticket_id, pid):
    p = os.path.join(kanban, "demo", f"{ticket_id}.json")
    t = _read(p)
    t["status"] = "in_progress"
    t["orchestrator"] = {"state": "dispatched", "pid": pid, "killRequested": False,
                         "dispatchedAt": oc.now_iso(),
                         "logFile": ".AI-kanban/_orchestrator/runs/fake.log"}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    return p


def _complete_setup(kanban, monkeypatch, pid):
    """Make ticket 1 a cleanly-exited agent WE spawned (→ 'completed' path)."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 0)
    monkeypatch.setitem(orch._PROCS, pid, object())


def test_completed_kanban_only_commits_to_master(kanban, monkeypatch):
    """A completed kanban-only ticket with met requirements commits to master and
    does NOT publish an isolated branch."""
    pid = 9001
    p = _set_dispatched(kanban, "1", pid)
    # Agent reported the commit gate passed.
    t = _read(p)
    t["commitGate"] = {"requirementsMet": True, "summary": "ran suite: 140 passed"}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    # Board requires tests pass.
    meta_path = os.path.join(kanban, "demo", "_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"project": "Demo", "commitRequirements": "all tests must pass"}, f)
    _complete_setup(kanban, monkeypatch, pid)

    # All changes are kanban-only.
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: [".AI-kanban/demo/1.json"])
    committed = []
    monkeypatch.setattr(orch, "commit_to_master",
                        lambda kd, task, summary: committed.append((str(task["id"]), summary))
                        or {"committed": True, "detail": "committed to master"})
    branch_published = []
    monkeypatch.setattr(orch, "publish_output_branch",
                        lambda *a, **k: branch_published.append(a) or
                        {"branch": "x", "pushed": True, "detail": ""})

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    assert committed and committed[0][0] == "1", "kanban-only work must commit to master"
    assert "ran suite: 140 passed" in committed[0][1], "commit summary must carry the gate summary"
    assert branch_published == [], "kanban-only work must NOT cut an isolated branch"
    assert "outputBranch" not in t1, "no output branch for kanban-only work"
    msgs = " ".join(c["message"] for c in t1.get("comments", []))
    assert "master" in msgs.lower()


def test_completed_kanban_only_gate_failed_does_not_commit(kanban, monkeypatch):
    """If commitRequirements are set but the gate failed, do NOT commit — the ticket
    still completes, and the reason is recorded for the human."""
    pid = 9002
    p = _set_dispatched(kanban, "1", pid)
    t = _read(p)
    t["commitGate"] = {"requirementsMet": False, "summary": "3 tests failing"}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    meta_path = os.path.join(kanban, "demo", "_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"project": "Demo", "commitRequirements": "all tests must pass"}, f)
    _complete_setup(kanban, monkeypatch, pid)

    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: [".AI-kanban/demo/1.json"])
    committed = []
    monkeypatch.setattr(orch, "commit_to_master",
                        lambda kd, task, summary: committed.append(task) or
                        {"committed": True, "detail": ""})

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed", "ticket still completes even when commit is gated off"
    assert committed == [], "must NOT commit when requirements are not met"
    msgs = " ".join(c["message"] for c in t1.get("comments", []))
    assert "3 tests failing" in msgs, "the gate failure reason must be recorded"


def test_completed_non_kanban_changes_still_use_branch_publish(kanban, monkeypatch):
    """Regression: a ticket that changed real source (not just .AI-kanban/) keeps the
    existing isolated-branch publish flow — auto-commit-to-master is kanban-only."""
    pid = 9003
    p = _set_dispatched(kanban, "1", pid)
    _complete_setup(kanban, monkeypatch, pid)

    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: ["src/app.py", ".AI-kanban/demo/1.json"])
    committed = []
    monkeypatch.setattr(orch, "commit_to_master",
                        lambda *a, **k: committed.append(a) or {"committed": True, "detail": ""})
    published = []
    monkeypatch.setattr(orch, "publish_output_branch",
                        lambda kd, task, branch, **k: published.append(branch) or
                        {"branch": branch, "pushed": True, "detail": "pushed"})

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    assert committed == [], "non-kanban work must NOT auto-commit to master"
    assert published and published[0].startswith("ticket/1-"), \
        "non-kanban work must keep the isolated-branch publish flow"
    assert t1.get("outputBranch", "").startswith("ticket/1-")


# --- ticket #38: commit inside the repo the changed files live in ---
#
# The workspace root (.AI-kanban's parent) is NOT a git repo — each repo is either
# `.AI-kanban` itself or a sibling sub-directory (subrepo, B2-SF, ...). Git
# must run inside the repo the changes live in, not at the (non-repo) root.

def test_repo_dir_for_paths_kanban_only_is_kanban_repo(kanban):
    """Kanban-only changes live in the `.AI-kanban` repo, so commit from there."""
    paths = [".AI-kanban/demo/1.json", ".AI-kanban/orchestrator.py"]
    assert orch.repo_dir_for_paths(kanban, paths) == os.path.abspath(kanban)


def test_repo_dir_for_paths_subrepo(kanban):
    """Changes under a sibling sub-repo commit from inside that sub-repo dir."""
    root = os.path.dirname(os.path.abspath(kanban))
    paths = ["subrepo/force-app/X.cls", "subrepo/README.md"]
    assert orch.repo_dir_for_paths(kanban, paths) == os.path.join(root, "subrepo")


def test_repo_dir_for_paths_empty_falls_back_to_workspace_root(kanban):
    """No changes / undecidable → fall back to the workspace root (prior behavior)."""
    assert orch.repo_dir_for_paths(kanban, []) == orch._repo_root(kanban)


def test_repo_dir_for_paths_multiple_subrepos_falls_back(kanban):
    """Changes spanning two different sub-repos can't pick one — fall back to root."""
    paths = ["subrepo/a.cls", "B2-SF/b.cls"]
    assert orch.repo_dir_for_paths(kanban, paths) == orch._repo_root(kanban)


def test_discover_changed_paths_aggregates_across_repos(kanban, monkeypatch):
    """The workspace root isn't a repo, so discovery asks each sub-repo and prefixes
    its porcelain paths with the repo dir name (root-relative output)."""
    root = os.path.dirname(os.path.abspath(kanban))
    # Mark .AI-kanban and a sibling sub-repo as git repos; leave a non-repo dir too.
    os.makedirs(os.path.join(kanban, ".git"), exist_ok=True)
    os.makedirs(os.path.join(root, "subrepo", ".git"), exist_ok=True)
    os.makedirs(os.path.join(root, "not-a-repo"), exist_ok=True)

    def fake_changed(cwd):
        cwd = os.path.abspath(cwd)
        if cwd == os.path.abspath(kanban):
            return ["demo/1.json"]
        if cwd == os.path.join(root, "subrepo"):
            return ["force-app/X.cls"]
        return []

    monkeypatch.setattr(orch, "changed_paths", fake_changed)
    got = set(orch.discover_changed_paths(kanban))
    assert got == {".AI-kanban/demo/1.json", "subrepo/force-app/X.cls"}


def test_commit_to_master_commits_inside_kanban_repo(kanban, monkeypatch):
    """commit_to_master must run git inside the `.AI-kanban` repo for kanban-only work,
    not at the non-repo workspace root."""
    cwds = []

    def fake_git(cmd, *, cwd):
        cwds.append(cwd)
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: [".AI-kanban/demo/1.json"])
    task = {"id": "1", "title": "First", "_board": "demo"}
    result = orch.commit_to_master(kanban, task, "did the thing")
    assert result["committed"] is True
    assert cwds, "git must have run"
    assert all(c == os.path.abspath(kanban) for c in cwds), \
        "commit must run inside the .AI-kanban repo, not the workspace root"


def test_publish_output_branch_runs_inside_subrepo(kanban, monkeypatch):
    """publish_output_branch must run git inside the sub-repo the changes live in."""
    root = os.path.dirname(os.path.abspath(kanban))
    cwds = []

    def fake_git(cmd, *, cwd):
        cwds.append(cwd)
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)
    monkeypatch.setattr(orch, "_default_branch_ref", lambda cwd: "main")
    monkeypatch.setattr(orch, "discover_changed_paths", lambda kd: ["subrepo/x.cls"])
    task = {"id": "1", "title": "First", "_board": "demo"}
    orch.publish_output_branch(kanban, task, "ticket/1-first")
    assert cwds, "git must have run"
    assert all(c == os.path.join(root, "subrepo") for c in cwds), \
        "branch publish must run inside the sub-repo, not the workspace root"


def test_agent_prompt_teaches_commit_gate():
    """The dispatch prompt must tell the agent to report a commitGate when the board
    has commit requirements, so the orchestrator can gate the auto-commit."""
    task = {"id": "9", "title": "Do a thing", "detail": "x", "_path": ".AI-kanban/demo/9.json"}
    profile = {"name": "backend", "systemPrompt": "be a dev"}
    prompt = orch._build_agent_prompt(task, profile)
    low = prompt.lower()
    assert "commitgate" in low
    assert "requirementsmet" in low
    assert "commit requirement" in low
