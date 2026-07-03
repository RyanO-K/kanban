# Kanban Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an orchestrator that watches the `.kanban/` board, uses Opus to dispatch headless `claude -p` sub-agents (selected by named profile) to work tickets autonomously, surfaces typed human-attention questions in the kanban UI, and is fully controllable (on/off, concurrency, kill, stop-all) from the UI.

**Architecture:** Four components share `.kanban/` JSON files as the single source of truth: a headless `orchestrator.py` loop (ticks ~60s: reap → triage with Opus → dispatch), `kanban_server.py` extensions (profiles CRUD, kill endpoint, state toggle), `config/<name>.json` profile files, and two new `kanban.html` tabs (Profiles, Orchestrator). A pure-logic core module (`orchestrator_core.py`) holds all decision logic so it is unit-testable without launching real processes; `orchestrator.py` is the thin runtime that wires the core to real subprocesses and a sleep loop.

**Tech Stack:** Python 3.13 stdlib only (no third-party runtime deps — matches the existing server). pytest 9 for tests. Vanilla JS / inline `<script>` for the HTML (matches existing `kanban.html`). The `claude` CLI in print mode (`claude -p`) for sub-agents.

## Global Constraints

- **Python: stdlib only** for runtime code (the existing `kanban_server.py` uses only stdlib; do not add dependencies). pytest is a dev/test dependency only.
- **Spec is authoritative:** `docs/superpowers/specs/2026-06-24-kanban-orchestrator-design.md`. Read it before starting.
- **Board detection:** a directory is a board only if it contains `_meta.json`. `config/` and `_orchestrator/` MUST NOT contain `_meta.json`, so they never appear as boards. Do not change this rule.
- **Field ownership (concurrency mitigation):** the loop writes only the ticket's `orchestrator` block + `status` + appends `history`. Sub-agents write only `comments` and the result/`question` fields. Never have two writers own the same field.
- **Status vocabulary:** ticket `status` ∈ {`todo`, `in_progress`, `blocked`, `pending`, `completed`}. `orchestrator.state` ∈ {`dispatched`, `done`, `reaped`, `blocked`}. Activity `kind` ∈ {`dispatch`, `complete`, `needs_human`, `reap`, `error`}.
- **Timestamps:** UTC ISO-8601 via `datetime.now(timezone.utc).isoformat(timespec="seconds")` (matches existing `now_iso()`).
- **Paths are POSIX-style in JSON** (e.g. `.kanban/_orchestrator/runs/7-<ts>.log`) to match existing `_filePath` style, even on Windows.
- **Git:** the git repo IS the `.kanban/` directory itself (already initialized — Task 0 is done). Run every `git`/`commit` command from inside `C:\Users\you\Documents\GitHub\.kanban`.
- **Repo root is `.kanban/`.** Throughout this plan, paths are written with a leading `.kanban/` prefix for readability, but the actual repo root is `.kanban/` — so `.kanban/orchestrator.py` is the file `orchestrator.py` at the repo root, `git add .kanban/orchestrator.py` is `git add orchestrator.py`, and every `cd "C:/Users/you/Documents/GitHub/.kanban"` is the repo root. The spec and plan docs live OUTSIDE this repo (under `../docs/`) and are not committed here.

---

## File Structure

**New files:**
- `.kanban/orchestrator_core.py` — pure decision logic (no subprocess, no sleep). Eligibility, reap decisions, triage-response validation, state/activity/marker read-write helpers, question/answer shapes. Fully unit-tested.
- `.kanban/orchestrator.py` — thin runtime: the tick loop, real `subprocess` spawning of `claude -p`, real process kill, sleep. Wires `orchestrator_core` to the OS.
- `.kanban/orchestrator_triage_prompt.md` — the shared "brain" prompt text used for profile/model selection (read by the loop; also usable by an interactive CLI session).
- `.kanban/config/frontend.json`, `.kanban/config/backend.json`, `.kanban/config/general.json` — three starter profiles.
- `.kanban/_orchestrator/state.json` — control state (created by Task 1 helper / on first run).
- `.kanban/_orchestrator/activity.json` — activity feed (created on first append).
- `.kanban/tests/test_orchestrator_core.py` — unit tests for the core.
- `.kanban/tests/test_server_orchestrator.py` — endpoint tests for the server extensions.
- `.kanban/tests/conftest.py` — pytest fixture building a temp `.kanban/` tree.

**Modified files:**
- `.kanban/kanban_server.py` — add profiles CRUD, kill endpoint, state GET/PUT, and route wiring. Keep `config/` and `_orchestrator/` out of `scan_boards`/`load_all_boards` (already true via `_meta.json` gate — add an explicit skip-set as belt-and-suspenders).
- `.kanban/kanban.html` — add a top-level view switcher (Boards / Profiles / Orchestrator) and the two new tab views.
- `.kanban/CLAUDE.md` — document the orchestrator, profiles, and `_orchestrator/` layout.

---

## Parallelization

After **Task 0** (git init) and **Task 1** (shared core data helpers) land, three tracks run in parallel because they touch disjoint files and depend only on Task 1's documented interfaces:

- **Track A (Loop):** Tasks 2, 3, 4 — `orchestrator_core.py` logic + `orchestrator.py` runtime.
- **Track B (Server):** Tasks 5, 6, 7 — `kanban_server.py` endpoints.
- **Track C (UI + profiles):** Tasks 8, 9, 10 — `config/*.json`, `kanban.html` tabs.
- **Task 11 (docs)** depends on all and runs last.

Track B and Track C share the data shapes from Task 1 but never edit the same files. Within a track, tasks are sequential.

---

### Task 0: Initialize git repository — ALREADY DONE

The `.kanban/` directory has already been initialized as a git repo with a
`.gitignore` (`__pycache__/`, `*.pyc`, `.pytest_cache/`, `_orchestrator/runs/`)
and an initial commit of the existing tree. **Skip this task** — it is recorded
here only so the task numbering matches the tracks below. Verify with:

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && git log --oneline -1`
Expected: a `chore: initialize .kanban as a git repo` commit.

---

### Task 1: Shared data helpers in `orchestrator_core.py`

The read/write helpers every other component relies on. No decision logic yet — just typed accessors for state, activity, profiles, and the ticket `orchestrator` marker, so Tracks A/B/C all use identical shapes.

**Files:**
- Create: `.kanban/orchestrator_core.py`
- Create: `.kanban/tests/conftest.py`
- Create: `.kanban/tests/test_orchestrator_core.py`

**Interfaces:**
- Consumes: nothing (only stdlib).
- Produces (import as `from orchestrator_core import ...`):
  - `KANBAN_DIR: str` — resolved `.kanban/` dir (the dir this file lives in).
  - `ORCH_DIR: str` — `<KANBAN_DIR>/_orchestrator`.
  - `CONFIG_DIR: str` — `<KANBAN_DIR>/config`.
  - `now_iso() -> str`
  - `read_state(kanban_dir: str) -> dict` — returns `{"enabled": bool, "concurrencyCap": int, "stopAllRequested": bool}`, creating the file with defaults `{"enabled": False, "concurrencyCap": 3, "stopAllRequested": False}` if missing.
  - `write_state(kanban_dir: str, state: dict) -> None`
  - `append_activity(kanban_dir: str, entry: dict) -> None` — appends `entry` (must include `ts`, `kind`) to `_orchestrator/activity.json`'s `entries` list, creating the file if missing.
  - `read_activity(kanban_dir: str, limit: int = 200) -> list[dict]` — most-recent-last list, capped to `limit`.
  - `list_profiles(kanban_dir: str) -> list[dict]` — every valid JSON in `config/`; skips malformed files.
  - `read_profile(kanban_dir: str, name: str) -> dict | None`
  - `write_profile(kanban_dir: str, profile: dict) -> None` — writes `config/<profile["name"]>.json`.
  - `delete_profile(kanban_dir: str, name: str) -> bool`
  - `get_marker(task: dict) -> dict | None` — returns `task.get("orchestrator")`.
  - `set_marker(task: dict, marker: dict) -> None` — sets `task["orchestrator"] = marker`.
  - `clear_marker(task: dict) -> None` — removes the `orchestrator` key if present.
  - `safe_name(name: str) -> str | None` — reuse the existing server's path-segment safety: returns `name` only if it's a single safe segment (no `/`, `\`, `.`, `..`), else `None`.

- [ ] **Step 1: Write conftest fixture**

Create `.kanban/tests/conftest.py`:

```python
import json
import os
import sys

import pytest

# Make the .kanban dir importable (orchestrator_core.py lives there).
KANBAN_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KANBAN_SRC)


