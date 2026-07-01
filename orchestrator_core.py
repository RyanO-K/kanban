"""Pure decision + IO-helper logic for the kanban orchestrator.

No subprocess launching, no sleeping. Everything here is unit-testable.
The runtime wiring (real `claude -p` processes, kill signals, the tick loop)
lives in orchestrator.py.
"""

import json
import os
import re
from datetime import datetime, timezone

KANBAN_DIR = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.join(KANBAN_DIR, "_orchestrator")
CONFIG_DIR = os.path.join(KANBAN_DIR, "config")

# The orchestrator's own background LLM calls (triage every tick, the pre-kill
# progress summarizer) default to Opus but are configurable so a user can downgrade
# the highest-frequency background cost. Empty/missing falls back to this default.
DEFAULT_LOOP_MODEL = "claude-opus-4-8"

DEFAULT_STATE = {"enabled": False, "concurrencyCap": 3,
                 "stopAllRequested": False, "idleSeconds": 600,
                 "tickSeconds": 60, "maxAgentSeconds": 0, "triageTimeoutSeconds": 120,
                 "triageModel": DEFAULT_LOOP_MODEL,
                 "summarizerModel": DEFAULT_LOOP_MODEL,
                 "autoCommit": True, "autoPush": True}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def branch_name(task):
    """The output branch name for a ticket: `ticket/<id>-<slug>`.

    Mirrors the CLAUDE.md git workflow convention (e.g. ticket/25-worktree-guidance).
    The slug is the title lowercased with any run of non-alphanumeric characters
    collapsed to a single hyphen and leading/trailing hyphens trimmed, so the
    result is always a valid git ref. When the title yields no usable slug we fall
    back to `ticket/<id>` so the name is still stable and valid.
    """
    tid = str(task.get("id", "")).strip()
    slug = re.sub(r"[^a-z0-9]+", "-", (task.get("title") or "").lower()).strip("-")
    return f"ticket/{tid}-{slug}" if slug else f"ticket/{tid}"


def read_board_meta(kanban_dir, board):
    """Read a board's `_meta.json`, returning `{}` if it's missing/unreadable.

    The orchestrator works across boards (tickets carry `_board`); the auto-commit
    gate needs a board's `commitRequirements`, which lives in its `_meta.json`.
    """
    if not board:
        return {}
    return _read_json(os.path.join(kanban_dir, board, "_meta.json"), {})


def commit_requirements_met(task, board_meta):
    """Whether a completed ticket may be auto-committed, per the board's commit gate.

    `commitRequirements` (from ticket #31) is a free-text statement of what must hold
    before committing (e.g. "all tests must pass"). The sub-agent — the only thing that
    actually runs the tests — reports the outcome by writing a `commitGate` object on
    the ticket: `{"requirementsMet": <bool>, "summary": "<what it verified>"}`.

    Returns (ok, reason):
      - No `commitRequirements` on the board -> (True, "no commit requirements") — no
        gate, mirroring the prior unconditional behaviour.
      - Requirements set and `commitGate.requirementsMet` is true -> (True, summary).
      - Requirements set but the agent reported a failure -> (False, summary).
      - Requirements set but NO `commitGate` was reported -> (False, explanation): we
        must not commit on an unverified gate.
    """
    requirements = (board_meta or {}).get("commitRequirements")
    if not requirements:
        return True, "no commit requirements"
    gate = (task or {}).get("commitGate")
    if not isinstance(gate, dict):
        return False, ("commit requirements are set but the agent reported no "
                       "commitGate — not committing on an unverified gate")
    summary = gate.get("summary", "")
    if gate.get("requirementsMet") is True:
        return True, summary or "commit requirements reported met"
    return False, summary or "agent reported commit requirements not met"


def use_worktrees(board_meta):
    """Whether this board's tickets should be worked in a git worktree.

    Per-project setting persisted on the board's `_meta.json` as `useWorktrees`
    (toggled from the Project Settings page, ticket #40). Defaults to False — when
    unset or falsey, sub-agents work in place on a branch with no worktree; when
    true they use EnterWorktree / the `git worktree add` fallback. Tolerates the
    stringified "true"/"false" a form control might submit, not just a JSON bool.
    """
    val = (board_meta or {}).get("useWorktrees")
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    return bool(val)


def _coerce_bool(val, default):
    """Coerce a JSON / form-control value to bool, defaulting when unset.

    Tolerates the stringified "true"/"false" a checkbox or form submit might send,
    not just a JSON bool, mirroring `use_worktrees`.
    """
    if val is None:
        return default
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    return bool(val)


