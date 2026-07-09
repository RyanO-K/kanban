"""Pure decision + IO-helper logic for the kanban orchestrator.

No subprocess launching, no sleeping. Everything here is unit-testable.
The runtime wiring (real `claude -p` processes, kill signals, the tick loop)
lives in orchestrator.py.
"""

import json
import os
import re
from datetime import datetime, timezone

# This module lives in .kanban/app/, so the board root is its parent dir.
KANBAN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCH_DIR = os.path.join(KANBAN_DIR, "_orchestrator")
CONFIG_DIR = os.path.join(KANBAN_DIR, "config")

# Board directories live under a dedicated, gitignored `boards/` folder (ticket
# #94) rather than loose at the .kanban root. Every per-board path resolution
# routes through boards_root() so the location is defined in one place.
BOARDS_SUBDIR = "boards"


def boards_root(kanban_dir):
    """The folder holding every board directory for a given .kanban tree."""
    return os.path.join(kanban_dir, BOARDS_SUBDIR)


def board_path(kanban_dir, board):
    """Absolute path to a single board's directory under boards_root()."""
    return os.path.join(boards_root(kanban_dir), board)

# The orchestrator's own background LLM calls (triage every tick, the pre-kill
# progress summarizer) default to Opus but are configurable so a user can downgrade
# the highest-frequency background cost. Empty/missing falls back to this default.
DEFAULT_LOOP_MODEL = "claude-opus-4-8"

# Fable 5 is a model option for sub-agents but may not always be available.
# When selected, the orchestrator probes availability at dispatch time and
# substitutes FABLE_FALLBACK_MODEL if the model is not accessible.
FABLE_MODEL = "claude-fable-5"
FABLE_FALLBACK_MODEL = "claude-opus-4-8"

DEFAULT_STATE = {"enabled": False, "concurrencyCap": 3,
                 "stopAllRequested": False, "idleSeconds": 600,
                 "tickSeconds": 60, "maxAgentSeconds": 0, "triageTimeoutSeconds": 120,
                 "triageModel": DEFAULT_LOOP_MODEL,
                 "summarizerModel": DEFAULT_LOOP_MODEL,
                 "autoCommit": True, "autoPush": True}


# --- Stream-json log parsing (shared with kanban_server) --------------------
#
# Moved here from kanban_server so orchestrator.py can call parse_log_turns
# when saving a completed ticket's log without creating a circular import.

_TOOL_RESULT_PREVIEW = 6000


def _tool_summary(name, tool_input):
    """A short, human-readable note of what a tool_use block is doing."""
    if not isinstance(tool_input, dict):
        return ""
    inp = tool_input
    if name in ("Read", "Write", "NotebookEdit") and inp.get("file_path"):
        return str(inp["file_path"])
    if name == "Edit" and inp.get("file_path"):
        return str(inp["file_path"])
    if name in ("Bash", "PowerShell") and inp.get("command"):
        return str(inp["command"])
    if name == "Glob" and inp.get("pattern"):
        return str(inp["pattern"])
    if name == "Grep" and inp.get("pattern"):
        return str(inp["pattern"])
    if name == "Skill" and inp.get("skill"):
        return str(inp["skill"])
    if name in ("Task", "Agent") and inp.get("description"):
        return str(inp["description"])
    for v in inp.values():
        if isinstance(v, str) and v:
            return v
    return ""