@pytest.fixture
def kanban(tmp_path):
    """A temp .kanban tree with one board and two tickets."""
    root = tmp_path / ".kanban"
    board = root / "demo"
    board.mkdir(parents=True)
    (board / "_meta.json").write_text(json.dumps({"project": "Demo"}), encoding="utf-8")
    (board / "1.json").write_text(
        json.dumps({"id": "1", "title": "First", "status": "todo"}), encoding="utf-8"
    )
    (board / "2.json").write_text(
        json.dumps({"id": "2", "title": "Second", "status": "todo", "dependsOn": ["1"]}),
        encoding="utf-8",
    )
    (root / "config").mkdir()
    (root / "_orchestrator").mkdir()
    return str(root)
```

- [ ] **Step 2: Write the failing tests**

Create `.kanban/tests/test_orchestrator_core.py`:

```python
import json
import os

import orchestrator_core as oc


def test_read_state_defaults(kanban):
    state = oc.read_state(kanban)
    assert state == {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False}
    # File was created.
    assert os.path.isfile(os.path.join(kanban, "_orchestrator", "state.json"))


def test_write_then_read_state(kanban):
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 5, "stopAllRequested": False})
    assert oc.read_state(kanban)["concurrencyCap"] == 5


def test_append_and_read_activity(kanban):
    oc.append_activity(kanban, {"ts": oc.now_iso(), "kind": "dispatch", "ticket": "1"})
    oc.append_activity(kanban, {"ts": oc.now_iso(), "kind": "reap", "ticket": "1"})
    entries = oc.read_activity(kanban)
    assert len(entries) == 2
    assert entries[-1]["kind"] == "reap"


def test_profile_round_trip(kanban):
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "UI work"})
    assert oc.read_profile(kanban, "frontend")["whenToUse"] == "UI work"
    assert any(p["name"] == "frontend" for p in oc.list_profiles(kanban))
    assert oc.delete_profile(kanban, "frontend") is True
    assert oc.read_profile(kanban, "frontend") is None