def auto_commit_enabled(state):
    """Global kill switch: may the orchestrator auto-commit/publish completed work?

    `autoCommit` lives on the orchestrator state (`state.json`) and defaults to True
    (the prior unconditional behaviour). When False the orchestrator still dispatches
    and reaps tickets, but `_finish_completion` records the pending output in a comment
    and skips ALL git mutation — so a user can review diffs before committing. Tolerates
    a stringified bool from a form control.
    """
    return _coerce_bool((state or {}).get("autoCommit"), True)


def auto_push_enabled(state):
    """Global kill switch: may the orchestrator push committed work to the remote?

    `autoPush` lives on the orchestrator state and defaults to True. When False the
    orchestrator still commits locally (so the work is captured on a branch) but never
    runs `git push`, leaving the human to push after review. Independent of
    `autoCommit`: turning push off keeps local commits; turning commit off skips both.
    """
    return _coerce_bool((state or {}).get("autoPush"), True)


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


# --- single-instance lock ---
#
# Only one orchestrator tick loop may run across all processes (multiple kanban
# servers can be up at once). We use an atomic O_CREAT|O_EXCL lock file holding
# the owner's pid. A lock whose pid is no longer alive is treated as stale and
# reclaimed, so a crashed/killed owner never wedges the orchestrator permanently.

def _lock_path(kanban_dir):
    return os.path.join(kanban_dir, "_orchestrator", "orchestrator.lock")