def _result_preview(content):
    """Flatten a tool_result `content` into a short text preview."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    else:
        text = ""
    text = text.strip()
    if len(text) > _TOOL_RESULT_PREVIEW:
        text = text[:_TOOL_RESULT_PREVIEW] + "…"
    return text


def _nearest_line_ts(line_ts, idx):
    """The timestamp of the nearest timestamped line at/after `idx`, else the
    nearest one before it, else None. `line_ts` is [(line_idx, ts), ...] sorted."""
    preceding = None
    for i, ts in line_ts:
        if i >= idx:
            return ts
        preceding = ts
    return preceding


def parse_log_turns(text, n=20):
    """Parse stream-json log *text* into the last *n* compact turn objects.

    Pure (no I/O) so it is unit-testable. Each line is one JSON object; only
    `assistant` lines become turns. A turn is `{seq, role, text, tools}`:
      - text:  concatenated text/thinking blocks (the agent's thoughts).
      - tools: a chip per tool_use: `{name, summary, result}`.
      - timestamp: the line's own top-level timestamp when present; the CLI
        stamps only `user`/tool_result lines, so assistant turns borrow the
        nearest following (else preceding) timestamped line's value.

    Tool results are folded into the chip of the tool_use they answer (matched
    by tool_use_id). Housekeeping lines and empty turns are dropped; malformed /
    non-JSON lines are skipped.
    """
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except (ValueError, TypeError):
            continue

    results_by_id = {}
    line_ts = []  # (line_idx, timestamp) for every line that carries one
    for i, obj in enumerate(lines):
        if isinstance(obj, dict) and obj.get("timestamp"):
            line_ts.append((i, obj["timestamp"]))
        content = (obj.get("message") or {}).get("content") if isinstance(obj, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    results_by_id[tid] = _result_preview(block.get("content"))

    turns = []
    for idx, obj in enumerate(lines):
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message") or {}
        content = message.get("content")
        text_parts = []
        tools = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text_parts.append(str(block.get("text", "")))
                elif bt == "thinking":
                    text_parts.append(str(block.get("thinking", "")))
                elif bt == "tool_use":
                    tools.append({
                        "name": str(block.get("name", "tool")),
                        "summary": _tool_summary(block.get("name"), block.get("input")),
                        "result": results_by_id.get(block.get("id"), ""),
                    })
        turn_text = "\n".join(p for p in text_parts if p).strip()
        if not turn_text and not tools:
            continue
        turn = {"role": "assistant", "text": turn_text, "tools": tools}
        ts = obj.get("timestamp") or _nearest_line_ts(line_ts, idx)
        if ts:
            turn["timestamp"] = ts
        turns.append(turn)
    if n and len(turns) > n:
        turns = turns[-n:]
    for i, t in enumerate(turns):
        t["seq"] = i
    return turns


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Agent chat (stdin injection) --------------------------------------------
#
# A human can message a RUNNING agent (spec docs/specs/2026-07-03-agent-chat-
# design.md): the kanban server appends lines to a per-run inbox file under
# CHAT_DIR and the orchestrator's per-run pump thread relays them to the agent
# process's stdin as stream-json user messages. The helpers here are pure so
# the server, the pump, and the tests share one encoding/decision
# implementation.

# One-line escape hatch: set False to restore the legacy argv-prompt dispatch
# (no streaming input, no pump threads; the chat endpoint then returns
# 409 {"error": "chat disabled"}).
CHAT_ENABLED = True

CHAT_DIR = os.path.join(ORCH_DIR, "chat")


def chat_inbox_path(board, ticket_id):
    """The chat inbox file for one ticket's run: <CHAT_DIR>/<board>__<id>.jsonl.

    Double underscore separates the board dir name from the id; both are
    filesystem-safe already (same convention as the idle sidecar files).
    Reads the module-global CHAT_DIR at call time so tests can repoint it.
    """
    return os.path.join(CHAT_DIR, f"{board}__{ticket_id}.jsonl")


def chat_encode_user_message(text):
    """One stream-json input line carrying *text* as a user message.

    This exact shape is what `claude -p --input-format stream-json` consumes;
    the initial prompt and every relayed chat message use it. Returns a single
    JSON line terminated by \\n.
    """
    return json.dumps(
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": text}]}},
        ensure_ascii=False) + "\n"


def chat_parse_inbox_line(line):
    """Parse one inbox JSONL line into {"message", "writer", "ts"}, or None.

    Malformed JSON, a non-dict payload, or a missing/empty/non-string
    `message` all return None — the pump skips such lines. A missing writer
    defaults to "unknown", a missing ts to "".
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    msg = obj.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return None
    return {"message": msg,
            "writer": str(obj.get("writer") or "unknown"),
            "ts": str(obj.get("ts") or "")}


def chat_should_close(result_seen_after_last_send, inbox_empty):
    """True when the pump should close the child's stdin, ending the run.

    Close only when (a) the CLI has emitted a top-level result SINCE the last
    user message we injected AND (b) no unsent inbox message is queued. If a
    chat message arrives before close, it is sent instead and the agent runs
    another turn; the next result re-arms the decision.
    """
    return bool(result_seen_after_last_send) and bool(inbox_empty)


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
    return _read_json(os.path.join(board_path(kanban_dir, board), "_meta.json"), {})


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


# --- Docker dev-workspace support (ticket #16) ---
#
# Option A of the containerization ticket: instead of running the dispatched
# `claude -p` agent as a plain host subprocess, the orchestrator can build one
# Docker image per board repo and run the agent INSIDE a container mounted on
# the workspace. Env vars for that container are editable per-board (Project
# Settings -> stored as `envVars` on _meta.json) and handed to the container via
# a rendered `--env-file`. Everything in this section is pure argv/text/config
# logic so it is unit-testable on any host — the real `docker build`/`docker run`
# calls live in orchestrator.py.

# Image repository namespace; the board slug becomes the tag.
DOCKER_IMAGE_PREFIX = "ai-kanban-workspace"
# Path INSIDE the container where the workspace root (parent of .AI-kanban) is
# mounted. The agent's prompt carries host paths; we translate them onto this.
CONTAINER_WORKSPACE = "/workspace"

# A valid POSIX/shell environment-variable name: letter/underscore then
# letters/digits/underscores. Anything else can't be a real env var and is
# dropped so a malformed settings payload can't inject junk into the container.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def use_docker(board_meta):
    """Whether this board's agents run inside a per-repo Docker container (ticket #16).

    Per-project setting persisted on the board's `_meta.json` as `useDocker`
    (toggled from the Project Settings page). Defaults to False so existing
    boards are unaffected — the agent keeps running as a plain host subprocess.
    Tolerates a stringified bool from a form control, like `use_worktrees`.
    """
    return _coerce_bool((board_meta or {}).get("useDocker"), False)


def valid_env_key(key):
    """True if `key` is a usable environment-variable name."""
    return isinstance(key, str) and bool(_ENV_KEY_RE.match(key))


def board_env_vars(board_meta):
    """Sanitized `{KEY: VALUE}` env map from a board's `envVars` (ticket #16).

    Only entries whose key is a valid env-var name survive; scalar values
    (str/int/float/bool) are coerced to their string form and `None` is dropped.
    Any other shape is ignored, so a malformed Project-Settings payload can never
    put non-string junk into the container environment.
    """
    raw = (board_meta or {}).get("envVars")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if not valid_env_key(key):
            continue
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str):
            continue
        out[key] = value
    return out