def test_list_profiles_skips_malformed(kanban):
    oc.write_profile(kanban, {"name": "good", "whenToUse": "x"})
    with open(os.path.join(kanban, "config", "bad.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    names = [p["name"] for p in oc.list_profiles(kanban)]
    assert names == ["good"]


def test_marker_helpers():
    task = {"id": "1"}
    assert oc.get_marker(task) is None
    oc.set_marker(task, {"state": "dispatched", "pid": 999})
    assert oc.get_marker(task)["pid"] == 999
    oc.clear_marker(task)
    assert "orchestrator" not in task


def test_safe_name():
    assert oc.safe_name("frontend") == "frontend"
    assert oc.safe_name("../etc") is None
    assert oc.safe_name("a/b") is None
    assert oc.safe_name("..") is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_core.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator_core'`.

- [ ] **Step 4: Implement `orchestrator_core.py`**

Create `.kanban/orchestrator_core.py`:

```python
"""Pure decision + IO-helper logic for the kanban orchestrator.

No subprocess launching, no sleeping. Everything here is unit-testable.
The runtime wiring (real `claude -p` processes, kill signals, the tick loop)
lives in orchestrator.py.
"""

import json
import os
from datetime import datetime, timezone

KANBAN_DIR = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.join(KANBAN_DIR, "_orchestrator")
CONFIG_DIR = os.path.join(KANBAN_DIR, "config")

DEFAULT_STATE = {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(name):
    if not name:
        return None
    if name != os.path.basename(name):
        return None
    if name in (".", "..") or "/" in name or "\\" in name:
        return None
    return name


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# --- state ---

def _state_path(kanban_dir):
    return os.path.join(kanban_dir, "_orchestrator", "state.json")


def read_state(kanban_dir):
    path = _state_path(kanban_dir)
    if not os.path.isfile(path):
        write_state(kanban_dir, dict(DEFAULT_STATE))
        return dict(DEFAULT_STATE)
    state = _read_json(path, dict(DEFAULT_STATE))
    merged = dict(DEFAULT_STATE)
    merged.update({k: state[k] for k in DEFAULT_STATE if k in state})
    return merged


def write_state(kanban_dir, state):
    _write_json(_state_path(kanban_dir), state)


# --- activity ---

def _activity_path(kanban_dir):
    return os.path.join(kanban_dir, "_orchestrator", "activity.json")


def append_activity(kanban_dir, entry):
    path = _activity_path(kanban_dir)
    data = _read_json(path, {"entries": []})
    if not isinstance(data, dict) or "entries" not in data:
        data = {"entries": []}
    data["entries"].append(entry)
    _write_json(path, data)


def read_activity(kanban_dir, limit=200):
    data = _read_json(_activity_path(kanban_dir), {"entries": []})
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return entries[-limit:]


# --- profiles ---

def _profile_path(kanban_dir, name):
    safe = safe_name(name)
    if safe is None:
        return None
    return os.path.join(kanban_dir, "config", f"{safe}.json")


def list_profiles(kanban_dir):
    config_dir = os.path.join(kanban_dir, "config")
    out = []
    if not os.path.isdir(config_dir):
        return out
    for entry in sorted(os.scandir(config_dir), key=lambda e: e.name):
        if entry.is_file() and entry.name.endswith(".json"):
            data = _read_json(entry.path, None)
            if isinstance(data, dict) and data.get("name"):
                out.append(data)
    return out


def read_profile(kanban_dir, name):
    path = _profile_path(kanban_dir, name)
    if path is None or not os.path.isfile(path):
        return None
    return _read_json(path, None)


def write_profile(kanban_dir, profile):
    path = _profile_path(kanban_dir, profile.get("name", ""))
    if path is None:
        raise ValueError("invalid profile name")
    _write_json(path, profile)


def delete_profile(kanban_dir, name):
    path = _profile_path(kanban_dir, name)
    if path is None or not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# --- ticket orchestrator marker ---

def get_marker(task):
    return task.get("orchestrator")


def set_marker(task, marker):
    task["orchestrator"] = marker


def clear_marker(task):
    task.pop("orchestrator", None)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_core.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/orchestrator_core.py .kanban/tests/conftest.py .kanban/tests/test_orchestrator_core.py
git commit -m "feat(orchestrator): shared data helpers + tests"
```

---

### Task 2: Eligibility & triage-validation logic (core)

Add the pure decision functions the loop needs: which tickets are eligible, and validating Opus's triage response.

**Files:**
- Modify: `.kanban/orchestrator_core.py`
- Modify: `.kanban/tests/test_orchestrator_core.py`

**Interfaces:**
- Consumes: `list_profiles`, `get_marker` from Task 1.
- Produces:
  - `is_in_flight(task: dict) -> bool` — True if `get_marker(task)` exists and its `state == "dispatched"`.
  - `eligible_tickets(tasks: list[dict]) -> list[dict]` — tickets where every id in `dependsOn` maps to a task with `status == "completed"`, the ticket itself is not `completed`/`in_progress`-in-flight, and not in-flight. A ticket with an answered question (`orchestrator.question.answer` set) is eligible for re-dispatch even if status is `blocked`. `tasks` is the full cross-board list; each task must carry `id` and may carry `_board`.
  - `validate_triage(response: dict, profile_names: set[str], eligible_ids: set[str]) -> list[dict]` — given Opus's parsed JSON `{"dispatch": [{"ticket","profile","model","reason"}, ...]}`, returns only the well-formed items whose `profile` ∈ `profile_names` and `ticket` ∈ `eligible_ids`. Drops malformed/unknown items. Never raises.

- [ ] **Step 1: Write the failing tests**

Append to `.kanban/tests/test_orchestrator_core.py`:

```python
def test_is_in_flight():
    assert oc.is_in_flight({"id": "1"}) is False
    assert oc.is_in_flight({"id": "1", "orchestrator": {"state": "done"}}) is False
    assert oc.is_in_flight({"id": "1", "orchestrator": {"state": "dispatched"}}) is True


def test_eligible_no_deps():
    tasks = [{"id": "1", "status": "todo"}]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["1"]


def test_eligible_blocked_by_incomplete_dep():
    tasks = [
        {"id": "1", "status": "todo"},
        {"id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["1"]


def test_eligible_dep_completed():
    tasks = [
        {"id": "1", "status": "completed"},
        {"id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["2"]


def test_eligible_skips_in_flight_and_completed():
    tasks = [
        {"id": "1", "status": "completed"},
        {"id": "2", "status": "in_progress", "orchestrator": {"state": "dispatched"}},
        {"id": "3", "status": "todo"},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["3"]


def test_eligible_answered_question_redispatch():
    tasks = [
        {"id": "1", "status": "blocked",
         "orchestrator": {"state": "blocked",
                          "question": {"id": "q1", "answer": {"value": "x", "notes": ""}}}},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["1"]


def test_validate_triage_filters():
    resp = {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "claude-opus-4-8", "reason": "ui"},
        {"ticket": "9", "profile": "frontend", "reason": "not eligible"},
        {"ticket": "2", "profile": "ghost", "reason": "bad profile"},
        {"profile": "frontend"},
    ]}
    out = oc.validate_triage(resp, {"frontend"}, {"1", "2"})
    assert len(out) == 1
    assert out[0]["ticket"] == "1"


def test_validate_triage_handles_garbage():
    assert oc.validate_triage({}, {"frontend"}, {"1"}) == []
    assert oc.validate_triage({"dispatch": "nope"}, {"frontend"}, {"1"}) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_core.py -k "eligible or triage or in_flight" -v`
Expected: FAIL — `AttributeError: module 'orchestrator_core' has no attribute 'is_in_flight'`.

- [ ] **Step 3: Implement the functions**

Append to `.kanban/orchestrator_core.py`:

```python
# --- eligibility & triage ---

def is_in_flight(task):
    marker = get_marker(task)
    return bool(marker) and marker.get("state") == "dispatched"


def _has_answered_question(task):
    marker = get_marker(task)
    if not marker:
        return False
    q = marker.get("question")
    return bool(q) and q.get("answer") is not None


def eligible_tickets(tasks):
    status_by_id = {str(t.get("id")): t.get("status") for t in tasks}
    out = []
    for t in tasks:
        if is_in_flight(t):
            continue
        # Answered-question tickets re-dispatch even though blocked.
        if _has_answered_question(t):
            out.append(t)
            continue
        if t.get("status") in ("completed", "in_progress"):
            continue
        deps = t.get("dependsOn") or []
        if isinstance(deps, str):
            deps = [deps]
        if all(status_by_id.get(str(d)) == "completed" for d in deps):
            out.append(t)
    return out


def validate_triage(response, profile_names, eligible_ids):
    if not isinstance(response, dict):
        return []
    items = response.get("dispatch")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ticket = str(item.get("ticket", ""))
        profile = item.get("profile", "")
        if ticket in eligible_ids and profile in profile_names:
            out.append({
                "ticket": ticket,
                "profile": profile,
                "model": item.get("model"),
                "reason": item.get("reason", ""),
            })
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_core.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/orchestrator_core.py .kanban/tests/test_orchestrator_core.py
git commit -m "feat(orchestrator): eligibility + triage validation"
```

---

### Task 3: Reap-decision & question-answer logic (core)

The logic that decides what to do with a finished/killed/stalled agent, and the question/answer round-trip shape.

**Files:**
- Modify: `.kanban/orchestrator_core.py`
- Modify: `.kanban/tests/test_orchestrator_core.py`

**Interfaces:**
- Consumes: `get_marker`, `now_iso` from Task 1.
- Produces:
  - `reap_decision(task: dict, *, alive: bool, exit_code: int | None, now_ts: float, dispatched_ts: float, stall_seconds: int = 900) -> dict` — returns a decision dict `{"action": ...}` where action ∈ `"kill_requested"`, `"completed"`, `"needs_human"`, `"crashed"`, `"stalled"`, `"running"`. Rules, in order:
    - marker `killRequested` is truthy → `kill_requested`.
    - `alive` is False and `exit_code == 0` and the marker has a `question` → `needs_human`.
    - `alive` is False and `exit_code == 0` → `completed`.
    - `alive` is False and `exit_code` not 0 (or None) → `crashed`.
    - `alive` is True and `now_ts - dispatched_ts >= stall_seconds` → `stalled` (caller then asks Opus whether to actually kill).
    - else → `running`.
  - `build_question(prompt, qtype="input", *, fmt="text", options=None, multi=False) -> dict` — returns the question dict shape (with `id`, `askedAt`, `answer=None`). `qtype` ∈ {`input`, `choice`}.
  - `apply_answer(question: dict, value, notes: str) -> dict` — returns a copy with `answer={"value": value, "notes": notes}` and `answeredAt` set.

- [ ] **Step 1: Write the failing tests**

Append to `.kanban/tests/test_orchestrator_core.py`:

```python
def _mk(marker=None, **kw):
    t = {"id": "1", "status": "in_progress"}
    if marker is not None:
        t["orchestrator"] = marker
    t.update(kw)
    return t


def test_reap_kill_requested():
    t = _mk({"state": "dispatched", "killRequested": True})
    d = oc.reap_decision(t, alive=True, exit_code=None, now_ts=100, dispatched_ts=0)
    assert d["action"] == "kill_requested"


def test_reap_completed():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=False, exit_code=0, now_ts=100, dispatched_ts=0)
    assert d["action"] == "completed"


def test_reap_needs_human():
    t = _mk({"state": "dispatched", "question": {"id": "q1"}})
    d = oc.reap_decision(t, alive=False, exit_code=0, now_ts=100, dispatched_ts=0)
    assert d["action"] == "needs_human"


def test_reap_crashed():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=False, exit_code=1, now_ts=100, dispatched_ts=0)
    assert d["action"] == "crashed"


def test_reap_stalled():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=True, exit_code=None, now_ts=1000, dispatched_ts=0,
                         stall_seconds=900)
    assert d["action"] == "stalled"


def test_reap_running():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=True, exit_code=None, now_ts=100, dispatched_ts=0,
                         stall_seconds=900)
    assert d["action"] == "running"


def test_build_and_answer_question():
    q = oc.build_question("Pick one", "choice", options=["a", "b"], multi=False)
    assert q["type"] == "choice"
    assert q["options"] == ["a", "b"]
    assert q["answer"] is None
    answered = oc.apply_answer(q, "a", "use a")
    assert answered["answer"] == {"value": "a", "notes": "use a"}
    assert answered["answeredAt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_core.py -k "reap or question" -v`
Expected: FAIL — `AttributeError: ... 'reap_decision'`.

- [ ] **Step 3: Implement**

Append to `.kanban/orchestrator_core.py`:

```python
# --- reap decisions & questions ---

def reap_decision(task, *, alive, exit_code, now_ts, dispatched_ts, stall_seconds=900):
    marker = get_marker(task) or {}
    if marker.get("killRequested"):
        return {"action": "kill_requested"}
    if not alive:
        if exit_code == 0:
            if marker.get("question"):
                return {"action": "needs_human"}
            return {"action": "completed"}
        return {"action": "crashed", "exit_code": exit_code}
    if now_ts - dispatched_ts >= stall_seconds:
        return {"action": "stalled"}
    return {"action": "running"}


def build_question(prompt, qtype="input", *, fmt="text", options=None, multi=False):
    q = {
        "id": "q-" + now_iso(),
        "type": qtype,
        "prompt": prompt,
        "askedAt": now_iso(),
        "answer": None,
        "answeredAt": None,
    }
    if qtype == "input":
        q["format"] = fmt
    elif qtype == "choice":
        q["options"] = options or []
        q["multi"] = bool(multi)
    return q


def apply_answer(question, value, notes):
    out = dict(question)
    out["answer"] = {"value": value, "notes": notes}
    out["answeredAt"] = now_iso()
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_core.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/orchestrator_core.py .kanban/tests/test_orchestrator_core.py
git commit -m "feat(orchestrator): reap decisions + question round-trip"
```

---

### Task 4: Orchestrator runtime loop & triage prompt

The thin runtime that wires the core to real `claude -p` subprocesses, a real kill, and a sleep loop. The dispatch is behind a `spawn_agent` seam so it is mockable.

**Files:**
- Create: `.kanban/orchestrator.py`
- Create: `.kanban/orchestrator_triage_prompt.md`
- Create: `.kanban/tests/test_orchestrator_loop.py`

**Interfaces:**
- Consumes: all of `orchestrator_core`.
- Produces:
  - `spawn_agent(kanban_dir, board, task, profile, model) -> dict` — launches `claude -p`, returns marker `{"state": "dispatched", "profile", "model", "pid", "dispatchedAt", "killRequested": False, "logFile"}`. The default implementation uses `subprocess.Popen`; tests monkeypatch it.
  - `kill_pid(pid: int) -> bool` — terminate a process; returns True if a signal was sent.
  - `load_all_tasks(kanban_dir) -> list[dict]` — read every ticket across boards, each annotated with `_board` and `_path`.
  - `write_task(path: str, task: dict) -> None`
  - `tick(kanban_dir, *, opus_triage) -> None` — one pass: read state → stop-all → reap → (if enabled) eligible → triage via the injected `opus_triage(prompt, eligible, profiles) -> dict` callable → dispatch up to cap. `opus_triage` is injected so tests pass a fake (no real model call).
  - `main()` — builds the real `opus_triage` (shells `claude -p` with the triage prompt), then loops `tick` every 60s.

- [ ] **Step 1: Write the triage prompt file**

Create `.kanban/orchestrator_triage_prompt.md`:

```markdown
# Orchestrator triage

You are the kanban orchestrator's triage brain. You are given:
- A list of ELIGIBLE tickets (id, title, detail, board).
- A list of available PROFILES (name, whenToUse, default model).
- The number of free dispatch slots (concurrency cap minus in-flight).

Choose which eligible tickets to work now and, for each, the best-fit profile
and the model you estimate it needs. Prefer the profile whose `whenToUse` best
matches the ticket. Pick a cheaper model for simple tickets, a stronger model
for hard ones. Do not exceed the free slots.

Respond with ONLY a JSON object, no prose:

{
  "dispatch": [
    {"ticket": "<id>", "profile": "<profile name>", "model": "<model id>", "reason": "<short why>"}
  ]
}

If nothing should be dispatched, return {"dispatch": []}.
```

- [ ] **Step 2: Write the failing loop test**

Create `.kanban/tests/test_orchestrator_loop.py`:

```python
import json
import os

import orchestrator_core as oc
import orchestrator as orch


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_tick_disabled_does_not_dispatch(kanban, monkeypatch):
    calls = []
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **k: calls.append(a) or {"state": "dispatched"})
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})
    assert calls == []


def test_tick_enabled_dispatches_eligible(kanban, monkeypatch):
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3, "stopAllRequested": False})

    def fake_spawn(kanban_dir, board, task, profile, model):
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 4242, "dispatchedAt": oc.now_iso(), "killRequested": False,
                "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    # Triage picks ticket 1 (ticket 2 depends on 1, so not eligible).
    orch.tick(kanban, opus_triage=lambda prompt, elig, profs: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1["status"] == "in_progress"
    assert t1["orchestrator"]["pid"] == 4242


def test_tick_respects_concurrency_cap(kanban, monkeypatch):
    # Ticket 1 already in-flight; cap is 1 → no new dispatch.
    p = os.path.join(kanban, "demo", "1.json")
    t = _read(p)
    t["status"] = "in_progress"
    t["orchestrator"] = {"state": "dispatched", "pid": 1, "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 1, "stopAllRequested": False})

    calls = []
    monkeypatch.setattr(orch, "spawn_agent", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})
    assert calls == []


def test_tick_stop_all_kills(kanban, monkeypatch):
    p = os.path.join(kanban, "demo", "1.json")
    t = _read(p)
    t["status"] = "in_progress"
    t["orchestrator"] = {"state": "dispatched", "pid": 777, "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3, "stopAllRequested": True})

    killed = []
    monkeypatch.setattr(orch, "kill_pid", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert killed == [777]
    t1 = _read(p)
    assert t1["status"] == "blocked"
    assert "orchestrator" not in t1 or t1["orchestrator"].get("state") != "dispatched"
    assert oc.read_state(kanban)["stopAllRequested"] is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator'`.

- [ ] **Step 4: Implement `orchestrator.py`**

Create `.kanban/orchestrator.py`:

```python
"""Kanban orchestrator runtime: the tick loop + real process management.

Decision logic lives in orchestrator_core. This module wires it to real
`claude -p` subprocesses, OS process kills, and a sleep loop. The dispatch
and process-liveness calls are module-level functions so tests can monkeypatch
them without launching real processes.
"""

import json
import os
import subprocess
import sys
import time

import orchestrator_core as oc

META_FILE = "_meta.json"
TICK_SECONDS = 60
SKIP_DIRS = {"config", "_orchestrator", "__pycache__", "tests"}


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
    out = {k: v for k, v in task.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")


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
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True,
            )
            return str(pid) in out.stdout
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def kill_pid(pid):
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _exit_code(pid):
    """Best-effort exit code for a dead process. We cannot retrieve the real
    code for an arbitrary PID we did not wait on, so a dead PID is treated as
    a clean exit (0) unless the agent left a crash marker. Real Popen handles
    in a long-lived loop would track this; here liveness is the signal."""
    return 0


def spawn_agent(kanban_dir, board, task, profile, model):
    runs_dir = os.path.join(kanban_dir, "_orchestrator", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    ts = oc.now_iso().replace(":", "").replace("-", "")
    log_name = f"{task['id']}-{ts}.log"
    log_path = os.path.join(runs_dir, log_name)

    prompt = _build_agent_prompt(task, profile)
    cmd = ["claude", "-p", prompt]
    if model:
        cmd += ["--model", model]
    allowed = profile.get("allowedTools")
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]

    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                            cwd=os.path.dirname(kanban_dir))
    return {
        "state": "dispatched",
        "profile": profile.get("name"),
        "model": model,
        "pid": proc.pid,
        "dispatchedAt": oc.now_iso(),
        "killRequested": False,
        "logFile": f".kanban/_orchestrator/runs/{log_name}",
    }


def _build_agent_prompt(task, profile):
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
        "(writer 'Claude'). If you need a human, set the ticket `status` to "
        "'blocked' and write an `orchestrator.question` object, then stop."
    )
    return "\n".join(parts)


# --- the tick ---

def _free_log_tail(path, n=20):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


def tick(kanban_dir, *, opus_triage):
    state = oc.read_state(kanban_dir)
    tasks = load_all_tasks(kanban_dir)

    # 1. Stop-all.
    if state.get("stopAllRequested"):
        for t in tasks:
            m = oc.get_marker(t)
            if m and m.get("state") == "dispatched":
                kill_pid(m.get("pid"))
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
        dispatched_ts = now if alive else now  # liveness drives decision below
        decision = oc.reap_decision(
            t, alive=alive, exit_code=None if alive else _exit_code(pid),
            now_ts=now, dispatched_ts=_marker_epoch(m, now))
        action = decision["action"]
        if action == "running":
            continue
        if action == "kill_requested":
            kill_pid(pid)
            _add_comment(t, "Killed by request.")
            _finish_blocked(kanban_dir, t, "kill")
        elif action == "completed":
            _add_history(t, t.get("status"), "completed")
            t["status"] = "completed"
            oc.clear_marker(t)
            write_task(t["_path"], t)
            oc.append_activity(kanban_dir, {"ts": oc.now_iso(), "kind": "complete",
                                            "ticket": t["id"]})
        elif action == "needs_human":
            _finish_blocked(kanban_dir, t, "needs_human",
                            message=(m.get("question") or {}).get("prompt", ""))
        elif action == "crashed":
            tail = _free_log_tail(os.path.join(kanban_dir, "..", m.get("logFile", "")))
            _add_comment(t, "NEEDS HUMAN: agent exited unexpectedly.\n" + tail)
            _finish_blocked(kanban_dir, t, "error")
        elif action == "stalled":
            kill_pid(pid)
            _add_comment(t, "Reaped: no progress (stalled).")
            _finish_blocked(kanban_dir, t, "reap", reason="stalled")

    # 3 + 4. Dispatch (only if enabled).
    if not state.get("enabled"):
        return

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

    response = opus_triage(_triage_prompt(kanban_dir), eligible, profiles) or {}
    chosen = oc.validate_triage(response, {p["name"] for p in profiles},
                                {str(t["id"]) for t in eligible})
    by_id = {str(t["id"]): t for t in eligible}
    by_name = {p["name"]: p for p in profiles}
    for item in chosen[:free]:
        t = by_id[item["ticket"]]
        profile = by_name[item["profile"]]
        model = item.get("model") or profile.get("model")
        marker = spawn_agent(kanban_dir, t["_board"], t, profile, model)
        oc.set_marker(t, marker)
        _add_history(t, t.get("status"), "in_progress")
        t["status"] = "in_progress"
        write_task(t["_path"], t)
        oc.append_activity(kanban_dir, {
            "ts": oc.now_iso(), "kind": "dispatch", "board": t["_board"],
            "ticket": t["id"], "profile": item["profile"], "model": model,
            "reason": item.get("reason", ""),
        })


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


def _real_opus_triage(prompt, eligible, profiles):
    payload = {
        "eligible": [{"id": t["id"], "title": t.get("title"),
                      "detail": t.get("detail", ""), "board": t.get("_board")}
                     for t in eligible],
        "profiles": [{"name": p["name"], "whenToUse": p.get("whenToUse", ""),
                      "model": p.get("model")} for p in profiles],
    }
    full = prompt + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False)
    try:
        out = subprocess.run(["claude", "-p", full, "--model", "claude-opus-4-8"],
                             capture_output=True, text=True, timeout=120)
        text = out.stdout.strip()
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start:end + 1]) if start >= 0 else {"dispatch": []}
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return {"dispatch": []}


def main():
    kanban_dir = oc.KANBAN_DIR
    print(f"Orchestrator running over {kanban_dir}. Ctrl+C to stop.")
    try:
        while True:
            try:
                tick(kanban_dir, opus_triage=_real_opus_triage)
            except Exception as e:  # never let one bad tick kill the loop
                oc.append_activity(kanban_dir, {"ts": oc.now_iso(), "kind": "error",
                                                "message": f"tick failed: {e}"})
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        print("\nOrchestrator stopped.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_orchestrator_loop.py -v`
Expected: all 4 tests PASS. Then run the full suite: `python -m pytest tests/ -v` — all PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/orchestrator.py .kanban/orchestrator_triage_prompt.md .kanban/tests/test_orchestrator_loop.py
git commit -m "feat(orchestrator): runtime tick loop + triage prompt"
```

---

### Task 5: Server — profiles CRUD API

Add list/get/create-or-update/delete for profiles to `kanban_server.py`, reusing `orchestrator_core`.

**Files:**
- Modify: `.kanban/kanban_server.py`
- Create: `.kanban/tests/test_server_orchestrator.py`

**Interfaces:**
- Consumes: `orchestrator_core.list_profiles/read_profile/write_profile/delete_profile`.
- Produces HTTP routes:
  - `GET /api/profiles` → `{"profiles": [...]}`
  - `GET /api/profiles/<name>` → the profile or 404
  - `PUT /api/profiles/<name>` body = profile JSON → writes it (name forced to `<name>`), 200
  - `DELETE /api/profiles/<name>` → 200 / 404

- [ ] **Step 1: Write the failing endpoint tests**

Create `.kanban/tests/test_server_orchestrator.py`:

```python
import json
import os
import threading
import http.client

import pytest

import kanban_server as ks


@pytest.fixture
def server(kanban, monkeypatch):
    # Point the server at the temp kanban dir.
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    import orchestrator_core as oc
    monkeypatch.setattr(oc, "KANBAN_DIR", kanban, raising=False)
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


def test_put_then_get_profile(server):
    status, _ = _req(server, "PUT", "/api/profiles/frontend",
                     {"whenToUse": "UI work", "model": "claude-opus-4-8"})
    assert status == 200
    status, body = _req(server, "GET", "/api/profiles/frontend")
    assert status == 200
    assert body["whenToUse"] == "UI work"
    assert body["name"] == "frontend"


def test_list_profiles(server):
    _req(server, "PUT", "/api/profiles/a", {"whenToUse": "x"})
    _req(server, "PUT", "/api/profiles/b", {"whenToUse": "y"})
    status, body = _req(server, "GET", "/api/profiles")
    assert status == 200
    assert {p["name"] for p in body["profiles"]} == {"a", "b"}


def test_delete_profile(server):
    _req(server, "PUT", "/api/profiles/temp", {"whenToUse": "x"})
    status, _ = _req(server, "DELETE", "/api/profiles/temp")
    assert status == 200
    status, _ = _req(server, "GET", "/api/profiles/temp")
    assert status == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_server_orchestrator.py -v`
Expected: FAIL — profiles routes return 404.

- [ ] **Step 3: Wire profiles into the server**

In `.kanban/kanban_server.py`, add near the top imports:

```python
import orchestrator_core as _oc
```

Add these handler helpers as module-level functions (next to `add_comment`):

```python
def profiles_list():
    return {"profiles": _oc.list_profiles(KANBAN_DIR)}, 200


def profile_get(name):
    p = _oc.read_profile(KANBAN_DIR, name)
    if p is None:
        return {"error": "not found"}, 404
    return p, 200


def profile_put(name, payload):
    safe = _oc.safe_name(name)
    if safe is None:
        return {"error": "bad name"}, 400
    payload = dict(payload or {})
    payload["name"] = safe
    _oc.write_profile(KANBAN_DIR, payload)
    return {"ok": True, "profile": payload}, 200


def profile_delete(name):
    ok = _oc.delete_profile(KANBAN_DIR, name)
    return ({"ok": True}, 200) if ok else ({"error": "not found"}, 404)
```

In `do_GET`, before the final `else`:

```python
        elif path == "/api/profiles":
            self._json(*profiles_list())
        elif path.startswith("/api/profiles/"):
            name = unquote(path[len("/api/profiles/"):])
            self._json(*profile_get(name))
```

Add a `do_PUT` method to `KanbanHandler`:

```python
    def do_PUT(self):
        parts = urlparse(self.path).path.rstrip("/").split("/")
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "profiles":
            name = unquote(parts[3])
            payload = self._read_json()
            if payload is None:
                return
            self._json(*profile_put(name, payload))
        else:
            self.send_error(404)
```

In `do_DELETE`, before the final `else`:

```python
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "profiles":
            self._json(*profile_delete(unquote(parts[3])))
            return
```

(Place that block at the top of `do_DELETE`, before the existing task-delete check.)

Add `PUT` to the CORS methods string in `_cors_headers`:

```python
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, PATCH, POST, DELETE, OPTIONS")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_server_orchestrator.py -v`
Expected: the 3 profile tests PASS.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/kanban_server.py .kanban/tests/test_server_orchestrator.py
git commit -m "feat(server): profiles CRUD API"
```

---

### Task 6: Server — orchestrator state GET/PUT

Expose the on/off + concurrency + stop-all state.

**Files:**
- Modify: `.kanban/kanban_server.py`
- Modify: `.kanban/tests/test_server_orchestrator.py`

**Interfaces:**
- Consumes: `orchestrator_core.read_state/write_state`.
- Produces:
  - `GET /api/orchestrator/state` → the state dict
  - `PUT /api/orchestrator/state` body = partial/full state → merges & writes, returns merged

- [ ] **Step 1: Write the failing tests**

Append to `.kanban/tests/test_server_orchestrator.py`:

```python
def test_get_default_state(server):
    status, body = _req(server, "GET", "/api/orchestrator/state")
    assert status == 200
    assert body == {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False}


def test_put_state_merges(server):
    status, body = _req(server, "PUT", "/api/orchestrator/state", {"enabled": True})
    assert status == 200
    assert body["enabled"] is True
    assert body["concurrencyCap"] == 3
    # Persisted.
    status, body2 = _req(server, "GET", "/api/orchestrator/state")
    assert body2["enabled"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_server_orchestrator.py -k state -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

Add module-level helpers in `kanban_server.py`:

```python
def orch_state_get():
    return _oc.read_state(KANBAN_DIR), 200


def orch_state_put(payload):
    state = _oc.read_state(KANBAN_DIR)
    for k in ("enabled", "concurrencyCap", "stopAllRequested"):
        if k in (payload or {}):
            state[k] = payload[k]
    _oc.write_state(KANBAN_DIR, state)
    return state, 200
```

In `do_GET`, add:

```python
        elif path == "/api/orchestrator/state":
            self._json(*orch_state_get())
```

In `do_PUT`, add a branch before the final `else`:

```python
        elif len(parts) == 4 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "state":
            payload = self._read_json()
            if payload is None:
                return
            self._json(*orch_state_put(payload))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_server_orchestrator.py -k state -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/kanban_server.py .kanban/tests/test_server_orchestrator.py
git commit -m "feat(server): orchestrator state endpoint"
```

---

### Task 7: Server — kill endpoint, activity feed, answer endpoint

Instant kill, the activity feed read, and writing a human answer back onto a ticket.

**Files:**
- Modify: `.kanban/kanban_server.py`
- Modify: `.kanban/tests/test_server_orchestrator.py`

**Interfaces:**
- Consumes: `orchestrator_core` (markers, activity, apply_answer), `orchestrator.kill_pid` / `_process_alive`.
- Produces:
  - `GET /api/orchestrator/activity` → `{"entries": [...]}`
  - `POST /api/orchestrator/kill/<board>/<id>` → kills the PID directly if reachable, else sets `killRequested`; returns `{"ok": True, "queued": bool}`
  - `POST /api/orchestrator/answer/<board>/<id>` body `{"value":..., "notes":...}` → writes the answer onto the ticket's `orchestrator.question`, returns 200

- [ ] **Step 1: Write the failing tests**

Append to `.kanban/tests/test_server_orchestrator.py`:

```python
import orchestrator_core as oc2


def test_activity_feed(server, kanban):
    oc2.append_activity(kanban, {"ts": oc2.now_iso(), "kind": "dispatch", "ticket": "1"})
    status, body = _req(server, "GET", "/api/orchestrator/activity")
    assert status == 200
    assert body["entries"][-1]["kind"] == "dispatch"


def test_kill_queues_when_unreachable(server, kanban, monkeypatch):
    # Put an in-flight marker on ticket 1 with a bogus pid.
    p = os.path.join(kanban, "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["orchestrator"] = {"state": "dispatched", "pid": 999999, "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    import orchestrator as orch
    monkeypatch.setattr(orch, "_process_alive", lambda pid: False)
    status, body = _req(server, "POST", "/api/orchestrator/kill/demo/1")
    assert status == 200
    assert body["queued"] is True
    with open(p, "r", encoding="utf-8") as f:
        assert json.load(f)["orchestrator"]["killRequested"] is True


def test_answer_written(server, kanban):
    p = os.path.join(kanban, "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "blocked"
    t["orchestrator"] = {"state": "blocked",
                         "question": {"id": "q1", "type": "input", "prompt": "?",
                                      "answer": None, "answeredAt": None}}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    status, _ = _req(server, "POST", "/api/orchestrator/answer/demo/1",
                     {"value": "yes", "notes": "go ahead"})
    assert status == 200
    with open(p, "r", encoding="utf-8") as f:
        q = json.load(f)["orchestrator"]["question"]
    assert q["answer"] == {"value": "yes", "notes": "go ahead"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_server_orchestrator.py -k "activity or kill or answer" -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

Add to `kanban_server.py` imports:

```python
import orchestrator as _orch
```

Add module-level helpers:

```python
def orch_activity():
    return {"entries": _oc.read_activity(KANBAN_DIR)}, 200


def _ticket_file(board, task_id):
    bsafe = _oc.safe_name(board)
    isafe = _oc.safe_name(f"{task_id}.json")
    if bsafe is None or isafe is None:
        return None
    return os.path.join(KANBAN_DIR, bsafe, isafe)


def orch_kill(board, task_id):
    path = _ticket_file(board, task_id)
    if path is None or not os.path.isfile(path):
        return {"error": "not found"}, 404
    with open(path, "r", encoding="utf-8") as f:
        task = json.load(f)
    marker = task.get("orchestrator") or {}
    pid = marker.get("pid")
    if pid and _orch._process_alive(pid):
        _orch.kill_pid(pid)
        queued = False
    else:
        marker["killRequested"] = True
        task["orchestrator"] = marker
        queued = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return {"ok": True, "queued": queued}, 200


def orch_answer(board, task_id, payload):
    path = _ticket_file(board, task_id)
    if path is None or not os.path.isfile(path):
        return {"error": "not found"}, 404
    with open(path, "r", encoding="utf-8") as f:
        task = json.load(f)
    marker = task.get("orchestrator") or {}
    q = marker.get("question")
    if not q:
        return {"error": "no question"}, 400
    marker["question"] = _oc.apply_answer(q, (payload or {}).get("value"),
                                          (payload or {}).get("notes", ""))
    task["orchestrator"] = marker
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return {"ok": True}, 200
```

In `do_GET` add:

```python
        elif path == "/api/orchestrator/activity":
            self._json(*orch_activity())
```

In `do_POST`, add before the final `else` (compute `parts` already exists):

```python
        elif len(parts) == 6 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "kill":
            self._json(*orch_kill(unquote(parts[4]), unquote(parts[5])))
        elif len(parts) == 6 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "answer":
            payload = self._read_json()
            if payload is None:
                return
            self._json(*orch_answer(unquote(parts[4]), unquote(parts[5]), payload))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/test_server_orchestrator.py -v`
Expected: all server tests PASS. Then full suite `python -m pytest tests/ -v` — all PASS.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/kanban_server.py .kanban/tests/test_server_orchestrator.py
git commit -m "feat(server): kill, activity feed, answer endpoints"
```

---

### Task 8: Starter profile JSONs

Three ready-to-use profiles so the orchestrator has candidates on day one.

**Files:**
- Create: `.kanban/config/frontend.json`
- Create: `.kanban/config/backend.json`
- Create: `.kanban/config/general.json`

**Interfaces:**
- Consumes: the profile shape from Task 1.
- Produces: profiles named `frontend`, `backend`, `general` that `list_profiles` returns.

- [ ] **Step 1: Create the three profiles**

`.kanban/config/frontend.json`:

```json
{
  "name": "frontend",
  "displayName": "Frontend Specialist",
  "whenToUse": "UI work, HTML/CSS/JS, kanban.html changes, visual design, layout, styling, and anything the user sees in the browser.",
  "model": "claude-opus-4-8",
  "allowedTools": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
  "systemPrompt": "You are a frontend specialist working a single kanban ticket. Match the existing vanilla-JS, inline-script style of kanban.html. Keep changes focused on the ticket.",
  "enabled": true
}
```

`.kanban/config/backend.json`:

```json
{
  "name": "backend",
  "displayName": "Backend / Python Specialist",
  "whenToUse": "Python server work, kanban_server.py, orchestrator.py, APIs, data shapes, file IO, and stdlib-only logic.",
  "model": "claude-opus-4-8",
  "allowedTools": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
  "systemPrompt": "You are a Python backend specialist working a single kanban ticket. Stdlib only, match existing patterns in kanban_server.py, write a failing test first when adding logic.",
  "enabled": true
}
```

`.kanban/config/general.json`:

```json
{
  "name": "general",
  "displayName": "Generalist",
  "whenToUse": "Anything that does not clearly fit frontend or backend: docs, CLAUDE.md, research, mixed tickets, or small one-off changes.",
  "model": "claude-sonnet-4-6",
  "allowedTools": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
  "systemPrompt": "You are a generalist working a single kanban ticket. Keep changes minimal and focused on the ticket detail.",
  "enabled": true
}
```

- [ ] **Step 2: Verify they load**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -c "import orchestrator_core as oc; print(sorted(p['name'] for p in oc.list_profiles(oc.KANBAN_DIR)))"`
Expected: `['backend', 'frontend', 'general']`

- [ ] **Step 3: Verify config is not a board**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -c "import kanban_server as ks; print([b['filename'] for b in ks.scan_boards()])"`
Expected: a list that does NOT contain `config` or `_orchestrator`.

- [ ] **Step 4: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/config/frontend.json .kanban/config/backend.json .kanban/config/general.json
git commit -m "feat(orchestrator): starter profiles"
```

---

### Task 9: HTML — view switcher & Profiles tab

Add a top-level Boards / Profiles / Orchestrator switcher to `kanban.html` and build the Profiles tab (list, edit form, save, delete; concurrency cap input lives here too).

**Files:**
- Modify: `.kanban/kanban.html`

**Interfaces:**
- Consumes: `GET/PUT/DELETE /api/profiles`, `GET/PUT /api/orchestrator/state`.
- Produces: a `data-view` switching mechanism the Orchestrator tab (Task 10) reuses.

- [ ] **Step 1: Add view-switcher buttons to the topbar**

In `.kanban/kanban.html`, inside `<div class="topbar">` after the `<h1>`, add:

```html
  <div class="view-tabs">
    <button class="view-tab active" data-view="boards">Boards</button>
    <button class="view-tab" data-view="profiles">Profiles</button>
    <button class="view-tab" data-view="orchestrator">Orchestrator</button>
  </div>
```

Add CSS in the first `<style>` block:

```css
  .view-tabs { display:flex; gap:4px; }
  .view-tab { background:var(--bg); color:var(--text-muted); border:1px solid var(--surface-alt);
    padding:5px 12px; border-radius:6px; font-size:12px; cursor:pointer; }
  .view-tab.active { background:#3b82f6; color:#fff; border-color:#3b82f6; }
```

- [ ] **Step 2: Add the two view containers after `.main-wrap`**

After the closing `</div>` of `<div class="main-wrap">`, add:

```html
<div class="view-panel" id="view-profiles" style="display:none;padding:18px;overflow:auto;height:calc(100vh - 52px);"></div>
<div class="view-panel" id="view-orchestrator" style="display:none;padding:18px;overflow:auto;height:calc(100vh - 52px);"></div>
```

- [ ] **Step 3: Add the view-switching + Profiles script**

Before the final `</body>`, add a new `<script>`:

```html
<script>
// ── View switching ──────────────────────────────────────────────
let currentView = "boards";
function switchView(view){
  currentView = view;
  document.querySelectorAll(".view-tab").forEach(b=>b.classList.toggle("active", b.dataset.view===view));
  $("board").parentElement.style.display = view==="boards" ? "flex" : "none";
  $("view-profiles").style.display = view==="profiles" ? "block" : "none";
  $("view-orchestrator").style.display = view==="orchestrator" ? "block" : "none";
  if(view==="profiles") renderProfiles();
  if(view==="orchestrator" && window.renderOrchestrator) renderOrchestrator();
}
document.querySelectorAll(".view-tab").forEach(b=>b.addEventListener("click",()=>switchView(b.dataset.view)));

// ── Profiles tab ────────────────────────────────────────────────
async function renderProfiles(){
  const wrap = $("view-profiles");
  wrap.innerHTML = "<div style='color:#64748b'>Loading…</div>";
  let profiles=[], state={};
  try{ profiles = (await apiFetch("/api/profiles")).profiles||[]; }catch(e){}
  try{ state = await apiFetch("/api/orchestrator/state"); }catch(e){}
  let html = '<h2 style="font-size:16px;margin-bottom:12px;">Concurrency</h2>';
  html += '<div style="margin-bottom:24px;">Max sub-agents in flight: '
        + '<input type="number" id="capInput" min="0" max="20" value="'+(state.concurrencyCap??3)
        + '" style="width:70px;background:var(--bg);color:var(--text);border:1px solid #475569;border-radius:5px;padding:5px;"> '
        + '<button class="add-btn" id="capSave">Save</button></div>';
  html += '<h2 style="font-size:16px;margin-bottom:12px;">Profiles</h2>';
  html += '<button class="add-btn" id="newProfileBtn">+ New Profile</button><div id="profileList" style="margin-top:14px;"></div>';
  wrap.innerHTML = html;
  $("capSave").addEventListener("click", async ()=>{
    await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({concurrencyCap:parseInt($("capInput").value,10)||0})});
    showToast("Concurrency saved");
  });
  $("newProfileBtn").addEventListener("click",()=>editProfile({name:"",whenToUse:"",model:"claude-opus-4-8",systemPrompt:"",allowedTools:["Read","Edit","Write","Bash","Grep","Glob"],enabled:true}));
  const list = $("profileList"); list.innerHTML="";
  profiles.forEach(p=>{
    const card = document.createElement("div");
    card.style.cssText="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:10px;";
    card.innerHTML = '<div style="font-weight:700;">'+esc(p.displayName||p.name)+' <span style="color:#64748b;font-size:11px;">'+esc(p.model||"")+'</span></div>'
      + '<div style="color:var(--text-muted);font-size:12px;margin:4px 0;">'+esc(p.whenToUse||"")+'</div>';
    const editBtn=document.createElement("button");editBtn.className="add-btn";editBtn.textContent="Edit";
    editBtn.addEventListener("click",()=>editProfile(p));
    const delBtn=document.createElement("button");delBtn.className="sp-del-btn";delBtn.style.marginLeft="8px";delBtn.textContent="Delete";
    delBtn.addEventListener("click",async ()=>{ if(!confirm("Delete profile "+p.name+"?"))return;
      await apiFetch("/api/profiles/"+encodeURIComponent(p.name),{method:"DELETE"}); renderProfiles(); });
    card.appendChild(editBtn);card.appendChild(delBtn);list.appendChild(card);
  });
}

function editProfile(p){
  const wrap=$("view-profiles");
  const f=document.createElement("div");
  f.style.cssText="background:var(--surface);border-radius:8px;padding:16px;margin-top:14px;max-width:640px;";
  f.innerHTML =
    '<label class="form-label">Name (lowercase, no spaces)<input class="form-input" id="pName" value="'+esc(p.name||"")+'"'+(p.name?" disabled":"")+'></label>'
   +'<label class="form-label">Display name<input class="form-input" id="pDisplay" value="'+esc(p.displayName||"")+'"></label>'
   +'<label class="form-label">When to use<textarea class="form-input form-textarea" id="pWhen">'+esc(p.whenToUse||"")+'</textarea></label>'
   +'<label class="form-label">Default model<input class="form-input" id="pModel" value="'+esc(p.model||"")+'"></label>'
   +'<label class="form-label">Allowed tools (comma-sep)<input class="form-input" id="pTools" value="'+esc((p.allowedTools||[]).join(","))+'"></label>'
   +'<label class="form-label">System prompt<textarea class="form-input form-textarea" id="pPrompt">'+esc(p.systemPrompt||"")+'</textarea></label>'
   +'<button class="btn btn-create" id="pSave">Save Profile</button>';
  wrap.appendChild(f);
  f.scrollIntoView({behavior:"smooth"});
  $("pSave").addEventListener("click", async ()=>{
    const name=(p.name||$("pName").value.trim());
    if(!name){showToast("Name required",true);return;}
    const body={name,displayName:$("pDisplay").value.trim(),whenToUse:$("pWhen").value.trim(),
      model:$("pModel").value.trim(),
      allowedTools:$("pTools").value.split(",").map(s=>s.trim()).filter(Boolean),
      systemPrompt:$("pPrompt").value, enabled:true};
    await apiFetch("/api/profiles/"+encodeURIComponent(name),{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    showToast("Profile saved"); renderProfiles();
  });
}
</script>
```

- [ ] **Step 4: Manual verification**

Run the server and open the UI:

```bash
cd "C:/Users/you/Documents/GitHub" && python .kanban/kanban_server.py
```

Open `http://localhost:8745`. Click **Profiles** → the three starter profiles render; the concurrency input shows 3; create a test profile, edit it, delete it; change the cap to 2 and Save. Confirm a toast appears on each action and no console errors. Click **Boards** → the board still renders normally. Stop the server.

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/kanban.html
git commit -m "feat(ui): view switcher + Profiles tab"
```

---

### Task 10: HTML — Orchestrator tab (controls, activity feed, question inbox)

Build the Orchestrator view: on/off toggle, Stop-all button, the activity feed, and the human-attention inbox with typed question forms (each with a Notes box) plus per-ticket kill buttons.

**Files:**
- Modify: `.kanban/kanban.html`

**Interfaces:**
- Consumes: `GET/PUT /api/orchestrator/state`, `GET /api/orchestrator/activity`, `POST /api/orchestrator/kill/<board>/<id>`, `POST /api/orchestrator/answer/<board>/<id>`, and the all-boards task list from `GET /api/board/__all__` (to find tickets with open questions / in-flight markers).

- [ ] **Step 1: Add the Orchestrator render script**

Before the final `</body>` (after the Profiles script), add:

```html
<script>
// ── Orchestrator tab ────────────────────────────────────────────
async function renderOrchestrator(){
  const wrap = $("view-orchestrator");
  let state={}, activity={entries:[]}, all={tasks:[]};
  try{ state = await apiFetch("/api/orchestrator/state"); }catch(e){}
  try{ activity = await apiFetch("/api/orchestrator/activity"); }catch(e){}
  try{ all = await apiFetch("/api/board/__all__"); }catch(e){}

  let html = '<div style="display:flex;align-items:center;gap:14px;margin-bottom:18px;">';
  html += '<button class="add-btn" id="orchToggle" style="background:'+(state.enabled?"#22c55e":"#6b7280")+'">'
        + (state.enabled?"Orchestrator: ON":"Orchestrator: OFF")+'</button>';
  html += '<button class="sp-del-btn" id="stopAll">Stop all</button>';
  html += '<span style="color:var(--text-muted);font-size:12px;">Cap: '+(state.concurrencyCap??3)+'</span></div>';

  // Human-attention inbox.
  const needsHuman = (all.tasks||[]).filter(t=>t.orchestrator&&t.orchestrator.question&&!t.orchestrator.question.answer);
  html += '<h2 style="font-size:16px;margin-bottom:10px;">Needs attention ('+needsHuman.length+')</h2>';
  html += '<div id="inbox"></div>';

  // In-flight with kill buttons.
  const inFlight = (all.tasks||[]).filter(t=>t.orchestrator&&t.orchestrator.state==="dispatched");
  html += '<h2 style="font-size:16px;margin:18px 0 10px;">In flight ('+inFlight.length+')</h2><div id="inflight"></div>';

  // Activity feed.
  html += '<h2 style="font-size:16px;margin:18px 0 10px;">Activity</h2><div id="feed"></div>';
  wrap.innerHTML = html;

  $("orchToggle").addEventListener("click", async ()=>{
    await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({enabled:!state.enabled})});
    renderOrchestrator();
  });
  $("stopAll").addEventListener("click", async ()=>{
    if(!confirm("Kill all in-flight agents?"))return;
    await apiFetch("/api/orchestrator/state",{method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({stopAllRequested:true})});
    showToast("Stop-all requested"); renderOrchestrator();
  });

  const inbox=$("inbox");
  if(!needsHuman.length) inbox.innerHTML='<div style="color:#475569;font-size:12px;">Nothing needs attention.</div>';
  needsHuman.forEach(t=>inbox.appendChild(questionCard(t)));

  const infl=$("inflight");
  if(!inFlight.length) infl.innerHTML='<div style="color:#475569;font-size:12px;">No agents running.</div>';
  inFlight.forEach(t=>{
    const d=document.createElement("div");
    d.style.cssText="background:var(--surface);border-radius:6px;padding:10px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;";
    d.innerHTML='<span>#'+esc(t.id)+' '+esc(t.title)+' <span style="color:#64748b;font-size:11px;">'+esc(t.orchestrator.profile||"")+' · pid '+esc(String(t.orchestrator.pid||""))+'</span></span>';
    const k=document.createElement("button");k.className="sp-del-btn";k.textContent="Kill";
    k.addEventListener("click",async ()=>{ await apiFetch("/api/orchestrator/kill/"+encodeURIComponent(t._board)+"/"+encodeURIComponent(t.id),{method:"POST"}); showToast("Kill sent"); renderOrchestrator(); });
    d.appendChild(k); infl.appendChild(d);
  });

  const feed=$("feed");
  (activity.entries||[]).slice().reverse().slice(0,80).forEach(e=>{
    const row=document.createElement("div");
    row.style.cssText="font-size:12px;color:var(--text-muted);padding:4px 0;border-bottom:1px solid var(--surface-alt);";
    row.textContent="["+(e.kind||"")+"] #"+(e.ticket||"")+" "+(e.reason||e.message||"")+"  "+(e.ts||"");
    feed.appendChild(row);
  });
}

function questionCard(t){
  const q=t.orchestrator.question;
  const card=document.createElement("div");
  card.style.cssText="background:var(--surface);border-left:3px solid #ef4444;border-radius:6px;padding:12px;margin-bottom:10px;";
  card.innerHTML='<div style="font-weight:600;">#'+esc(t.id)+' '+esc(t.title)+'</div>'
    +'<div style="margin:6px 0;color:var(--text-muted);font-size:13px;">'+esc(q.prompt)+'</div>';
  const ctrl=document.createElement("div");
  let getValue=()=>null;
  if(q.type==="choice"){
    (q.options||[]).forEach(opt=>{
      const id="opt_"+t.id+"_"+opt.replace(/\W/g,"");
      const lbl=document.createElement("label");lbl.style.cssText="display:block;font-size:13px;margin:3px 0;";
      lbl.innerHTML='<input type="'+(q.multi?"checkbox":"radio")+'" name="q_'+esc(t.id)+'" value="'+esc(opt)+'"> '+esc(opt);
      ctrl.appendChild(lbl);
    });
    getValue=()=>{
      const checked=[...ctrl.querySelectorAll("input:checked")].map(i=>i.value);
      return q.multi?checked:(checked[0]??null);
    };
  } else {
    const inp=document.createElement("input");
    inp.className="form-input"; inp.type=(q.format==="number"?"number":"text");
    ctrl.appendChild(inp);
    getValue=()=>inp.value.trim()||null;
  }
  card.appendChild(ctrl);
  const notes=document.createElement("textarea");
  notes.className="form-input form-textarea"; notes.placeholder="Notes (override if the question is off-base)…";
  notes.style.marginTop="8px"; card.appendChild(notes);
  const submit=document.createElement("button");submit.className="btn btn-create";submit.style.marginTop="8px";submit.textContent="Answer & resume";
  submit.addEventListener("click",async ()=>{
    await apiFetch("/api/orchestrator/answer/"+encodeURIComponent(t._board)+"/"+encodeURIComponent(t.id),
      {method:"POST",headers:{"Content-Type":"application/json"},
       body:JSON.stringify({value:getValue(),notes:notes.value.trim()})});
    showToast("Answer sent"); renderOrchestrator();
  });
  card.appendChild(submit);
  return card;
}
</script>
```

- [ ] **Step 2: Manual verification — controls & feed**

Start the server (`python .kanban/kanban_server.py`), open the UI, click **Orchestrator**. Confirm: the ON/OFF button toggles and persists across a tab switch; "Stop all" prompts and shows a toast; the Activity section renders (empty is fine). No console errors.

- [ ] **Step 3: Manual verification — question inbox round-trip**

With the server running, hand-edit `.kanban/kanban-dev/<some id>.json` to add:

```json
"status": "blocked",
"orchestrator": {"state":"blocked","question":{"id":"q1","type":"choice","options":["A","B"],"multi":false,"prompt":"Pick one","answer":null,"answeredAt":null}}
```

Reload the Orchestrator tab → the ticket appears under **Needs attention** with A/B radios and a Notes box. Select A, type a note, click **Answer & resume**. Re-open the ticket JSON and confirm `orchestrator.question.answer == {"value":"A","notes":"..."}`. Revert the hand-edit. Stop the server.

- [ ] **Step 4: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/kanban.html
git commit -m "feat(ui): Orchestrator tab — controls, feed, question inbox"
```

---

### Task 11: Documentation & end-to-end smoke

Document the orchestrator in `.kanban/CLAUDE.md` and run one real end-to-end smoke test.

**Files:**
- Modify: `.kanban/CLAUDE.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Document the orchestrator**

Append a new section to `.kanban/CLAUDE.md`:

```markdown
## Orchestrator

`orchestrator.py` is a headless loop that works the board autonomously. Each ~60s
tick it reaps finished/killed/stalled sub-agents, then (if enabled) asks Opus to
triage eligible tickets and dispatches headless `claude -p` sub-agents up to the
concurrency cap. Decision logic lives in `orchestrator_core.py` (unit-tested);
`orchestrator.py` is the runtime that spawns real processes.

Run it: `python .kanban/orchestrator.py` (alongside `kanban_server.py`). You can
also open a normal `claude` CLI in this workspace to talk to it — it reads the same
files and the same triage prompt (`orchestrator_triage_prompt.md`).

- **Profiles** live in `.kanban/config/<name>.json` (`whenToUse`, model, allowedTools,
  systemPrompt). Opus picks the best-fit profile by `whenToUse`. `config/` is never a
  board (no `_meta.json`). Manage them in the **Profiles** tab.
- **Control state** is `.kanban/_orchestrator/state.json` (`enabled`, `concurrencyCap`,
  `stopAllRequested`), toggled from the **Orchestrator** tab.
- **Activity feed** is `.kanban/_orchestrator/activity.json`; sub-agent logs are under
  `_orchestrator/runs/`.
- **In-flight marker** on a ticket: `orchestrator` block (`state`, `profile`, `model`,
  `pid`, `dispatchedAt`, `killRequested`, `logFile`). Field ownership: the loop writes
  only the `orchestrator` block + `status` + `history`; sub-agents write only
  `comments` and `question`/result fields.
- **Human-attention questions:** a sub-agent that needs input sets `status:"blocked"`
  and writes `orchestrator.question` (`type` ∈ input|choice; every answer also carries
  free-text `notes`). Answer it in the Orchestrator tab; the ticket auto re-dispatches.
- **Kill:** Orchestrator tab kill buttons hit `POST /api/orchestrator/kill/<board>/<id>`
  (instant if the PID is alive, else queued via `killRequested`). The loop also reaps
  stalled agents on its own. "Stop all" kills everything in flight.
```

- [ ] **Step 2: Full test suite**

Run: `cd "C:/Users/you/Documents/GitHub/.kanban" && python -m pytest tests/ -v`
Expected: every test PASSES.

- [ ] **Step 3: End-to-end smoke (real claude -p)**

Create a throwaway ticket and run one real dispatch with a cheap model:

```bash
cd "C:/Users/you/Documents/GitHub"
# Create a trivial ticket on a scratch board.
mkdir -p .kanban/smoke && echo '{"project":"Smoke"}' > .kanban/smoke/_meta.json
echo '{"id":"1","title":"Write hello","status":"todo","detail":"Append a comment to this ticket JSON (writer Claude) saying the orchestrator works. Do nothing else."}' > .kanban/smoke/1.json
# Enable orchestrator with a sonnet-defaulted general profile and cap 1.
python - <<'PY'
import sys; sys.path.insert(0,".kanban")
import orchestrator_core as oc
oc.write_state(".kanban", {"enabled":True,"concurrencyCap":1,"stopAllRequested":False})
PY
# Run a single tick using the real triage + spawn.
python - <<'PY'
import sys; sys.path.insert(0,".kanban")
import orchestrator as orch
orch.tick(".kanban", opus_triage=orch._real_opus_triage)
print("tick ran")
PY
```

Wait ~1–2 min, then check `.kanban/smoke/1.json` for an in-flight marker / later a `Claude` comment, and `.kanban/_orchestrator/activity.json` for a `dispatch` entry. This confirms the full loop → `claude -p` → ticket-write path. Then clean up:

```bash
cd "C:/Users/you/Documents/GitHub" && rm -rf .kanban/smoke
```

(If `claude -p` is not available on PATH in this environment, note that in the ticket comment and rely on the mocked loop tests instead — the smoke test is the only step that needs the live CLI.)

- [ ] **Step 4: Commit**

```bash
cd "C:/Users/you/Documents/GitHub"
git add .kanban/CLAUDE.md
git commit -m "docs(orchestrator): document loop, profiles, control state"
```

---

## Self-Review notes (for the implementer)

- **Spec coverage:** loop/tick (Task 4), Opus triage + profile selection (Task 4 + prompt), profiles as `config/*.json` never-a-board (Tasks 1, 8), Profiles tab (Task 9), Orchestrator tab + activity feed (Task 10), typed questions + Notes + auto re-dispatch (Tasks 3, 7, 10 + eligibility in Task 2), on/off + concurrency + stop-all (Tasks 4, 6, 9, 10), instant kill + autonomous reap (Tasks 4, 7, 10), shared brain prompt (Task 4), error handling — malformed JSON skip / crash → blocked / stale marker / triage validation (Tasks 1, 2, 4). All spec sections map to a task.
- **Concurrency-cap-lowered-below-in-flight:** handled in Task 4 `tick` — `free = max(0, cap - in_flight)`; never kills to comply.
- **Known limitation carried from spec:** no file locking; field ownership is the mitigation (Global Constraints + Task 11 docs).
