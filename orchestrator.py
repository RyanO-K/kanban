"""Kanban orchestrator runtime: the tick loop + real process management.

Decision logic lives in orchestrator_core. This module wires it to real
`claude -p` subprocesses, OS process kills, and a sleep loop. The dispatch
and process-liveness calls are module-level functions so tests can monkeypatch
them without launching real processes.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid

import orchestrator_core as oc

META_FILE = "_meta.json"
TICK_SECONDS = 60
SKIP_DIRS = {"config", "_orchestrator", "__pycache__", "tests"}

# Registry of Popen objects for processes we spawned.
# Maps pid (int) -> Popen so we can read real exit codes on reap.
_PROCS = {}


# --- ticket IO across boards ---

def load_all_tasks(kanban_dir):
    tasks = []
    for entry in sorted(os.scandir(kanban_dir), key=lambda e: e.name):
        if not entry.is_dir() or entry.name in SKIP_DIRS:
            continue
        if not os.path.isfile(os.path.join(entry.path, META_FILE)):
            continue
        for fe in os.scandir(entry.path):
            if fe.is_file() and fe.name.endswith(".json") and fe.name != META_FILE:
                try:
                    with open(fe.path, "r", encoding="utf-8") as f:
                        task = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue
                task["_board"] = entry.name
                task["_path"] = fe.path
                tasks.append(task)
    return tasks


def write_task(path, task):
    """Atomically write a task JSON file via temp-file + os.replace."""
    out = {k: v for k, v in task.items() if not k.startswith("_")}
    dir_ = os.path.dirname(path)
    # Write to a sibling temp file then atomically replace — avoids partial writes.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _reread_task(task):
    """Re-read a ticket from disk, preserving the injected `_path`/`_board`.

    The tick loads every ticket once at the top; by the time we reap one, a
    sub-agent may have written fresh comments/question/commitGate to disk. Acting
    on (and writing back) the stale snapshot would clobber those. Re-reading here
    means the orchestrator's terminal mutations (status/history/marker/its own
    comments) are layered onto the sub-agent's latest state. Mirrors the
    re-read-before-write pattern in the server's orch_kill/orch_answer.
    (Ticket #42.) Returns the original task unchanged if the file can't be read.
    """
    path = task.get("_path")
    try:
        with open(path, "r", encoding="utf-8") as f:
            fresh = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError):
        return task
    fresh["_path"] = path
    fresh["_board"] = task.get("_board")
    return fresh


# --- idle-tracking sidecar (ticket #42) ------------------------------------
#
# A 'running' agent's tick used to rewrite its whole ticket every ~60s purely to
# persist logSize/lastGrowthAt (idle-stall tracking) on the orchestrator marker —
# clobbering any comment/question/commitGate the live sub-agent wrote in between.
# That bookkeeping is orchestrator-private and belongs nowhere near the ticket,
# so it lives in a per-ticket sidecar under _orchestrator/idle/ instead. The
# ticket file is now only written when the orchestrator has a real ownership
# change to record (dispatch/reap/status), and those go through _reread_task.

def _idle_dir(kanban_dir):
    return os.path.join(kanban_dir, "_orchestrator", "idle")


def _idle_path(kanban_dir, board, task_id):
    return os.path.join(_idle_dir(kanban_dir), f"{board}__{task_id}.json")


def read_idle(kanban_dir, board, task_id):
    """Return the {logSize, lastGrowthAt} idle record for a ticket, or {}."""
    try:
        with open(_idle_path(kanban_dir, board, task_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def write_idle(kanban_dir, board, task_id, idle):
    """Persist a ticket's idle record atomically (sidecar, not the ticket)."""
    os.makedirs(_idle_dir(kanban_dir), exist_ok=True)
    path = _idle_path(kanban_dir, board, task_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idle, f)
    os.replace(tmp, path)


def clear_idle(kanban_dir, board, task_id):
    """Drop a ticket's idle record once it is no longer in flight."""
    try:
        os.remove(_idle_path(kanban_dir, board, task_id))
    except OSError:
        pass


def _add_history(task, frm, to):
    task.setdefault("history", []).append({
        "action": "status_change", "from": frm, "to": to, "timestamp": oc.now_iso(),
    })


def _add_comment(task, message):
    task.setdefault("comments", []).append({
        "writer": "Orchestrator", "message": message, "timestamp": oc.now_iso(),
    })


# --- process management (mockable seams) ---

def _process_alive(pid):
    if not pid:
        return False
    # Fast path: if we have the Popen object, poll it directly.
    if pid in _PROCS:
        return _PROCS[pid].poll() is None
    # Fallback for processes not in our registry (e.g. loop restart, external pids).
    try:
        if sys.platform == "win32":
            # Use CSV output with exact PID filter; check that the second quoted field
            # matches exactly to avoid PID 12 matching inside 123.
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True,
            )
            # Each CSV row has fields like: "image.exe","pid","..."
            # We look for the exact quoted pid string as a CSV field.
            quoted_pid = f'"{pid}"'
            for line in out.stdout.splitlines():
                fields = line.split(",")
                if len(fields) >= 2 and fields[1].strip() == quoted_pid:
                    return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def kill_pid(pid):
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            # /T kills the whole descendant tree. `claude -p` spawns node/MCP/tool
            # children; without /T they are orphaned and keep consuming CPU/RAM.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        else:
            # Agents are spawned with start_new_session=True (their own process
            # group), so signal the whole group to take the children down too.
            try:
                os.killpg(os.getpgid(pid), 15)
            except (OSError, AttributeError):
                os.kill(pid, 15)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _release_proc(pid):
    """Close the log handle and remove pid from the registry.

    Call this whenever we are done with a dispatched process (killed, reaped,
    crashed, completed, needs_human, stop-all).  Safe to call even if the pid
    is not in the registry (no-op).
    """
    p = _PROCS.pop(pid, None)
    if p is not None:
        log_f = getattr(p, "_log_f", None)
        if log_f is not None:
            try:
                log_f.close()
            except OSError:
                pass


def _exit_code(pid):
    """Return the true exit code for a dead process we spawned.

    If the Popen is in our registry, poll it (ensure reaped) and return its
    real returncode (may be non-zero or negative for crashes).  Also releases
    the log handle and removes from registry.

    If the pid is NOT in our registry (loop restarted, process wasn't ours),
    return None — meaning "exit status unknown".  reap_decision treats
    None (not == 0) as crashed, so unknown-exit dead processes are routed to
    the crashed path rather than silently completing.  This is the correct
    behaviour after a restart: we cannot confirm a clean exit, so we err on
    the side of requiring human attention.
    """
    if pid in _PROCS:
        p = _PROCS[pid]
        p.poll()  # ensure reaped
        code = p.returncode
        _release_proc(pid)
        return code
    return None  # unknown pid → treat as crashed (not 0)


# --- output-branch publishing (mockable seams) ---