def board_passthrough_env(board_meta):
    """De-duplicated list of valid env-var NAMES from a board's `passthroughEnv`.

    `passthroughEnv` (ticket #7) is the secret-name counterpart to `envVars`: it
    lists environment-variable NAMES the orchestrator forwards into the container
    with a bare `docker run -e NAME`, so each value is inherited from the
    orchestrator's own environment and is NEVER written to `_meta.json`, the
    rendered env-file, or the image. That is how a board gets a non-Anthropic
    secret (GitHub push token, `DATABASE_URL`, `RENDER_API_KEY`, Discord token)
    into its container without any value touching disk.

    Only entries that are valid env-var names survive (`valid_env_key`); invalid
    or non-string entries are dropped. Order is preserved and duplicates removed,
    so a malformed Project-Settings payload can never inject junk names. A
    non-list (or missing) `passthroughEnv` yields an empty list.
    """
    raw = (board_meta or {}).get("passthroughEnv")
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for name in raw:
        if not valid_env_key(name) or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def render_env_file(env):
    """Render an env map to Docker `--env-file` text: one `KEY=VALUE` per line.

    The `--env-file` format is line-based, so a newline in a value would corrupt
    the file; carriage returns are dropped and newlines flattened to spaces. Keys
    are assumed already validated by `board_env_vars`. Returns "" for an empty map
    (Docker accepts an empty env-file).
    """
    lines = []
    for key, value in (env or {}).items():
        flat = str(value).replace("\r", "").replace("\n", " ")
        lines.append(f"{key}={flat}")
    return ("\n".join(lines) + "\n") if lines else ""