def _pid_alive(pid):
    """Best-effort cross-platform liveness check for a stale-lock holder."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True,
            )
            quoted = f'"{pid}"'
            for line in out.stdout.splitlines():
                fields = line.split(",")
                if len(fields) >= 2 and fields[1].strip() == quoted:
                    return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def acquire_lock(kanban_dir):
    """Try to become the single orchestrator owner.

    Returns True if we acquired the lock (and wrote our pid into it), False if
    another live process already holds it. A lock left by a dead pid is reclaimed.
    """
    path = _lock_path(kanban_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except FileExistsError:
        # Lock exists — check whether its owner is still alive.
        try:
            with open(path, "r", encoding="utf-8") as f:
                owner = int((f.read() or "0").strip() or 0)
        except (OSError, ValueError):
            owner = 0
        if owner == os.getpid():
            return True  # re-entrant: we already own it
        if _pid_alive(owner):
            return False
        # Stale lock from a dead owner: reclaim it.
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            return True
        except OSError:
            return False


def release_lock(kanban_dir):
    """Release the lock if we own it (best-effort; safe to call always)."""
    path = _lock_path(kanban_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            owner = int((f.read() or "0").strip() or 0)
        if owner == os.getpid():
            os.remove(path)
    except (OSError, ValueError):
        pass


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


# --- usage limits (ticket #60) ---
#
# A `claude -p` sub-agent (or a background triage/summarizer call) can hit a
# Claude usage limit. In headless print mode the CLI exits non-zero and emits a
# "Claude AI usage limit reached|<reset-epoch>" line. Without special handling
# that exit looks like a crash, so the ticket would be wrongly blocked for a
# human and the orchestrator would keep re-dispatching tickets straight into the
# same limit. Instead we detect the signal, re-queue the ticket, and park
# dispatch in a sidecar file until the reset time — the tick loop then resumes
# automatically (a self-restart) once the window clears.

# Default park duration when the CLI gives no parseable reset time. Claude usage
# limits run on a rolling ~5-hour window, so wait that long before probing again.
DEFAULT_USAGE_RESET_SECONDS = 5 * 3600

_USAGE_LIMIT_RE = re.compile(r"usage limit", re.IGNORECASE)
_RESET_EPOCH_RE = re.compile(r"(\d{10,13})")


def parse_usage_limit(text):
    """Detect a Claude usage-limit signal in an agent's log/CLI output.

    Returns `None` when no usage-limit phrase is present. Otherwise returns
    `{"resetAt": <int unix-seconds> | None}`. The reset epoch is read from the
    first 10–13 digit run AT OR AFTER the "usage limit" phrase (the canonical
    print-mode form is `...usage limit reached|<epoch>`), so an unrelated long
    number elsewhere in the log is not mistaken for a reset time. A 13-digit
    value is treated as milliseconds and normalised to whole seconds.
    """
    if not text:
        return None
    m = _USAGE_LIMIT_RE.search(text)
    if not m:
        return None
    reset_at = None
    em = _RESET_EPOCH_RE.search(text, m.start())
    if em:
        val = int(em.group(1))
        if val >= 10 ** 12:  # milliseconds → seconds
            val //= 1000
        reset_at = val
    return {"resetAt": reset_at}


def _usage_pause_path(kanban_dir):
    return os.path.join(kanban_dir, "_orchestrator", "usage_pause.json")


def read_usage_pause(kanban_dir):
    """The current usage-limit pause record, or `{}` if none/unreadable.

    Stored in a dedicated sidecar (not `state.json`) so it never collides with
    user-controlled control flags, mirroring the idle-tracking sidecar.
    """
    data = _read_json(_usage_pause_path(kanban_dir), {})
    return data if isinstance(data, dict) else {}


def set_usage_pause(kanban_dir, reset_at, now_ts, *,
                    default_seconds=DEFAULT_USAGE_RESET_SECONDS, reason=""):
    """Park dispatch until a usage limit resets.

    `reset_at` is the reset epoch parsed from the CLI (may be None). When it is
    missing or already in the past we fall back to `now_ts + default_seconds` so
    a stale/absent reset never leaves the orchestrator either un-paused or paused
    forever. Returns the written record.
    """
    if reset_at and reset_at > now_ts:
        paused_until = int(reset_at)
    else:
        paused_until = int(now_ts + default_seconds)
    record = {"pausedUntil": paused_until, "since": now_iso(), "reason": reason}
    _write_json(_usage_pause_path(kanban_dir), record)
    return record


def usage_pause_remaining(kanban_dir, now_ts):
    """Seconds of pause left (0 if not paused or already expired)."""
    paused_until = read_usage_pause(kanban_dir).get("pausedUntil")
    if not paused_until:
        return 0
    return max(0, paused_until - now_ts)


def is_usage_paused(kanban_dir, now_ts):
    """Whether dispatch is currently parked by a usage-limit pause."""
    return usage_pause_remaining(kanban_dir, now_ts) > 0


def clear_usage_pause(kanban_dir):
    """Remove any usage-limit pause (best-effort; safe to call always)."""
    try:
        os.remove(_usage_pause_path(kanban_dir))
    except OSError:
        pass


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


# Statuses that count as "finished". The board/server treats `done` and
# `completed` as the same column, so eligibility and dependency-satisfaction
# must accept either spelling.
_DONE_STATES = ("completed", "done")


def _dep_met_fn(tasks):
    """Build a per-board dependency-satisfaction predicate over `tasks`.

    Ticket ids are unique only WITHIN a board, so dependency resolution must be
    scoped per board. A flat id->status map would let a same-id ticket on another
    board satisfy (or break) a dependency. Key by (board, id); fall back to a
    global id map only for tasks that carry no board.
    """
    status_by_board_id = {}
    status_by_id = {}
    for t in tasks:
        sid = str(t.get("id"))
        status_by_id[sid] = t.get("status")
        status_by_board_id[(t.get("_board"), sid)] = t.get("status")

    def dep_met(board, dep_id):
        dep_id = str(dep_id)
        if board is not None and (board, dep_id) in status_by_board_id:
            return status_by_board_id[(board, dep_id)] in _DONE_STATES
        # A dependency counts as met only if it exists AND is finished.
        return status_by_id.get(dep_id) in _DONE_STATES

    return dep_met


def _deps_of(task):
    deps = task.get("dependsOn") or []
    if isinstance(deps, str):
        deps = [deps]
    return deps


def promotable_tickets(tasks):
    """Tickets currently in `todo` whose dependencies are all met.

    These are ready to start work but not yet queued, so the tick loop promotes
    them to `ready`. Dependency resolution is per-board, mirroring eligibility.
    """
    dep_met = _dep_met_fn(tasks)
    out = []
    for t in tasks:
        if t.get("status") != "todo":
            continue
        if all(dep_met(t.get("_board"), d) for d in _deps_of(t)):
            out.append(t)
    return out


def eligible_tickets(tasks):
    """Tickets the orchestrator may dispatch right now.

    Fresh work is dispatched from `ready` (the promotion gate already cleared
    each ticket's dependencies — see `promotable_tickets`). A `blocked` ticket
    whose question has been answered re-dispatches regardless of status.
    """
    out = []
    for t in tasks:
        if is_in_flight(t):
            continue
        # A blocked ticket with an answered question re-dispatches.
        if t.get("status") == "blocked" and _has_answered_question(t):
            out.append(t)
            continue
        if t.get("status") == "ready":
            out.append(t)
    return out


def validate_triage(response, profile_names, eligible_ids):
    """Filter a triage response down to dispatchable items, keyed by (board, id).

    Ticket ids are unique only WITHIN a board, so an id alone can be ambiguous.
    `eligible_ids` is a set whose members are either `(board, id)` tuples or — for
    board-agnostic callers (the unit tests, a single-board id with no collision) —
    bare id strings. A dispatch item matches when its `(board, ticket)` pair is
    eligible, or, only when no explicit board is given, when its bare ticket id is.
    Duplicate `(board, ticket)` entries are collapsed so the spawn loop never
    double-dispatches one ticket.
    """
    if not isinstance(response, dict):
        return []
    items = response.get("dispatch")
    if not isinstance(items, list):
        return []
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        ticket = str(item.get("ticket", ""))
        board = item.get("board")
        profile = item.get("profile", "")
        if profile not in profile_names:
            continue
        if board is not None and (board, ticket) in eligible_ids:
            pass
        elif board is None and ticket in eligible_ids:
            pass
        else:
            continue
        key = (board, ticket)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "ticket": ticket,
            "board": board,
            "profile": profile,
            "model": item.get("model"),
            "reason": item.get("reason", ""),
        })
    return out


# --- reap decisions & questions ---

def agent_left_signal(task):
    """Classify what a sub-agent left behind in its ticket, for reaping an
    *adopted* agent (one whose pid we can no longer match to a live Popen,
    e.g. after a server restart). We can't read its exit code, so we infer
    success from its own writes.

    Returns:
        "question" — it asked for a human (orchestrator.question present).
        "progress" — it reported done: a Claude-authored comment exists, OR it
                     moved its own status off the dispatched 'in_progress'
                     (and didn't merely reset to 'todo').
        "none"     — it left no trace (a genuine orphan / crash).

    A pending question takes precedence over a done-comment.
    """
    marker = get_marker(task) or {}
    if marker.get("question"):
        return "question"
    for c in task.get("comments", []) or []:
        if c.get("writer") == "Claude":
            return "progress"
    status = task.get("status")
    if status not in ("in_progress", "todo"):
        return "progress"
    return "none"


def note_log_growth(marker, current_size, now_iso):
    """Record a sub-agent's log growth on its marker for idle-stall tracking.

    The streamed log grows as the agent works; a growing log means it's alive.
    Update `logSize` to the latest size, and bump `lastGrowthAt` whenever the log
    grew (or seed it if it was never set). When the log did NOT grow, leave
    `lastGrowthAt` alone so the idle clock keeps counting from the last activity.
    """
    prev = marker.get("logSize", 0)
    if current_size > prev or "lastGrowthAt" not in marker:
        marker["lastGrowthAt"] = now_iso
    marker["logSize"] = current_size
    return marker


def backfill_dispatch(eligible, chosen, profiles, free):
    """Fill leftover free dispatch slots greedily so the cap drives the count.

    Triage may name fewer tickets than there are free slots. For each remaining
    eligible ticket (in order) not already chosen, assign a best-fit profile in
    code until `free` total is reached. Best-fit = first profile with a non-empty
    `whenToUse`, else the first profile. Returns the extra dispatch items (may be
    empty). No-op when there are no profiles.
    """
    if not profiles:
        return []
    profile = next((p for p in profiles if p.get("whenToUse")), profiles[0])
    # Key by (board, id): ids collide across boards, so the bare-id keying used to
    # mask a same-id ticket on another board. A chosen item may name a ticket
    # board-lessly (an unambiguous id); resolve it to its board so we don't re-add
    # the same ticket.
    board_of = {}
    for t in eligible:
        board_of.setdefault(str(t.get("id")), t.get("_board"))
    used = set()
    for item in chosen:
        tid = str(item.get("ticket"))
        board = item.get("board")
        if board is None:
            board = board_of.get(tid)
        used.add((board, tid))
    out = []
    for t in eligible:
        if len(chosen) + len(out) >= free:
            break
        tid = str(t.get("id"))
        key = (t.get("_board"), tid)
        if key in used:
            continue
        out.append({"ticket": tid, "board": t.get("_board"),
                    "profile": profile["name"],
                    "model": profile.get("model"), "reason": "backfill"})
        used.add(key)
    return out


def reap_decision(task, *, alive, exit_code, now_ts, dispatched_ts,
                  stall_seconds=900, adopted=False,
                  idle_seconds=None, last_growth_ts=None,
                  max_agent_seconds=None):
    marker = get_marker(task) or {}
    if marker.get("killRequested"):
        return {"action": "kill_requested"}
    if not alive:
        if adopted:
            # We can't trust exit_code for an adopted agent (we never held its
            # Popen). Judge it by what it left in the ticket instead.
            signal = agent_left_signal(task)
            if signal == "question":
                return {"action": "needs_human"}
            if signal == "progress":
                return {"action": "completed"}
            return {"action": "crashed", "exit_code": exit_code}
        if exit_code == 0:
            if marker.get("question"):
                return {"action": "needs_human"}
            return {"action": "completed"}
        return {"action": "crashed", "exit_code": exit_code}
    # Absolute wall-clock cap: stall regardless of log growth when set and exceeded.
    if max_agent_seconds and now_ts - dispatched_ts >= max_agent_seconds:
        return {"action": "stalled"}
    # Idle-based stall when idle_seconds is supplied (real progress signal);
    # otherwise fall back to legacy wall-clock-since-dispatch.
    if idle_seconds is not None:
        base = last_growth_ts if last_growth_ts is not None else dispatched_ts
        if now_ts - base >= idle_seconds:
            return {"action": "stalled"}
        return {"action": "running"}
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