def _repo_root(kanban_dir):
    """The workspace root: the parent of .AI-kanban.

    NOTE: this directory is NOT itself a git repo — each repo is either `.AI-kanban`
    itself or a sibling sub-directory (a checked-out Salesforce repo, etc.). It is
    only the fallback cwd when the changed files don't identify a single repo; the
    actual commit/publish cwd is `repo_dir_for_paths`.
    """
    kanban_dir = os.path.abspath(kanban_dir)
    return os.path.dirname(kanban_dir) or kanban_dir


def repo_dir_for_paths(kanban_dir, paths):
    """The git repo directory a set of changed paths lives in — where git must run.

    The workspace root holds many independent repos, so git can't run there. We
    derive the repo from the changed files (paths are workspace-root-relative, as
    `git status --porcelain` would report them relative to the root):

      - All under `.AI-kanban/`  -> the `.AI-kanban` directory (its own repo; this is the
        "kanban lives on master" tree).
      - All under one sibling top-level dir (e.g. `subrepo/...`) -> that
        sub-repo's directory: we `cd` into the checked-out repo and commit there.
      - Empty, or spanning two different sub-repos (can't pick one) -> fall back to
        the workspace root (prior behavior; the best-effort git call just no-ops on
        a non-repo rather than committing the wrong tree).
    """
    kanban_dir = os.path.abspath(kanban_dir)
    root = _repo_root(kanban_dir)
    tops = set()
    for p in paths or []:
        norm = p.replace("\\", "/").lstrip("/")
        if not norm:
            continue
        top = norm.split("/", 1)[0]
        tops.add(top)
    if len(tops) != 1:
        return root  # nothing changed, or multiple repos — can't pick one
    top = tops.pop()
    if top == ".AI-kanban":
        return kanban_dir
    return os.path.join(root, top)


def repo_root_for_board(kanban_dir, board_meta):
    """The git repo root for a board's work — its configured project `directory`.

    Ticket #40: the Project Settings page sets a per-project `directory`; that
    directory IS the project's git repo and is the repo root for worktree creation
    and commits. When unset (or missing on disk) we fall back to the workspace root
    (parent of .AI-kanban), preserving the multi-sibling-repo discovery behavior.
    """
    directory = (board_meta or {}).get("directory")
    if directory and os.path.isdir(directory):
        return os.path.abspath(directory)
    return _repo_root(kanban_dir)


def worktree_path(repo_root, task):
    """The per-ticket worktree directory under a repo root: `.claude/worktrees/ticket-<id>`.

    Mirrors the CLAUDE.md fallback (`git worktree add .claude/worktrees/ticket-<id>`),
    so the orchestrator looks for the agent's worktree in the same place the agent
    creates it. Worktrees live under the repo's `.claude/` directory (ticket #40).
    """
    return os.path.join(os.path.abspath(repo_root), ".claude", "worktrees",
                        f"ticket-{task.get('id')}")


def _is_worktree_dir(path):
    """True if `path` is one of our per-ticket worktrees (`.claude/worktrees/ticket-<id>`).

    Used by publish to tell a worktree (whose branch is already checked out with the
    agent's changes) apart from a plain repo dir (where we must cut the ticket branch).
    """
    path = os.path.normpath(path)
    parent = os.path.dirname(path)
    return (os.path.basename(parent) == "worktrees"
            and os.path.basename(os.path.dirname(parent)) == ".claude"
            and os.path.basename(path).startswith("ticket-"))


def _is_git_repo(path):
    """True if `path` is a git repo (has a `.git` entry). Mirrors the `.git` check
    `discover_changed_paths` uses, so the two agree on what counts as a repo."""
    return os.path.exists(os.path.join(path, ".git"))


def _ticket_branch_in_repo(repo_dir, task):
    """The local branch in `repo_dir` that holds this ticket's committed work, else None.

    An agent works on a ticket branch and COMMITS there before finishing, leaving a
    CLEAN working tree (CLAUDE.md git workflow + the dispatch's commit requirements).
    Once committed, the branch is the only way to find the output. Match both the
    agent's title-case `<id>-Feature-Name` convention and the canonical
    `ticket/<id>-<slug>` publish name. Best-effort — git missing / not a repo → None.
    """
    tid = str(task.get("id", ""))
    if not tid:
        return None
    try:
        out = _run_git(["branch", "--list", f"{tid}-*", f"ticket/{tid}-*",
                        oc.branch_name(task)], cwd=repo_dir)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None
    for line in out.splitlines():
        # `git branch --list` marks the current branch with a leading "* ".
        name = line.replace("*", "", 1).strip()
        if name:
            return name
    return None


def _repo_with_ticket_branch(kanban_dir, task):
    """A workspace repo (sibling dir or `.AI-kanban`) that already holds the ticket's
    committed branch, else None.

    The recovery for a clean working tree: change discovery finds nothing (the agent
    already committed), so we ask each repo whether it carries the ticket's branch and
    return the first that does — that is the repo whose output must be published.
    """
    root = _repo_root(kanban_dir)
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return None
    for name in entries:
        sub = os.path.join(root, name)
        if not _is_git_repo(sub):
            continue
        if _ticket_branch_in_repo(sub, task):
            return sub
    return None


def commit_cwd_for_task(kanban_dir, task, board_meta):
    """The directory git must run in to capture a ticket's working-tree output.

    Resolution order (ticket #40):
      1. If the board uses worktrees and the ticket's worktree exists, that worktree
         — it holds the agent's checked-out branch + changes — is where git runs.
      2. Else the configured project `directory` (the repo root), if set on disk.
      3. Else the legacy discover/`repo_dir_for_paths` pipeline (multi-sibling-repo
         workspace with no per-project directory configured).

    Ticket #39: step 3 falls back to the NON-repo workspace root when discovery is
    undecidable — most commonly because the agent already committed its work and left
    a clean tree, so `git status` reports nothing. Running git there fails with
    "fatal: not a git repository". When the resolved dir isn't a repo, recover it from
    the ticket's committed branch before giving up on the root.
    """
    root = repo_root_for_board(kanban_dir, board_meta)
    if oc.use_worktrees(board_meta):
        wt = worktree_path(root, task)
        if os.path.isdir(wt):
            return wt
    directory = (board_meta or {}).get("directory")
    if directory and os.path.isdir(directory):
        return root
    cwd = repo_dir_for_paths(kanban_dir, discover_changed_paths(kanban_dir))
    if not _is_git_repo(cwd):
        recovered = _repo_with_ticket_branch(kanban_dir, task)
        if recovered:
            return recovered
    return cwd


_GIT_TIMEOUT = 120  # seconds — matches the model-call timeout