def _docker_safe(name):
    """Lowercase `name` to a Docker-safe token (image-tag / container-name part).

    Docker repository tags and container names allow only a restricted charset;
    everything outside `[a-z0-9_.-]` collapses to a single '-', and leading/
    trailing separators are trimmed. Empty input yields 'workspace' so the result
    is always a valid, non-empty token.
    """
    safe = re.sub(r"[^a-z0-9_.-]+", "-", (name or "").lower()).strip("-._")
    return safe or "workspace"


def docker_image_tag(board):
    """The image tag built for a board repo, e.g. `ai-kanban-workspace:demo`."""
    return f"{DOCKER_IMAGE_PREFIX}:{_docker_safe(board)}"


def docker_container_name(board, task):
    """The container name for a board+ticket run, e.g. `ai-kanban-workspace-demo-16`.

    Deterministic so the orchestrator can `docker kill` it by name on reap even
    after a loop restart (when it no longer holds the client Popen). Falls back to
    the board-only name when the ticket has no id.
    """
    raw = str((task or {}).get("id", "")).strip()
    base = f"{DOCKER_IMAGE_PREFIX}-{_docker_safe(board)}"
    return f"{base}-{_docker_safe(raw)}" if raw else base


def translate_host_paths(text, host_root, container_root=CONTAINER_WORKSPACE):
    """Rewrite host workspace-root paths in `text` onto the container mount.

    Inside the container only the workspace root is mounted (at `container_root`),
    so a host path like `C:\\Users\\me\\Github\\.AI-kanban\\demo\\16.json` in the
    agent prompt must become `/workspace/.AI-kanban/demo/16.json`. We match the
    host root in both its native and forward-slash forms and normalize the
    backslashes in the matched tail. Best-effort: if `host_root` is empty or never
    appears, the text is returned unchanged.
    """
    if not host_root or not text:
        return text
    root = os.path.abspath(host_root).rstrip("\\/")
    variants = {root, root.replace("\\", "/")}
    result = text
    for variant in sorted(variants, key=len, reverse=True):
        if not variant:
            continue
        idx = result.find(variant)
        while idx != -1:
            end = idx + len(variant)
            # Consume the path tail (until whitespace) and flip its separators.
            tail_end = end
            while tail_end < len(result) and not result[tail_end].isspace():
                tail_end += 1
            tail = result[end:tail_end].replace("\\", "/")
            replacement = container_root + tail
            result = result[:idx] + replacement + result[tail_end:]
            idx = result.find(variant, idx + len(replacement))
    return result


def docker_build_argv(image_tag, dockerfile_path, context_dir):
    """The `docker build` argv that produces a board's workspace image."""
    return ["docker", "build", "-t", image_tag,
            "-f", dockerfile_path, context_dir]


def docker_run_argv(image_tag, container_name, mount_src, env_file, inner_argv,
                    container_workdir=CONTAINER_WORKSPACE, passthrough_env=None,
                    interactive=False):
    """The `docker run` argv that runs `inner_argv` inside the workspace image.

    - `--rm` so the container is discarded on exit (its work is on the mounted
      volume, persisted to the host).
    - `-i` (when `interactive`) keeps the container's stdin attached to the
      host `docker run` client so agent chat can stream stream-json input
      through it (spec 2026-07-03).
    - `--name` fixes the container name so reap can `docker kill` it by name.
    - `-v mount_src:/workspace` mounts the whole workspace root, giving the agent
      both its board repo and the `.AI-kanban` tree (its ticket JSON lives there).
    - `--env-file` supplies the board's editable env vars.
    - `passthrough_env` names host env vars to forward with bare `-e NAME`
      (value inherited from the orchestrator's own environment) — used for
      secrets like `ANTHROPIC_API_KEY` that shouldn't be written into _meta.json.
    """
    argv = ["docker", "run", "--rm"]
    if interactive:
        argv.append("-i")
    argv += ["--name", container_name,
             "-v", f"{mount_src}:{CONTAINER_WORKSPACE}",
             "-w", container_workdir]
    if env_file:
        argv += ["--env-file", env_file]
    for name in (passthrough_env or []):
        argv += ["-e", name]
    argv.append(image_tag)
    argv += list(inner_argv)
    return argv


def board_dockerfile_name(board):
    """Filename of a board's REQUIRED per-board Dockerfile, e.g. `demo.Dockerfile`.

    A `useDocker` board must build from its own Dockerfile — the generic
    `node:20-slim` template can't run most boards' tests (ticket #6). This is the
    name the orchestrator looks for under `_orchestrator/docker/`; the slug is
    Docker-safe so the name is always valid and non-empty.
    """
    return f"{_docker_safe(board)}.Dockerfile"


def resolve_board_dockerfile(docker_dir, board):
    """Absolute path to a board's per-board Dockerfile if it exists, else None.

    Looks only for `<docker_dir>/<board-slug>.Dockerfile`. Deliberately does NOT
    fall back to the generic `Dockerfile`: a `useDocker` board with no per-board
    file must be blocked (see `docker_preflight`), not silently built from an
    image that can't run its tests.
    """
    if not docker_dir:
        return None
    path = os.path.join(docker_dir, board_dockerfile_name(board))
    return path if os.path.isfile(path) else None


def docker_preflight(docker_dir, board_meta, board):
    """Whether a board may be dispatched in Docker mode; returns `(ok, reason)`.

    `ok` is True when either the board does not use Docker (it never runs in a
    container, so no Dockerfile is needed) OR it uses Docker AND a per-board
    Dockerfile exists at `<docker_dir>/<board-slug>.Dockerfile`. When `useDocker`
    is on but the per-board file is missing, `ok` is False and `reason` is an
    actionable instruction naming the file to create — the orchestrator blocks the
    ticket with it instead of silently building the wrong (generic) image.
    """
    if not use_docker(board_meta):
        return True, "docker not enabled"
    if resolve_board_dockerfile(docker_dir, board):
        return True, "per-board Dockerfile present"
    name = board_dockerfile_name(board)
    reason = (
        f"Board '{board}' has useDocker enabled but no per-board Dockerfile. "
        f"Create _orchestrator/docker/{name} so the container has the toolchain "
        f"(e.g. Python/pytest) needed to run this board's tests — copy the generic "
        f"_orchestrator/docker/Dockerfile as a starting template and add the deps. "
        f"The generic node image is NOT used as a fallback. Once the file exists, "
        f"answer this question (any note) to re-dispatch the ticket."
    )
    return False, reason


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

# The CLI has used several wordings for the same condition: the classic
# "Claude AI usage limit reached|<epoch>" and (CLI ~2.1.x) "You've hit your
# session limit · resets 2:10pm" — match either noun so a wording change
# doesn't silently turn limits back into "crashes" (ticket #99 regression).
_USAGE_LIMIT_RE = re.compile(r"(?:usage|session) limit", re.IGNORECASE)
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


# stream-json line types that can legitimately carry the CLI's OWN terminal
# status (as opposed to "user"/"assistant" content blocks, which just echo
# whatever the sub-agent read or wrote — e.g. a Read/cat of orchestrator_core.py
# itself, whose comments and docstrings talk about "usage limit" detection).
_STATUS_LINE_TYPES = {"result", "system"}


def usage_limit_from_transcript_tail(tail):
    """Detect a genuine usage-limit signal in a raw log tail, ignoring the
    phrase when it only appears inside echoed tool/file content.

    `tail` may be a stream-json transcript (one JSON object per line) or plain
    text (e.g. stderr). Each line that parses as JSON is only scanned when its
    top-level "type" is a CLI-status type (`_STATUS_LINE_TYPES`); "user"/
    "assistant" lines are skipped since their content is arbitrary
    (potentially containing the literal phrase without meaning a real limit
    was hit). Lines that are not JSON at all (plain stderr) are scanned as-is.

    Besides the phrase match, two structured signals are recognised (CLI
    ~2.1.x emits both, and neither carries a machine-readable epoch in its
    human text): a `rate_limit_event` line whose `rate_limit_info.status` is
    "rejected" (its `resetsAt` is the authoritative reset epoch), and a
    terminal `result` line with `api_error_status` 429.
    Returns the same shape as `parse_usage_limit`, or `None`.
    """
    if not tail:
        return None
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[0] in "{[":
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "rate_limit_event":
                info = obj.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    reset_at = info.get("resetsAt")
                    return {"resetAt": int(reset_at) if isinstance(reset_at, (int, float)) else None}
                continue
            if obj.get("type") not in _STATUS_LINE_TYPES:
                continue
            limit = parse_usage_limit(json.dumps(obj))
            if limit is None and obj.get("type") == "result" and obj.get("api_error_status") == 429:
                limit = {"resetAt": None}
        else:
            limit = parse_usage_limit(line)
        if limit is not None:
            return limit
    return None