def _run_git(cmd, *, cwd, timeout=_GIT_TIMEOUT):
    """Run a git command in `cwd`, returning stdout. Raises on non-zero exit.

    A thin, mockable wrapper around subprocess so publish_output_branch's git
    sequence can be tested without a real repo (mirrors the kill_pid /
    _process_alive seams).

    timeout: seconds before subprocess.TimeoutExpired is raised (default 120).
    GIT_TERMINAL_PROMPT=0 and GIT_ASKPASS are injected so a headless push with
    no cached creds fails fast instead of blocking forever on a credential prompt.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}
    out = subprocess.run(["git"] + cmd, cwd=cwd, capture_output=True,
                         text=True, check=True, timeout=timeout, env=env)
    return out.stdout.strip()


def _default_branch_ref(cwd):
    """The ref a ticket branch should be cut from: the repo's DEFAULT branch.

    A ticket branch must start from the default branch (e.g. main), NOT from
    whatever the orchestrator happens to have checked out — otherwise each
    ticket inherits the unrelated work of the previous ticket's branch.

    Resolution order, best-effort:
      1. `origin/HEAD` (the remote's default), e.g. -> `origin/main`.
      2. A local `main` / `master`.
    Returns the start-point ref string, or None if none can be resolved (in
    which case the caller falls back to the current HEAD, preserving old
    behaviour rather than failing).
    """
    # 1. Whatever origin advertises as its default branch.
    try:
        ref = _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
        # e.g. "refs/remotes/origin/main" -> "origin/main"
        name = ref.split("refs/remotes/", 1)[-1] if ref else ""
        if name:
            return name
    except (subprocess.CalledProcessError, OSError, ValueError):
        pass
    # 2. A conventional local default branch.
    for name in ("main", "master"):
        try:
            _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=cwd)
            return name
        except (subprocess.CalledProcessError, OSError, ValueError):
            continue
    return None


def publish_output_branch(kanban_dir, task, branch, board_meta=None, push=True):
    """Commit the agent's working-tree output to `branch` and push it. Best-effort.

    A ticket's output is hard to find unless it lives on a known branch. When an
    agent completes we move whatever it changed onto `ticket/<id>-<slug>` and push
    it, so the branch (recorded on the ticket) tells a human exactly where the
    output is.

    Git runs in the repo the work lives in (`commit_cwd_for_task`): the ticket's
    worktree when the board uses worktrees, else the project `directory`, else the
    discovered sub-repo. Two modes:

    - **Worktree** (ticket #40): the worktree already has the ticket's OWN branch
      checked out, carrying the agent's changes. Re-cutting from the default branch
      would discard that work, so we commit + push the worktree's current branch.
    - **In place**: cut the ticket branch off the repo's DEFAULT branch (see
      `_default_branch_ref`), not the currently checked-out branch, so each ticket's
      output is isolated rather than stacked on the previous ticket's work.

    This NEVER raises: a missing git, a non-repo tree, nothing to commit, or no
    remote must not block the ticket's completion. Returns
    `{"branch", "pushed": bool, "detail": str}` describing the outcome for the
    human-facing comment.
    """
    if board_meta is None:
        board_meta = oc.read_board_meta(kanban_dir, task.get("_board"))
    cwd = commit_cwd_for_task(kanban_dir, task, board_meta)
    msg = f"ticket #{task.get('id')}: {task.get('title','')}"
    try:
        if _is_worktree_dir(cwd):
            # The agent's worktree already has its branch checked out — publish it
            # as-is. Read the actual branch name so the ticket records where the
            # output really landed (the agent may name it `<id>-Feature`, not the
            # `ticket/<id>-<slug>` convention).
            try:
                current = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
            except subprocess.CalledProcessError:
                current = ""
            current = current or branch
            _run_git(["add", "-A"], cwd=cwd)
            try:
                _run_git(["commit", "-m", msg], cwd=cwd)
            except subprocess.CalledProcessError:
                pass  # nothing new to commit — still publish the branch ref
            if not push:
                return {"branch": current, "pushed": False,
                        "detail": "committed locally; push skipped"}
            _run_git(["push", "-u", "origin", current], cwd=cwd)
            return {"branch": current, "pushed": True,
                    "detail": f"pushed to origin/{current}"}
        # In-place. If the agent already committed its work on a ticket branch, the
        # tree is clean and that branch IS the output — push it as-is (like the
        # worktree case). Re-cutting from the default base would discard the agent's
        # commits, so only fall through to the cut-a-fresh-branch path when no such
        # branch exists (the agent left uncommitted changes). (Ticket #39.)
        existing = _ticket_branch_in_repo(cwd, task)
        if existing:
            try:
                _run_git(["checkout", existing], cwd=cwd)
            except subprocess.CalledProcessError:
                pass
            _run_git(["add", "-A"], cwd=cwd)
            try:
                _run_git(["commit", "-m", msg], cwd=cwd)
            except subprocess.CalledProcessError:
                pass  # nothing new beyond what the agent already committed
            if not push:
                return {"branch": existing, "pushed": False,
                        "detail": "committed locally; push skipped"}
            _run_git(["push", "-u", "origin", existing], cwd=cwd)
            return {"branch": existing, "pushed": True,
                    "detail": f"pushed to origin/{existing}"}
        # No existing ticket branch: create one off the default branch (falling back
        # to current HEAD if it can't be resolved), carrying the working-tree changes.
        base = _default_branch_ref(cwd)
        _run_git(["checkout", "-B", branch] + ([base] if base else []), cwd=cwd)
        _run_git(["add", "-A"], cwd=cwd)
        try:
            _run_git(["commit", "-m", msg], cwd=cwd)
        except subprocess.CalledProcessError:
            pass  # nothing to commit — still try to publish the branch ref
        if not push:
            return {"branch": branch, "pushed": False,
                    "detail": "committed locally; push skipped"}
        _run_git(["push", "-u", "origin", branch], cwd=cwd)
        return {"branch": branch, "pushed": True,
                "detail": f"pushed to origin/{branch}"}
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() or f"git exited {e.returncode}"
        return {"branch": branch, "pushed": False, "detail": detail}
    except (OSError, ValueError) as e:
        # git not installed / not a repo / bad cwd — record and move on.
        return {"branch": branch, "pushed": False, "detail": f"git unavailable: {e}"}


def _parse_porcelain(out):
    """Parse `git status --porcelain` output into a list of changed paths."""
    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # Porcelain: "XY <path>" (XY = 2 status chars). A rename is "R  old -> new".
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip().strip('"'))
    return paths


def changed_paths(cwd):
    """Repo-relative paths the working tree at `cwd` has changed (best-effort, never raises).

    Uses `git status --porcelain`, which lists staged, unstaged, and untracked
    changes (porcelain format: a 2-char status code, a space, then the path). Returns
    `[]` when git is unavailable / not a repo / nothing changed — the caller treats an
    empty list as "no changes", so a missing git never forces a wrong branch decision.

    Runs via subprocess directly (not the strip()-ing `_run_git`) so the porcelain
    status columns — whose first column is often a space (e.g. " M path") — are not
    corrupted by a leading-whitespace strip that would shift the first line's parse.
    """
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError, ValueError):
        return []
    return _parse_porcelain(out)


def discover_changed_paths(kanban_dir):
    """Workspace-root-relative changed paths across every repo under the workspace.

    The workspace root is NOT a single git repo — `.AI-kanban` and each sibling
    top-level directory is an independent repo (a checked-out Salesforce repo, etc.).
    A plain `git status` at the root therefore reports nothing. So we ask each
    candidate repo directly and prefix its `git status --porcelain` paths with the
    repo's top-level directory name, yielding root-relative paths in the same shape
    the rest of the pipeline (`changes_are_kanban_only`, `repo_dir_for_paths`)
    already expects. Best-effort: non-repo / unreadable dirs are skipped silently.
    """
    root = _repo_root(kanban_dir)
    kanban_name = os.path.basename(os.path.abspath(kanban_dir))  # ".AI-kanban"
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for name in entries:
        sub = os.path.join(root, name)
        if not os.path.isdir(os.path.join(sub, ".git")):
            continue  # only directories that are git repos
        for rel in changed_paths(sub):
            out.append(f"{name}/{rel}")
    # `os.path.basename(.AI-kanban)` is ".AI-kanban"; its changes are reported as
    # ".AI-kanban/<rel>", matching changes_are_kanban_only's prefix check.
    return out


def changes_are_kanban_only(paths):
    """True iff there is at least one change and every changed path is under `.AI-kanban/`.

    This is the deterministic trigger for committing to master rather than cutting an
    isolated ticket branch: CLAUDE.md mandates that `.AI-kanban/` files always land on
    master and only non-kanban source needs a branch. An empty change set is False
    (nothing to commit — neither path applies).
    """
    if not paths:
        return False
    return all(p.replace("\\", "/").startswith(".AI-kanban/") for p in paths)


def commit_to_master(kanban_dir, task, summary):
    """Stage + commit the agent's work on the CURRENT branch (master). Best-effort.

    Kanban-board development happens directly on master, so a completed kanban-only
    ticket is committed in place (no branch checkout) with a self-describing message:
    a `ticket #<id>: <title>` subject (matching the branch-publish convention) and the
    agent's verification `summary` as the body. Never raises — a missing git / non-repo
    tree / nothing-to-commit is
    reported, not thrown, so completion is never wedged. Returns
    `{"committed": bool, "detail": str}`.
    """
    cwd = repo_dir_for_paths(kanban_dir, discover_changed_paths(kanban_dir))
    subject = f"ticket #{task.get('id')}: {task.get('title','')}".strip()
    message = f"{subject}\n\n{summary}" if summary else subject
    try:
        _run_git(["add", "-A"], cwd=cwd)
        try:
            _run_git(["commit", "-m", message], cwd=cwd)
        except subprocess.CalledProcessError:
            return {"committed": False, "detail": "nothing to commit"}
        return {"committed": True, "detail": "committed to master"}
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() or f"git exited {e.returncode}"
        return {"committed": False, "detail": detail}
    except (OSError, ValueError) as e:
        return {"committed": False, "detail": f"git unavailable: {e}"}


def _finish_completion(kanban_dir, task):
    """Decide how a completed ticket's output is recorded: auto-commit to master for
    kanban-only work, else the existing isolated-branch publish.

    Kanban-board work (changes entirely under `.AI-kanban/`) is committed directly to
    master with a summary, gated by the board's `commitRequirements` (the agent's
    `commitGate` report). Everything else keeps the isolated `ticket/<id>-<slug>`
    branch flow unchanged. Every outcome is recorded in a comment for the human.
    """
    board_meta = oc.read_board_meta(kanban_dir, task.get("_board"))
    # Global kill switch (ticket #55): a user who wants to review diffs first can turn
    # off auto-commit entirely. When off we still complete the ticket but skip ALL git
    # mutation, recording the pending output (changed paths + would-be branch) so the
    # human knows what's waiting in the working tree. `autoPush` (commit but don't push)
    # is threaded into the branch-publish seams below.
    state = oc.read_state(kanban_dir)
    if not oc.auto_commit_enabled(state):
        _record_pending_output(kanban_dir, task)
        return
    push = oc.auto_push_enabled(state)
    # Worktree (source) work lives on the ticket's own branch INSIDE the worktree —
    # it isn't visible to the root-level change discovery (which only knows the
    # sibling repos), so route it straight to a gated branch publish. (Ticket #40.)
    if oc.use_worktrees(board_meta) and os.path.isdir(
            worktree_path(repo_root_for_board(kanban_dir, board_meta), task)):
        ok, reason = oc.commit_requirements_met(task, board_meta)
        if not ok:
            _add_comment(task, f"Not published — commit requirements not met: {reason}. "
                               f"Output remains in the worktree.")
            return
        _record_output_branch(kanban_dir, task, board_meta, push=push)
        return
    paths = discover_changed_paths(kanban_dir)
    if not changes_are_kanban_only(paths):
        _record_output_branch(kanban_dir, task, board_meta, push=push)
        return
    ok, reason = oc.commit_requirements_met(task, board_meta)
    if not ok:
        _add_comment(task, f"Not auto-committed to master — commit requirements "
                           f"not met: {reason}. Output remains in the working tree.")
        return
    result = commit_to_master(kanban_dir, task, reason)
    if result.get("committed"):
        _add_comment(task, f"Auto-committed to master ({result.get('detail','')}). {reason}")
    else:
        _add_comment(task, f"Could not auto-commit to master: {result.get('detail','')}. "
                           f"Output remains in the working tree.")


def _record_pending_output(kanban_dir, task):
    """Record a completed ticket's uncommitted output when auto-commit is off.

    The global `autoCommit` kill switch (ticket #55) lets a user dispatch work but
    review diffs before anything is committed or pushed. The ticket still completes;
    we leave a comment naming the changed paths and the branch the work WOULD publish
    to, so the human can find and review it in the working tree. Best-effort: path
    discovery never raises (an empty list just means "nothing detected").
    """
    paths = discover_changed_paths(kanban_dir)
    branch = oc.branch_name(task)
    changed = ", ".join(paths) if paths else "(none detected)"
    _add_comment(task,
                 "Auto-commit/push disabled (global kill switch) — output left "
                 "uncommitted in the working tree for review. "
                 f"Changed paths: {changed}. Would publish to branch `{branch}`.")


def _record_output_branch(kanban_dir, task, board_meta=None, push=True):
    """Publish the ticket's output branch and record it on the ticket.

    Sets a top-level `outputBranch` field (machine-readable, for the board UI) and
    appends a comment naming the branch and whether the push succeeded, so the
    output's location is visible both on the ticket and in its comment thread.
    Best-effort: any failure is recorded, never raised, so completion proceeds.

    For a worktree the published branch is whatever the agent checked out there, so
    we record the branch `publish_output_branch` actually pushed, not the convention.

    `board_meta` is accepted for callers that already have it, but is NOT forwarded
    positionally — `publish_output_branch` reads it from the task's board itself when
    omitted, which keeps the publish seam a stable 3-arg call for existing callers.
    """
    branch = oc.branch_name(task)
    result = publish_output_branch(kanban_dir, task, branch, push=push)
    branch = result.get("branch", branch)
    task["outputBranch"] = branch
    if result.get("pushed"):
        _add_comment(task, f"Output pushed to branch `{branch}` ({result.get('detail','')}).")
    elif not push:
        # autoPush off: the work was committed locally but deliberately not pushed.
        _add_comment(task, f"Output committed to branch `{branch}` but not pushed "
                           f"(auto-push disabled): {result.get('detail','')}.")
    else:
        _add_comment(task, f"Output branch `{branch}` could not be pushed: "
                           f"{result.get('detail','')}. Output remains in the working tree.")


def _claude_cmd():
    """Resolve the `claude` executable to a full path. On Windows the CLI is
    often a .cmd/.exe shim that bare Popen can't launch, so prefer an explicit
    path from PATH; fall back to the bare name."""
    return shutil.which("claude") or "claude"


def spawn_agent(kanban_dir, board, task, profile, model):
    # Resolve to an absolute path so the run dir and cwd are always valid.
    # (os.path.dirname(".") is "" — an invalid cwd that raises WinError 123.)
    kanban_dir = os.path.abspath(kanban_dir)
    runs_dir = os.path.join(kanban_dir, "_orchestrator", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    ts = oc.now_iso().replace(":", "").replace("-", "")
    log_name = f"{task['id']}-{ts}.log"
    log_path = os.path.join(runs_dir, log_name)

    # The sub-agent runs from the repo root (parent of .AI-kanban) so it can see
    # the .AI-kanban/<board>/<id>.json paths in its prompt. Fall back to the
    # kanban dir itself if there is no parent.
    cwd = os.path.dirname(kanban_dir) or kanban_dir

    # Mint the sub-agent's session id up front and pass it to the CLI with
    # --session-id, rather than scraping it from stdout. The orchestrator then
    # knows the id deterministically and records it on the ticket, so a human can
    # `claude --resume <id>` to take over a blocked/stuck ticket manually.
    session_id = str(uuid.uuid4())

    board_meta = oc.read_board_meta(kanban_dir, board)
    prompt = _build_agent_prompt(task, profile, board_meta)
    cmd = [_claude_cmd(), "-p", prompt, "--session-id", session_id,
           "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    allowed = profile.get("allowedTools")
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]

    log_f = open(log_path, "w", encoding="utf-8")
    # On POSIX, put the agent in its own session/process group so kill_pid can
    # take down the whole child tree via os.killpg (Windows uses taskkill /T).
    popen_kw = {} if sys.platform == "win32" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=cwd,
                            **popen_kw)
    # Stash log handle on proc so _release_proc can close it on reap.
    proc._log_f = log_f
    # Register the Popen so _exit_code / _process_alive can use it.
    _PROCS[proc.pid] = proc
    return {
        "state": "dispatched",
        "profile": profile.get("name"),
        "model": model,
        "pid": proc.pid,
        "sessionId": session_id,
        "dispatchedAt": oc.now_iso(),
        "killRequested": False,
        "logFile": f".AI-kanban/_orchestrator/runs/{log_name}",
    }


def _build_agent_prompt(task, profile, board_meta=None):
    parts = [profile.get("systemPrompt", "You are working a kanban ticket.")]
    parts.append(f"\nTicket #{task['id']}: {task.get('title','')}")
    if task.get("detail"):
        parts.append(f"\nDetail:\n{task['detail']}")
    parts.append(f"\nTicket file: {task.get('_path','')}")
    marker = oc.get_marker(task) or {}
    q = marker.get("question")
    if q and q.get("answer"):
        parts.append(
            f"\nPrior question: {q.get('prompt')}\n"
            f"Human answer value: {q['answer'].get('value')}\n"
            f"Human notes (authoritative, overrides the question if it was wrong): "
            f"{q['answer'].get('notes')}"
        )
    parts.append(
        "\nWhen done, append a summary comment to the ticket JSON's `comments` "
        "(writer 'Claude')."
    )
    parts.append(_git_workflow_guidance(task, board_meta))
    parts.append(_COMMIT_GATE_GUIDANCE)
    parts.append(_HUMAN_INPUT_GUIDANCE)
    return "\n".join(parts)


def _git_workflow_guidance(task, board_meta):
    """Per-project git-workflow guidance handed to a sub-agent (ticket #40).

    Two things the agent must get right when it might be in a git worktree:

    - **Board path anchoring:** a worktree moves the agent's cwd away from the
      workspace root, so a cwd-relative `.AI-kanban/...` path no longer resolves. The
      agent must read/edit its ticket JSON, `_meta.json`, and skills via ABSOLUTE
      paths (the ticket file above is absolute).
    - **Worktree vs in place:** the board's `useWorktrees` flag decides whether the
      agent isolates its code changes in a worktree or works in place on a branch.
    """
    board_meta = board_meta or {}
    directory = board_meta.get("directory") or ""
    repo_hint = f" The project's repo root is `{directory}`." if directory else ""
    board_anchor = (
        "\nBOARD PATH: a git worktree moves your working directory away from the "
        "workspace root, so always read and edit your ticket JSON, `_meta.json`, and "
        "any `.AI-kanban/` skills via their ABSOLUTE paths (your ticket file path above "
        "is absolute) — never a cwd-relative `.AI-kanban/...` path."
    )
    if oc.use_worktrees(board_meta):
        wt = f".claude/worktrees/ticket-{task.get('id')}"
        return (board_anchor +
                "\nGIT WORKFLOW: this project uses worktrees. Isolate your code changes "
                f"in a git worktree.{repo_hint} If the EnterWorktree tool is available, "
                f"prefer it; otherwise run `git worktree add {wt} -b <id>-Feature-Name` "
                "from the repo root (ensure `.claude/worktrees/` is gitignored). Commit "
                "your changes in the worktree when done.")
    return (board_anchor +
            "\nGIT WORKFLOW: this project does NOT use worktrees — work in place on a "
            f"branch. Run `git checkout -b <id>-Feature-Name` in the repo root{repo_hint} "
            "and commit there when done. Do NOT create a git worktree.")


# Commit-gate guidance. When a board sets `commitRequirements` (a natural-language
# statement of what must hold before committing, e.g. "all tests must pass"), the
# orchestrator auto-commits kanban-only work to master ONLY if the agent reports the
# requirements were met. The agent is the only thing that actually runs the tests, so
# it reports the outcome by writing a `commitGate` object on the ticket. Without this
# report the orchestrator will NOT commit (it won't commit on an unverified gate).
_COMMIT_GATE_GUIDANCE = (
    "\nCOMMIT REQUIREMENTS: this board may set a free-text `commitRequirements` field "
    "in its `_meta.json` stating what must hold before your work is committed (e.g. "
    "\"all tests must pass\"). If it is set, satisfy it (run the tests), then record the "
    "outcome on the ticket JSON as a top-level `commitGate` object so the orchestrator "
    "can commit your work:\n"
    "  \"commitGate\": { \"requirementsMet\": true, \"summary\": \"<what you ran/verified>\" }\n"
    "Set `requirementsMet` to false (with a `summary` of what failed) if you could not "
    "satisfy them — the orchestrator will then NOT commit and will leave the work in the "
    "tree. If the board has no `commitRequirements`, no commitGate is needed."
)


# Escalation guidance handed to every sub-agent. The mechanism (block + write
# `orchestrator.question`, reaped to `needs_human`, surfaced in the dashboard
# inbox, answered, auto re-dispatched) has existed but gone unused because the
# old prompt only NAMED the object without its shape — so agents never produced
# a question the dashboard could render or the reaper could detect. This spells
# out WHEN to escalate and the exact JSON shape, matching `orchestrator_core.
# build_question` (what the answer endpoint + `questionCard` UI consume).
_HUMAN_INPUT_GUIDANCE = (
    "\nIF YOU GET BLOCKED — you are missing a decision, credential, or fact only "
    "a human has, or the ticket is ambiguous and you cannot safely proceed — do "
    "NOT guess and do NOT silently quit. Escalate for human input instead:\n"
    "  1. Set the ticket `status` to \"blocked\".\n"
    "  2. Add an `orchestrator.question` object to the ticket JSON, then stop.\n"
    "A human answers it in the orchestrator dashboard and the orchestrator "
    "automatically re-dispatches you with their answer.\n"
    "The question shape (leave `answer` and `answeredAt` null — the human fills "
    "them):\n"
    "  Free text / number:\n"
    "    \"question\": {\n"
    "      \"id\": \"q-1\", \"type\": \"input\", \"format\": \"text\",\n"
    "      \"prompt\": \"<your specific question>\",\n"
    "      \"answer\": null, \"answeredAt\": null\n"
    "    }\n"
    "  (use \"format\": \"number\" for a numeric answer.)\n"
    "  Pick from options:\n"
    "    \"question\": {\n"
    "      \"id\": \"q-1\", \"type\": \"choice\", \"multi\": false,\n"
    "      \"options\": [\"<option a>\", \"<option b>\"],\n"
    "      \"prompt\": \"<your specific question>\",\n"
    "      \"answer\": null, \"answeredAt\": null\n"
    "    }\n"
    "  (set \"multi\": true to allow selecting several options.)\n"
    "Ask one clear, answerable question; put any context the human needs in the "
    "`prompt`."
)


# --- the tick ---

def _free_log_tail(path, n=20):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def _summarize_progress(kanban_dir, task, reason):
    """Interpret an in-flight agent's log + ticket into a status summary.

    Before the orchestrator kills a sub-agent it should leave a resumable record
    of where the agent got to and what's left, rather than a raw log dump. We
    read the agent run-log tail (its only externally-visible progress) and the
    ticket, then ask Opus to interpret them into a short "last known checkpoint
    + next steps" note that a human or a re-dispatch can pick up from.

    `reason` is the kill reason ("kill"/"stalled") so the model can frame the
    summary appropriately. The model call is best-effort: if it fails or returns
    nothing we fall back to the raw log tail, so a missing/slow model never
    blocks the kill. Mirrors `_real_opus_triage`'s claude-CLI + fallback pattern.
    """
    log_path = os.path.join(kanban_dir, "..", (oc.get_marker(task) or {}).get("logFile", ""))
    tail = _free_log_tail(log_path)
    # A stalled/hung agent frequently leaves an EMPTY stdout log (it died before
    # flushing). In that case its prior ticket comments are the ONLY surviving
    # record of what it did, so feed them to the model too.
    comments = "\n".join(
        f"- {c.get('writer','?')}: {c.get('message','')}"
        for c in (task.get("comments") or [])
    )
    ticket = {
        "id": task.get("id"),
        "title": task.get("title"),
        "detail": task.get("detail", ""),
        "status": task.get("status"),
    }
    prompt = (
        "You are the kanban orchestrator. A sub-agent working the ticket below is "
        f"about to be killed (reason: {reason}). Interpret its run-log, the ticket, "
        "and the ticket's prior comments to summarize the CURRENT status. Reply with "
        "two short labelled sections and nothing else:\n"
        "CHECKPOINT: the last known good state the agent reached (what it did/changed).\n"
        "NEXT STEPS: what remains so a human or a fresh agent can resume.\n\n"
        f"TICKET:\n{json.dumps(ticket, ensure_ascii=False)}\n\n"
        f"TICKET COMMENTS (prior progress notes):\n{comments or '(none)'}\n\n"
        f"AGENT LOG (tail):\n{tail or '(empty — agent produced no log output)'}"
    )
    state = oc.read_state(kanban_dir)
    model = state.get("summarizerModel") or oc.DEFAULT_LOOP_MODEL
    timeout = state.get("triageTimeoutSeconds") or 120
    try:
        out = subprocess.run(["claude", "-p", prompt, "--model", model],
                             capture_output=True, text=True, timeout=timeout)
        summary = (out.stdout or "").strip()
        if summary:
            return summary
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    # Fallback: the model was unavailable. Leave whatever last-known progress we
    # have for a human to read. When the log is empty, say so explicitly (rather
    # than a bare "(no log output)") and surface the prior comments instead.
    if tail:
        return ("Could not interpret progress (summarizer unavailable). "
                "Last log tail before kill:\n" + tail)
    return ("Could not interpret progress (summarizer unavailable). The agent "
            "produced no log output before the kill. Last ticket comments:\n"
            + (comments or "(none)"))


def tick(kanban_dir, *, opus_triage, summarize_progress=None):
    """One orchestrator tick: reap in-flight agents, then dispatch eligible tickets.

    Args:
        kanban_dir: path to the .AI-kanban directory.
        opus_triage: callable(prompt, eligible, profiles, free) -> dict used to
            pick which tickets to dispatch and with which profiles.
        summarize_progress: optional callable(kanban_dir, task, reason) -> str.
            Before killing an in-flight agent (kill-requested or stalled), this
            interprets the agent's log + ticket into a "last known checkpoint +
            next steps" comment so the kill leaves a resumable record.  Defaults
            to `_summarize_progress`; injectable so tests don't spawn a model.
    """
    if summarize_progress is None:
        summarize_progress = _summarize_progress
    state = oc.read_state(kanban_dir)
    tasks = load_all_tasks(kanban_dir)

    # 1. Stop-all.
    if state.get("stopAllRequested"):
        for t in tasks:
            m = oc.get_marker(t)
            if m and m.get("state") == "dispatched":
                pid = m.get("pid")
                kill_pid(pid)
                if pid:
                    _release_proc(pid)
                # Re-read fresh so the killed agent's last comments survive our
                # status write, and retire its idle sidecar. (Ticket #42.)
                t = _reread_task(t)
                clear_idle(kanban_dir, t["_board"], t["id"])
                _add_comment(t, "Stopped by Stop-All.")
                _add_history(t, t.get("status"), "blocked")
                t["status"] = "blocked"
                oc.clear_marker(t)
                write_task(t["_path"], t)
                oc.append_activity(kanban_dir, {"ts": oc.now_iso(), "kind": "reap",
                                                "ticket": t["id"], "reason": "stop-all"})
        state["stopAllRequested"] = False
        oc.write_state(kanban_dir, state)
        tasks = load_all_tasks(kanban_dir)

    # 2. Reap in-flight.
    now = time.time()
    for t in tasks:
        m = oc.get_marker(t)
        if not m or m.get("state") != "dispatched":
            continue
        pid = m.get("pid")
        alive = _process_alive(pid)
        # An agent we no longer hold a Popen for (e.g. dispatched before a
        # server re-exec) is "adopted": reap_decision judges it by what it left
        # in the ticket rather than by an exit code we can't read.
        adopted = pid not in _PROCS
        # Track streamed-log growth so stall = real idleness, not wall-clock.
        log_rel = m.get("logFile", "")
        log_abs = os.path.join(kanban_dir, "..", log_rel) if log_rel else None
        try:
            size = os.path.getsize(log_abs) if log_abs else 0
        except OSError:
            size = 0
        # Idle-stall tracking lives in a private sidecar, NOT on the ticket, so a
        # running agent's tick never rewrites (and clobbers) the live ticket. (#42)
        idle = read_idle(kanban_dir, t["_board"], t["id"])
        oc.note_log_growth(idle, size, oc.now_iso())
        state_idle = state.get("idleSeconds", 600)
        max_agent = state.get("maxAgentSeconds", 0) or None
        last_growth = _marker_epoch({"dispatchedAt": idle.get("lastGrowthAt")}, now) \
            if idle.get("lastGrowthAt") else None
        decision = oc.reap_decision(
            t, alive=alive, exit_code=None if alive else _exit_code(pid),
            now_ts=now, dispatched_ts=_marker_epoch(m, now), adopted=adopted,
            idle_seconds=state_idle, last_growth_ts=last_growth,
            max_agent_seconds=max_agent)
        action = decision["action"]
        if action == "running":
            # Persist idle tracking to the sidecar only — the ticket is untouched.
            write_idle(kanban_dir, t["_board"], t["id"], idle)
            continue
        # Terminal: re-read fresh so the sub-agent's latest comments/question/
        # commitGate are merged with our status write, and retire the sidecar.
        t = _reread_task(t)
        m = oc.get_marker(t) or m
        clear_idle(kanban_dir, t["_board"], t["id"])
        if action == "kill_requested":
            # Interpret + record the agent's progress BEFORE destroying it, so
            # the kill leaves a resumable checkpoint instead of just "Killed".
            _add_comment(t, "CHECKPOINT before kill (requested):\n"
                         + summarize_progress(kanban_dir, t, "kill"))
            write_task(t["_path"], t)
            kill_pid(pid)
            if pid:
                _release_proc(pid)
            _add_comment(t, "Killed by request.")
            _finish_blocked(kanban_dir, t, "kill")
        elif action == "completed":
            if pid:
                _release_proc(pid)
            # Record the agent's output before marking the ticket done: kanban-only
            # work is auto-committed to master (gated by commit requirements);
            # everything else is published to the ticket's isolated branch.
            _finish_completion(kanban_dir, t)
            _add_history(t, t.get("status"), "completed")
            t["status"] = "completed"
            oc.clear_marker(t)
            write_task(t["_path"], t)
            oc.append_activity(kanban_dir, {"ts": oc.now_iso(), "kind": "complete",
                                            "ticket": t["id"],
                                            "branch": t.get("outputBranch")})
        elif action == "needs_human":
            if pid:
                _release_proc(pid)
            _finish_blocked(kanban_dir, t, "needs_human",
                            message=(m.get("question") or {}).get("prompt", ""))
        elif action == "crashed":
            if pid:
                _release_proc(pid)
            tail = _free_log_tail(os.path.join(kanban_dir, "..", m.get("logFile", "")))
            # A "crash" is often just a Claude usage limit (the CLI exits non-zero
            # and prints "usage limit reached|<reset-epoch>"). That isn't a real
            # failure and needs no human: re-queue the ticket and park dispatch
            # until the limit resets, when the tick loop resumes on its own.
            limit = oc.parse_usage_limit(tail)
            if limit is not None:
                reset = limit.get("resetAt")
                oc.set_usage_pause(kanban_dir, reset, now,
                                   reason=f"ticket {t['id']} hit a usage limit")
                until = oc.read_usage_pause(kanban_dir).get("pausedUntil")
                _add_comment(
                    t, "Paused: Claude usage limit reached. Re-queued to `ready`; "
                       "the orchestrator will resume automatically once the limit "
                       "resets" + (f" (~{_fmt_epoch(until)})." if until else "."))
                _add_history(t, t.get("status"), "ready")
                t["status"] = "ready"
                oc.clear_marker(t)
                write_task(t["_path"], t)
                oc.append_activity(kanban_dir, {
                    "ts": oc.now_iso(), "kind": "usage_limit", "board": t["_board"],
                    "ticket": t["id"], "pausedUntil": until})
                continue
            _add_comment(t, "NEEDS HUMAN: agent exited unexpectedly.\n" + tail)
            _finish_blocked(kanban_dir, t, "error")
        elif action == "stalled":
            # Interpret + record the agent's progress BEFORE reaping it.
            _add_comment(t, "CHECKPOINT before kill (stalled):\n"
                         + summarize_progress(kanban_dir, t, "stalled"))
            write_task(t["_path"], t)
            kill_pid(pid)
            if pid:
                _release_proc(pid)
            _add_comment(t, "Reaped: no progress (stalled).")
            _finish_blocked(kanban_dir, t, "reap", reason="stalled")

    # 3. Promote todo -> ready for any ticket whose dependencies are now met.
    # This is board housekeeping (it runs regardless of `enabled`): dispatch is
    # gated by `enabled`, but the Ready queue should stay current either way.
    tasks = load_all_tasks(kanban_dir)
    for t in oc.promotable_tickets(tasks):
        _add_history(t, t.get("status"), "ready")
        t["status"] = "ready"
        write_task(t["_path"], t)
        oc.append_activity(kanban_dir, {"ts": oc.now_iso(), "kind": "promote",
                                        "board": t["_board"], "ticket": t["id"]})

    # 4 + 5. Dispatch from `ready` (only if enabled).
    if not state.get("enabled"):
        return

    # Usage-limit pause (ticket #60): while parked we still reaped and promoted
    # above, but we must not dispatch new work into the same limit. Once the reset
    # time passes, clear the pause and resume — the loop restarts itself.
    pause = oc.read_usage_pause(kanban_dir)
    if pause.get("pausedUntil"):
        if now < pause["pausedUntil"]:
            return
        oc.clear_usage_pause(kanban_dir)
        oc.append_activity(kanban_dir, {"ts": oc.now_iso(), "kind": "usage_resume",
                                        "pausedUntil": pause.get("pausedUntil")})

    tasks = load_all_tasks(kanban_dir)
    in_flight = sum(1 for t in tasks if oc.is_in_flight(t))
    free = max(0, state.get("concurrencyCap", 3) - in_flight)
    if free <= 0:
        return

    eligible = oc.eligible_tickets(tasks)
    if not eligible:
        return
    profiles = oc.list_profiles(kanban_dir)
    if not profiles:
        return

    response = opus_triage(_triage_prompt(kanban_dir), eligible, profiles, free) or {}
    # Dispatch keys by (board, id): ticket ids are unique only within a board, so a
    # same-id ticket on two boards would collide under bare-id keying. An id that is
    # unambiguous across the eligible set may still be named board-lessly (the triage
    # prompt's existing shape / a single-board board); an ambiguous id requires an
    # explicit board to be dispatchable.
    ids = [str(t["id"]) for t in eligible]
    dup_ids = {i for i in ids if ids.count(i) > 1}
    elig_keys = set()
    for t in eligible:
        tid = str(t["id"])
        elig_keys.add((t["_board"], tid))
        if tid not in dup_ids:
            elig_keys.add(tid)
    chosen = oc.validate_triage(response, {p["name"] for p in profiles}, elig_keys)
    # The cap, not triage's output length, must drive the count: fill any leftover
    # free slots greedily from eligible tickets triage didn't name.
    chosen = chosen + oc.backfill_dispatch(eligible, chosen, profiles, free)
    by_key = {(t["_board"], str(t["id"])): t for t in eligible}
    by_bare = {}
    for t in eligible:
        by_bare.setdefault(str(t["id"]), t)
    by_name = {p["name"]: p for p in profiles}
    for item in chosen[:free]:
        board = item.get("board")
        tid = str(item["ticket"])
        # An explicit board pins the exact ticket; a board-less item is only ever
        # kept for an unambiguous id, so its bare-id lookup is unique.
        t = by_key[(board, tid)] if board is not None else by_bare[tid]
        profile = by_name[item["profile"]]
        # A model pinned on the ticket (from the ticket UI picklist) wins over
        # triage's suggestion and the profile default.
        model = t.get("model") or item.get("model") or profile.get("model")
        # A spawn failure for one ticket must not abort the whole tick — log it
        # and move on, so other tickets still get dispatched.
        try:
            marker = spawn_agent(kanban_dir, t["_board"], t, profile, model)
        except Exception as e:
            oc.append_activity(kanban_dir, {
                "ts": oc.now_iso(), "kind": "error", "board": t["_board"],
                "ticket": t["id"], "message": f"spawn failed: {e}",
            })
            continue
        oc.set_marker(t, marker)
        # Record the sub-agent's session id at the top level so the board UI can
        # offer a `claude --resume <id>` takeover command for a stuck ticket.
        if marker.get("sessionId"):
            t["claudeSessionId"] = marker["sessionId"]
        _add_history(t, t.get("status"), "in_progress")
        t["status"] = "in_progress"
        write_task(t["_path"], t)
        oc.append_activity(kanban_dir, {
            "ts": oc.now_iso(), "kind": "dispatch", "board": t["_board"],
            "ticket": t["id"], "profile": item["profile"], "model": model,
            "reason": item.get("reason", ""),
        })


def _fmt_epoch(epoch):
    """A short human-readable UTC time for a unix epoch (for ticket comments)."""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return str(epoch)


def _marker_epoch(marker, default):
    """Epoch seconds for the marker's dispatchedAt, or `default` if unparseable."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(marker["dispatchedAt"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return default


def _finish_blocked(kanban_dir, task, kind, *, message="", reason=""):
    marker = oc.get_marker(task) or {}
    _add_history(task, task.get("status"), "blocked")
    task["status"] = "blocked"
    # Preserve a question (needs_human) but mark no longer dispatched.
    if marker.get("question"):
        marker["state"] = "blocked"
        oc.set_marker(task, marker)
    else:
        oc.clear_marker(task)
    write_task(task["_path"], task)
    entry = {"ts": oc.now_iso(), "kind": kind, "ticket": task["id"]}
    if message:
        entry["message"] = message
    if reason:
        entry["reason"] = reason
    oc.append_activity(kanban_dir, entry)


def _triage_prompt(kanban_dir):
    path = os.path.join(kanban_dir, "orchestrator_triage_prompt.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "Return {\"dispatch\": []}"


def _real_opus_triage(prompt, eligible, profiles, free, model=None, timeout=120):
    model = model or oc.DEFAULT_LOOP_MODEL
    payload = {
        "freeSlots": free,
        "eligible": [{"id": t["id"], "title": t.get("title"),
                      "detail": t.get("detail", ""), "board": t.get("_board")}
                     for t in eligible],
        "profiles": [{"name": p["name"], "whenToUse": p.get("whenToUse", ""),
                      "model": p.get("model")} for p in profiles],
    }
    full = prompt + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False)
    try:
        out = subprocess.run(["claude", "-p", full, "--model", model],
                             capture_output=True, text=True, timeout=timeout)
        text = out.stdout.strip()
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1]) if start >= 0 else {"dispatch": []}
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return {"dispatch": []}


def run_loop(kanban_dir=None, *, stop_event=None, tick_seconds=TICK_SECONDS,
             opus_triage=None):
    """Run the orchestrator tick loop until `stop_event` is set.

    Shared by `main()` (standalone `python orchestrator.py`) and the kanban
    server's background thread. One bad tick never kills the loop — it is logged
    to the activity feed and the loop continues. When `stop_event` is provided
    we wait on it (so shutdown is prompt); otherwise we plain-sleep between ticks.

    Single-instance lock: only ONE loop may tick across all processes. The server's
    background loop and a manual `python orchestrator.py` would otherwise run two
    concurrent loops → double dispatch + cap breaches. We acquire the cross-process
    lock first and refuse to tick if another live process holds it, releasing on exit
    so the next process can take over.
    """
    kanban_dir = kanban_dir or oc.KANBAN_DIR
    if opus_triage is None:
        # Default triage reads triageModel and triageTimeoutSeconds fresh each
        # call so live config changes take effect without a restart.
        def opus_triage(prompt, eligible, profiles, free):
            st = oc.read_state(kanban_dir)
            model = st.get("triageModel") or oc.DEFAULT_LOOP_MODEL
            timeout = st.get("triageTimeoutSeconds") or 120
            return _real_opus_triage(prompt, eligible, profiles, free,
                                     model=model, timeout=timeout)
    if not oc.acquire_lock(kanban_dir):
        oc.append_activity(kanban_dir, {
            "ts": oc.now_iso(), "kind": "skip",
            "message": "another orchestrator holds the lock; not starting this loop",
        })
        return
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                tick(kanban_dir, opus_triage=opus_triage)
            except Exception as e:  # never let one bad tick kill the loop
                oc.append_activity(kanban_dir, {"ts": oc.now_iso(), "kind": "error",
                                                "message": f"tick failed: {e}"})
            # Read tickSeconds fresh so a live config change takes effect without restart.
            interval = oc.read_state(kanban_dir).get("tickSeconds") or tick_seconds
            if stop_event is not None:
                if stop_event.wait(interval):
                    break
            else:
                time.sleep(interval)
    finally:
        oc.release_lock(kanban_dir)


def main():
    kanban_dir = oc.KANBAN_DIR
    print(f"Orchestrator running over {kanban_dir}. Ctrl+C to stop.")
    try:
        run_loop(kanban_dir)
    except KeyboardInterrupt:
        print("\nOrchestrator stopped.")


if __name__ == "__main__":
    main()