# Login-error phrases the CLI prints when the agent process has no valid auth.
# Both strings are matched case-insensitively anywhere in the log/output so a
# stream-json blob with the phrase inside a result field is also caught.
_LOGIN_ERROR_RE = re.compile(r"not logged in|please run /login", re.IGNORECASE)


def parse_login_error(text):
    """Detect a Claude login error in an agent's log/CLI output.

    Returns True when the text contains "Not logged in" or "Please run /login"
    (case-insensitive). Returns False for empty, None, or unrelated text.
    """
    if not text:
        return False
    return bool(_LOGIN_ERROR_RE.search(text))


def login_error_from_transcript_tail(tail):
    """Detect a genuine login error in a raw log tail, ignoring the phrases
    when they only appear inside echoed tool/file content.

    Same line filtering as `usage_limit_from_transcript_tail`: a JSON line is
    only scanned when its top-level "type" is a CLI-status type
    (`_STATUS_LINE_TYPES`); "user"/"assistant" content blocks are skipped since
    they echo arbitrary file/tool content — e.g. this module's own
    `_LOGIN_ERROR_RE` source, the ticket #104 false positive. Non-JSON lines
    (plain stderr) are scanned as-is. Returns True/False.
    """
    if not tail:
        return False
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[0] in "{[":
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict) or obj.get("type") not in _STATUS_LINE_TYPES:
                continue
            if parse_login_error(json.dumps(obj)):
                return True
        elif parse_login_error(line):
            return True
    return False


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


def resolve_model(model, *, fable_available):
    """Resolve a requested model, substituting the fable fallback when needed.

    When `model` is FABLE_MODEL and `fable_available` is False, returns
    FABLE_FALLBACK_MODEL (opus). All other models pass through unchanged.
    `fable_available` is injected by the caller (orchestrator.py probes the CLI)
    so this function stays pure and unit-testable.
    """
    if model == FABLE_MODEL and not fable_available:
        return FABLE_FALLBACK_MODEL
    return model


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


def resume_session_id(task):
    """The prior Claude session to RESUME when re-dispatching this ticket, or None.

    Ticket #13: a blocked ticket that is being unblocked (its question has been
    answered) should continue its EXISTING context via `claude --resume <id>`
    rather than restart with a fresh session that has forgotten everything it
    learned before it blocked. That only applies when the ticket already recorded
    a `claudeSessionId` from an earlier run — a first-ever dispatch (no prior
    session) has nothing to resume and must start fresh.
    """
    if not _has_answered_question(task):
        return None
    return task.get("claudeSessionId") or None


# Statuses that count as "finished". The board/server treats `done` and
# `completed` as the same column, so eligibility and dependency-satisfaction
# must accept either spelling.
_DONE_STATES = ("completed", "done")


def is_done(task):
    """True if the ticket has reached a terminal/finished status.

    A finished ticket is terminal: the reap loop must NOT re-reap it even if a
    stale `dispatched` marker reappears (a re-dispatch/adoption write race can
    re-add the marker after `clear_marker`). Without this guard the adopted-agent
    path re-classifies it as `completed` on every tick and re-runs the publish
    side-effects (ticket #48).
    """
    return task.get("status") in _DONE_STATES


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

    Ready tickets are returned sorted by their `order` field ascending (lower =
    higher priority) so backfill_dispatch picks the top of the Ready list first.
    Tickets without an `order` field sort after those that have one.
    """
    ready = []
    other = []
    for t in tasks:
        if is_in_flight(t):
            continue
        # A blocked ticket with an answered question re-dispatches.
        if t.get("status") == "blocked" and _has_answered_question(t):
            other.append(t)
            continue
        if t.get("status") == "ready":
            ready.append(t)
    ready.sort(key=lambda t: (t["order"] if t.get("order") is not None else float("inf")))
    return ready + other


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
