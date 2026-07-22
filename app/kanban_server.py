#!/usr/bin/env python3
"""Kanban board server — serves task boards from .kanban/ as a kanban API.

A *board* is a subdirectory of .kanban/ that contains a `_meta.json` file.
The directory name is the board's slug (its identifier in the API). Inside:

    <slug>/
      _meta.json   board-level info: project, updated, context, openQuestions, outOfScope
      <id>.json     one ticket per file (the task object)
      ...

Only subdirectories containing a `_meta.json` are treated as boards, so
helper directories (e.g. `__pycache__`) are ignored automatically.
"""

import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.error import URLError
from urllib.parse import urlparse, unquote, parse_qs
from urllib.request import Request, urlopen

import orchestrator_core as _oc
import perf_monitor


def _atomic_write_json(path, data):
    """Write *data* to *path* atomically via a sibling .tmp file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        # Never leave a partial sibling behind for a reader to trip over.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# --- Config ---
PORT = 8745
# Bind to loopback by default so the board is not reachable from other LAN hosts
# (the API exposes destructive endpoints: perf_kill, server/restart, DELETE task).
# Override with KANBAN_HOST=0.0.0.0 to expose it deliberately on a trusted network.
DEFAULT_HOST = "127.0.0.1"
HOST = os.environ.get("KANBAN_HOST", DEFAULT_HOST)

# Local auth token guarding state-changing endpoints. A request that carries a
# foreign browser Origin must present this token (header `X-Kanban-Token`) — this
# stops any website the user visits from driving mutations cross-origin. Set
# KANBAN_TOKEN to pin a value (e.g. for external CLI tools); otherwise a random
# per-process token is minted and printed on boot. The same-origin UI is served
# the token directly (see _serve_html), so it never needs configuring by hand.
AUTH_TOKEN = os.environ.get("KANBAN_TOKEN") or secrets.token_urlsafe(32)

# Origins allowed to read API responses cross-origin: only loopback (the UI is
# served same-origin from this server). Any other Origin is refused a CORS grant.
_ALLOWED_ORIGIN_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", re.IGNORECASE
)


def allowed_origin(origin):
    """Return *origin* if it is a permitted (loopback) browser origin, else None."""
    if origin and _ALLOWED_ORIGIN_RE.match(origin):
        return origin
    return None


# This module lives in .kanban/app/, so the board root is its parent dir.
KANBAN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(KANBAN_DIR, "static")
HTML_PATH = os.path.join(STATIC_DIR, "kanban.html")
CSS_PATH = os.path.join(STATIC_DIR, "kanban.css")
JS_PATH = os.path.join(STATIC_DIR, "kanban.js")
META_FILE = "_meta.json"
# Board directories live under a dedicated `boards/` folder (ticket #94), which
# is gitignored — keeping the .kanban root clean of loose board dirs mixed in
# with source. Board discovery and per-board path resolution route through
# boards_root() so the location is defined once and honors a monkeypatched
# KANBAN_DIR (it re-derives from KANBAN_DIR at call time rather than being a
# frozen module constant).
BOARDS_SUBDIR = "boards"


def boards_root():
    """Absolute path to the dedicated folder that holds every board dir."""
    return os.path.join(KANBAN_DIR, BOARDS_SUBDIR)


def _scandir_boards():
    """Scan the boards folder, yielding its entries (empty if it doesn't exist).

    A fresh tree may not have created `boards/` yet, so a missing folder is not
    an error — it just means there are no boards.
    """
    try:
        return list(os.scandir(boards_root()))
    except FileNotFoundError:
        return []


# Specs / plans live as markdown under .kanban/docs/. A doc associates itself
# with a ticket via a `**Ticket:** `.kanban/<board>/<id>.json`` line in its header
# (the convention used by the brainstorming/writing-plans skills).
DOCS_DIR = os.path.join(KANBAN_DIR, "docs")
# Per-run sub-agent stdout logs (stream-json) the orchestrator writes when it
# dispatches a ticket. The live-logs endpoint reads from here and never escapes it.
WORKSPACE_ROOT = os.path.dirname(KANBAN_DIR)
RUNS_DIR = os.path.join(KANBAN_DIR, "_orchestrator", "runs")

# Optional persisted bind config, so host/port can be set without env vars or
# argv (e.g. when launched by the orchestrator/UI). Read in main() with safe
# defaults; argv and env still take precedence (see main()).
SERVER_CONFIG_PATH = os.path.join(KANBAN_DIR, "_orchestrator", "server.json")


def load_server_config(path=None):
    """Read host/port overrides from `_orchestrator/server.json`.

    Returns a dict with `host` and `port`. A missing/unreadable/malformed file,
    or an individually missing or invalid key, falls back to the safe defaults
    (loopback host, default PORT) per-field so a bad config can never wedge the
    server onto a non-loopback bind or an unusable port.
    """
    path = path or SERVER_CONFIG_PATH
    host = DEFAULT_HOST
    port = PORT
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"host": host, "port": port}
    if isinstance(data, dict):
        h = data.get("host")
        if isinstance(h, str) and h.strip():
            host = h.strip()
        try:
            p = data.get("port")
            if p is not None:
                port = int(p)
        except (TypeError, ValueError):
            pass
    return {"host": host, "port": port}

# Self-describing guide stamped onto every ticket. Lets an LLM handed a single
# ticket file path locate the board docs and learn how to use the board. The
# string is identical on every ticket; it points at the source-of-truth docs
# rather than duplicating their prose. The backfill script imports this constant.
KANBAN_GUIDE = (
    "This is a .kanban board ticket (file-based kanban). To learn how to use this "
    "board, read .kanban/CLAUDE.md (the agent guide: status values, history/session "
    "conventions, server API) and this ticket's sibling _meta.json (project context). "
    "Tickets are JSON; agents read and edit them in place. "
    "Reusable skills for board work live in .kanban/skills/<skill-name>/SKILL.md "
    "(e.g. create-promotion-prs for raising acme-sfdx promotion PRs from the CLI); "
    "read the relevant SKILL.md before doing a task it covers."
)

# --- Status column mapping ---
STATUS_MAP = {
    "todo":        ("todo",        "Todo",        "#6b7280"),
    "ready":        ("ready",       "Ready",       "#8b5cf6"),
    "in_progress": ("in_progress", "In Progress", "#f59e0b"),
    "in-progress": ("in_progress", "In Progress", "#f59e0b"),
    "blocked":     ("blocked",     "Blocked",     "#ef4444"),
    "completed":   ("done",        "Done",        "#22c55e"),
    "done":        ("done",        "Done",        "#22c55e"),
}

# --- Model picklist ---
# Valid values for a ticket's top-level "model" field, set from the ticket UI
# and honored by the orchestrator at dispatch (ticket model > triage > profile).
# Served to the UI at runtime via GET /api/models (see MODEL_LIST) instead of
# being duplicated by hand in kanban.js. An empty string clears the override.
DEFAULT_MODELS = [
    {"value": "claude-haiku-4-5-20251001", "label": "Haiku (small)"},
    {"value": "claude-sonnet-4-6", "label": "Sonnet (medium)"},
    {"value": "claude-opus-4-8", "label": "Opus (large)"},
]

MODELS_API_URL = "https://api.anthropic.com/v1/models"
MODELS_API_VERSION = "2023-06-01"
MODELS_API_TIMEOUT = 3  # seconds; startup must never hang on a slow/dead network


def discover_models(api_key=None, url=None, timeout=MODELS_API_TIMEOUT, opener=None):
    """Fetch the live model catalog from the Anthropic API for the ticket model
    picklist, so it reflects what's actually available rather than a hand-
    maintained constant. Requires an API key (ANTHROPIC_API_KEY by default);
    with no key, or on any network/parse failure, falls back to DEFAULT_MODELS
    so server startup never blocks or fails on network availability. Only
    "claude-*" ids are kept (the endpoint may list non-Claude entries),
    sorted by id for a stable picklist order.
    """
    if api_key is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return list(DEFAULT_MODELS)

    req = Request(url or MODELS_API_URL, headers={
        "x-api-key": api_key,
        "anthropic-version": MODELS_API_VERSION,
    })
    opener = opener or urlopen
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (URLError, OSError, ValueError, TimeoutError):
        return list(DEFAULT_MODELS)

    raw = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return list(DEFAULT_MODELS)

    models = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        model_id = m.get("id")
        if not isinstance(model_id, str) or not model_id.startswith("claude-"):
            continue
        models.append({"value": model_id, "label": m.get("display_name") or model_id})
    if not models:
        return list(DEFAULT_MODELS)

    models.sort(key=lambda m: m["value"])
    return models


# Current model picklist: DEFAULT_MODELS until refresh_models() runs (server
# startup) or a test overrides it. MODEL_VALUES is the fast-lookup companion
# used to validate a ticket's "model" field.
MODEL_LIST = list(DEFAULT_MODELS)
MODEL_VALUES = {m["value"] for m in MODEL_LIST}


def refresh_models():
    """Re-discover the model picklist and update the module-level globals.
    Called once at server startup (see main()) so MODEL_LIST/MODEL_VALUES
    reflect what's available for this run without needing a restart-free
    live-reload path."""
    global MODEL_LIST, MODEL_VALUES
    MODEL_LIST = discover_models()
    MODEL_VALUES = {m["value"] for m in MODEL_LIST}
    return MODEL_LIST


def models_list():
    return {"models": MODEL_LIST}, 200

# Canonical status value written back to JSON for each column key
COLUMN_STATUS = {
    "todo":        "todo",
    "ready":        "ready",
    "in_progress": "in_progress",
    "blocked":     "blocked",
    "done":        "completed",
}

COLUMNS = [
    {"key": "todo",        "label": "Todo",        "color": "#6b7280"},
    {"key": "ready",       "label": "Ready",       "color": "#8b5cf6"},
    {"key": "in_progress", "label": "In Progress", "color": "#f59e0b"},
    {"key": "blocked",     "label": "Blocked",     "color": "#ef4444"},
    {"key": "done",        "label": "Done",        "color": "#22c55e"},
]


def get_task_column(status):
    if not status:
        return "todo"
    key = status.strip().lower().replace(" ", "_").replace("-", "_")
    mapped = STATUS_MAP.get(key)
    return mapped[0] if mapped else "todo"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Path safety -----------------------------------------------------------

def safe_segment(name):
    """Return name only if it is a single, safe path segment, else None."""
    if not name:
        return None
    if name != os.path.basename(name):
        return None
    if name in (".", "..") or os.sep in name or (os.altsep and os.altsep in name):
        return None
    return name


def board_dir(slug):
    """Resolve a board slug to its directory path, or (None, slug) if unsafe."""
    safe = safe_segment(slug)
    if safe is None:
        return None, slug
    return os.path.join(boards_root(), safe), safe


def ticket_path(board_path, task_id):
    """Resolve a task id to its ticket file path, or None if unsafe."""
    name = safe_segment(f"{task_id}.json")
    if name is None:
        return None
    return os.path.join(board_path, name)


def is_board(path):
    return os.path.isdir(path) and os.path.isfile(os.path.join(path, META_FILE))


def list_ticket_files(board_path):
    out = []
    for entry in os.scandir(board_path):
        if entry.is_file() and entry.name.endswith(".json") and entry.name != META_FILE:
            out.append(entry.path)
    return out


def task_sort_key(task):
    raw = task.get("id", "")
    try:
        return (0, int(raw))
    except (ValueError, TypeError):
        return (1, str(raw))


def board_mtime(board_path):
    """Newest mtime across _meta.json and every ticket file."""
    latest = 0.0
    for entry in os.scandir(board_path):
        if entry.is_file() and entry.name.endswith(".json"):
            latest = max(latest, entry.stat().st_mtime)
    return latest


def docs_mtime():
    """Newest mtime across markdown under .kanban/docs/ (0.0 when absent).

    Folded into every board payload's `mtime` so a spec/plan edit invalidates
    the `?since=` short-circuit and triggers a client re-render. Re-derives
    the docs path from KANBAN_DIR at call time (KANBAN_DIR is monkeypatched
    in tests; the module-level DOCS_DIR constant would go stale).
    """
    latest = 0.0
    for root, _dirs, files in os.walk(os.path.join(KANBAN_DIR, "docs")):
        for name in files:
            if name.lower().endswith(".md"):
                try:
                    latest = max(latest, os.stat(os.path.join(root, name)).st_mtime)
                except OSError:
                    continue
    return latest


def touch_meta(board_path):
    """Bump _meta.json's `updated` field to today's date."""
    meta_path = os.path.join(board_path, META_FILE)
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    meta["updated"] = date.today().isoformat()
    try:
        _atomic_write_json(meta_path, meta)
    except OSError:
        pass


def write_ticket(path, task):
    _atomic_write_json(path, task)


# --- Spec / plan discovery --------------------------------------------------
#
# A spec or plan is a markdown file under .kanban/docs/ that links itself to a
# ticket. The link is a header line of the form
#     **Ticket:** `.kanban/<board>/<id>.json`
# (the convention written by the brainstorming / writing-plans skills). We scan
# docs/ once per board load, build a {board/id -> [docs]} index, and attach the
# matching docs to each ticket as `_specs`. A ticket may also opt in explicitly
# via a `spec` / `specs` field holding doc path(s) relative to .kanban/.

_TICKET_REF_RE = re.compile(
    r"\*\*Ticket:\*\*\s*`?\.kanban[\\/]([^\s`/\\]+)[\\/](\d+)\.json`?", re.IGNORECASE
)
# H1 markdown title, used as the doc's display label when present.
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$")


def _doc_meta(abs_path):
    """Read a markdown doc's header: its (board, id) ticket ref and H1 title.

    Returns (board, task_id, title) — any of which may be None. Only the first
    ~40 lines are inspected so this stays cheap across many docs.
    """
    board = task_id = title = None
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 40:
                    break
                if title is None:
                    m = _H1_RE.match(line)
                    if m:
                        title = m.group(1).strip()
                if board is None:
                    m = _TICKET_REF_RE.search(line)
                    if m:
                        board, task_id = m.group(1), m.group(2)
    except OSError:
        return None, None, None
    return board, task_id, title


def _doc_kind(rel_path):
    """Classify a doc by its docs/ subfolder: 'spec', 'plan', or 'doc'."""
    parts = rel_path.replace("\\", "/").split("/")
    if "specs" in parts:
        return "spec"
    if "plans" in parts:
        return "plan"
    return "doc"


def _doc_entry(abs_path, title=None):
    """Build the front-end doc descriptor for a doc at *abs_path*."""
    rel = os.path.relpath(abs_path, KANBAN_DIR).replace("\\", "/")
    name = title or os.path.splitext(os.path.basename(abs_path))[0]
    return {"title": name, "path": rel, "kind": _doc_kind(rel)}


def build_spec_index():
    """Map 'board/id' -> [doc descriptor, ...] by scanning .kanban/docs/.

    Returns {} (and never raises) when docs/ is absent. Cheap enough to rebuild
    on each board poll; the doc tree is small and reads only file headers.
    """
    index = {}
    if not os.path.isdir(DOCS_DIR):
        return index
    for root, _dirs, files in os.walk(DOCS_DIR):
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            abs_path = os.path.join(root, fn)
            board, task_id, title = _doc_meta(abs_path)
            if not board or not task_id:
                continue
            index.setdefault(f"{board}/{task_id}", []).append(_doc_entry(abs_path, title))
    return index


def attach_specs(task, slug, spec_index):
    """Attach a `_specs` list to *task* (specs auto-discovered + explicit refs).

    `slug` is the task's board. Explicit references come from a `spec`/`specs`
    field holding a doc path (string or list) relative to .kanban/. Discovered
    and explicit docs are merged, de-duplicated by path.
    """
    found = list(spec_index.get(f"{slug}/{task.get('id')}", []))
    seen = {d["path"] for d in found}

    explicit = task.get("specs") or task.get("spec")
    if explicit:
        refs = explicit if isinstance(explicit, list) else [explicit]
        for ref in refs:
            if not isinstance(ref, str) or not ref.strip():
                continue
            rel = ref.strip()
            for prefix in (".kanban/", ".kanban\\"):
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                    break
            rel = rel.replace("\\", "/").lstrip("/")
            abs_path = os.path.normpath(os.path.join(KANBAN_DIR, rel))
            # Confine explicit refs to the .kanban/ tree.
            if os.path.commonpath([abs_path, KANBAN_DIR]) != KANBAN_DIR:
                continue
            norm_rel = os.path.relpath(abs_path, KANBAN_DIR).replace("\\", "/")
            if norm_rel in seen:
                continue
            _, _, title = _doc_meta(abs_path) if os.path.isfile(abs_path) else (None, None, None)
            found.append(_doc_entry(abs_path, title))
            seen.add(norm_rel)

    if found:
        task["_specs"] = found


def read_doc(rel_path):
    """Return the raw text of a doc under .kanban/docs/, or (None, status).

    Path is confined to the docs/ tree; anything escaping it is rejected.
    """
    rel = unquote(rel_path or "").replace("\\", "/").lstrip("/")
    abs_path = os.path.normpath(os.path.join(KANBAN_DIR, rel))
    # Must resolve inside docs/ and be a real file.
    if os.path.commonpath([abs_path, DOCS_DIR]) != DOCS_DIR:
        return None, 403
    if not os.path.isfile(abs_path):
        return None, 404
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read(), 200
    except OSError:
        return None, 500


# --- Board operations ------------------------------------------------------

def scan_boards():
    boards = []
    for entry in sorted(_scandir_boards(), key=lambda e: e.name):
        if not entry.is_dir() or not is_board(entry.path):
            continue
        slug = entry.name
        try:
            with open(os.path.join(entry.path, META_FILE), "r", encoding="utf-8") as f:
                meta = json.load(f)
            project = meta.get("project", slug)
        except (json.JSONDecodeError, OSError):
            project = slug
        task_count = len(list_ticket_files(entry.path))
        # `filename` carries the slug so existing front-end code keeps working.
        boards.append({"filename": slug, "project": project, "taskCount": task_count})
    return boards


ALL_SLUG = "__all__"


def load_all_boards():
    """Aggregate tasks from every board into a single virtual board."""
    tasks = []
    latest_mtime = 0.0
    spec_index = build_spec_index()
    for entry in sorted(_scandir_boards(), key=lambda e: e.name):
        if not entry.is_dir() or not is_board(entry.path):
            continue
        slug = entry.name
        try:
            with open(os.path.join(entry.path, META_FILE), "r", encoding="utf-8") as f:
                meta = json.load(f)
            project = meta.get("project", slug)
        except (json.JSONDecodeError, OSError):
            project = slug

        for tp in list_ticket_files(entry.path):
            try:
                with open(tp, "r", encoding="utf-8") as f:
                    task = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            task["_column"] = get_task_column(task.get("status", ""))
            task["_board"] = slug
            task["_project"] = project
            task["_filePath"] = os.path.abspath(tp).replace("\\", "/")
            attach_specs(task, slug, spec_index)
            tasks.append(task)

        latest_mtime = max(latest_mtime, board_mtime(entry.path))

    tasks.sort(key=task_sort_key)

    return {
        "project": "All Boards",
        "updated": "",
        "filename": ALL_SLUG,
        "tasks": tasks,
        # Same formula as board_snapshot_mtime(ALL_SLUG) — the two must stay
        # in lockstep or the ?since= short-circuit never (or always) fires.
        "mtime": max(latest_mtime, docs_mtime()),
        "columns": COLUMNS,
    }, 200


def load_board(slug):
    if slug == ALL_SLUG:
        return load_all_boards()

    path, safe = board_dir(slug)
    if path is None or not is_board(path):
        return None, 404

    try:
        with open(os.path.join(path, META_FILE), "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e)}, 500

    tasks = []
    spec_index = build_spec_index()
    for tp in list_ticket_files(path):
        try:
            with open(tp, "r", encoding="utf-8") as f:
                task = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        task["_column"] = get_task_column(task.get("status", ""))
        task["_board"] = safe
        task["_filePath"] = os.path.abspath(tp).replace("\\", "/")
        attach_specs(task, safe, spec_index)
        tasks.append(task)
    tasks.sort(key=task_sort_key)

    result = {
        "project": meta.get("project", safe),
        "updated": meta.get("updated", ""),
        "filename": safe,
        "tasks": tasks,
        # Same formula as board_snapshot_mtime(slug) — the two must stay in
        # lockstep or the ?since= short-circuit never (or always) fires.
        "mtime": max(board_mtime(path), docs_mtime()),
        "columns": COLUMNS,
    }
    # Pass through optional board-level metadata if present.
    for key in ("context", "openQuestions", "outOfScope", "commitRequirements",
                "directory", "useWorktrees", "useDocker", "envVars",
                "passthroughEnv"):
        if key in meta:
            result[key] = meta[key]
    # Surface the one-paragraph context blurb as a flat field for the settings
    # UI (the rest of `context` may hold arbitrary structured keys).
    if isinstance(meta.get("context"), dict) and meta["context"].get("description"):
        result["description"] = meta["context"]["description"]
    return result, 200


def board_snapshot_mtime(slug):
    """Stat-only recomputation of the payload `mtime` load_board would return.

    Used by board_get's ?since= short-circuit: file stats only — no ticket
    JSON opens, no spec-index rebuild — so idle polls stay invisible to
    on-access file scanning. Must stay in lockstep with load_board's payload
    mtime (same formula), else the short-circuit never (or always) fires.
    Returns None for an unknown board so the caller falls through to the full
    load (which 404s as before).
    """
    if slug == ALL_SLUG:
        latest = 0.0
        for entry in _scandir_boards():
            if entry.is_dir() and is_board(entry.path):
                latest = max(latest, board_mtime(entry.path))
        return max(latest, docs_mtime())
    path, _safe = board_dir(slug)
    if path is None or not is_board(path):
        return None
    return max(board_mtime(path), docs_mtime())


def board_get(slug, since=None):
    """GET /api/board/<slug>[?since=<mtime>] — full payload, or a cheap
    {"unchanged": true} answer when nothing changed since `since`.

    Polling clients echo back the `mtime` of the last payload they rendered;
    when the stat-only snapshot still matches, the server skips the full load
    entirely. A malformed `since`, an unknown board, or any mtime drift falls
    through to load_board (unknown slugs keep their 404).
    """
    if since is not None:
        try:
            since_f = float(since)
        except (TypeError, ValueError):
            since_f = None
        if since_f is not None:
            snap = board_snapshot_mtime(slug)
            if snap is not None and snap == since_f:
                return {"unchanged": True, "mtime": snap}, 200
    return load_board(slug)


# Board-level metadata fields the UI is allowed to edit. `commitRequirements`
# is a free-text, natural-language statement of what must hold before an agent
# commits/completes work (e.g. "all tests must pass") — agents read it from
# _meta.json. `directory` is the project's working directory on disk.
# `useWorktrees` is the per-project boolean (ticket #40) gating whether tickets
# are worked in a git worktree or in place on a branch. `useDocker` +
# `envVars` (ticket #16) gate/configure running the agent inside a per-repo
# Docker container. `passthroughEnv` (ticket #7) lists secret env-var NAMES the
# orchestrator forwards into the container by name only (values stay in the
# orchestrator env, never on disk). The flat `description` field is handled
# specially (merged into `context.description`); `envVars` and `passthroughEnv`
# are sanitized specially (a dict / a list of names, not a scalar) just below.
EDITABLE_META_FIELDS = ("project", "context", "openQuestions", "outOfScope",
                        "commitRequirements", "directory", "useWorktrees",
                        "useDocker", "envVars", "passthroughEnv")


def update_board_meta(slug, payload):
    """Merge editable board-meta fields from *payload* into the board's _meta.json.

    Only keys in EDITABLE_META_FIELDS (plus the flat `description` alias) are
    applied; everything else in the file is preserved. A field set to an empty
    string is removed (lets the UI clear a value). The `updated` date is bumped
    on success.
    """
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404

    meta_path = os.path.join(path, META_FILE)
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e)}, 500

    payload = payload or {}
    for key in EDITABLE_META_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        # `envVars` is a {KEY: VALUE} map (ticket #16), not a scalar: sanitize it
        # through the same core logic the orchestrator uses (drop invalid keys /
        # non-scalar values) and remove the field entirely when nothing survives.
        if key == "envVars":
            clean = _oc.board_env_vars({"envVars": value})
            if clean:
                meta[key] = clean
            else:
                meta.pop(key, None)
            continue
        # `passthroughEnv` is a list of env-var NAMES (ticket #7): secret names
        # forwarded to the container by name only. The Project Settings textarea
        # submits one name per line, so accept either a JSON list or a
        # newline/comma/space-separated string; sanitize through the core (drop
        # invalid names, de-dupe) and remove the field when nothing survives.
        if key == "passthroughEnv":
            if isinstance(value, str):
                value = [n for n in re.split(r"[\s,]+", value.strip()) if n]
            clean = _oc.board_passthrough_env({"passthroughEnv": value})
            if clean:
                meta[key] = clean
            else:
                meta.pop(key, None)
            continue
        if isinstance(value, str):
            value = value.strip()
        if value == "" or value is None:
            meta.pop(key, None)
        else:
            meta[key] = value

    # When Docker is explicitly turned off, container-only fields are meaningless
    # — remove them so no stale config sits around on a non-Docker board (ticket #87).
    if "useDocker" in payload and not _oc.use_docker(meta):
        meta.pop("envVars", None)
        meta.pop("passthroughEnv", None)

    # `description` is a flat alias for context.description — merge it into the
    # existing `context` object rather than overwriting its other structured keys.
    if "description" in payload:
        desc = payload["description"]
        if isinstance(desc, str):
            desc = desc.strip()
        ctx = meta.get("context")
        if not isinstance(ctx, dict):
            ctx = {}
        if desc:
            ctx["description"] = desc
        else:
            ctx.pop("description", None)
        if ctx:
            meta["context"] = ctx
        else:
            meta.pop("context", None)

    meta["updated"] = date.today().isoformat()
    try:
        _atomic_write_json(meta_path, meta)
    except OSError as e:
        return {"error": str(e)}, 500

    return {"ok": True, "meta": meta}, 200


def update_task_status(slug, task_id, new_column):
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404
    if new_column not in COLUMN_STATUS:
        return {"error": f"unknown column: {new_column}"}, 400

    tp = ticket_path(path, task_id)
    if tp is None or not os.path.isfile(tp):
        return {"error": f"task {task_id} not found"}, 404

    # Capture the timestamp BEFORE re-reading: tests (and real concurrent writers)
    # use the now_iso() call as a hook to inject a concurrent on-disk write. By
    # calling it first we guarantee the re-read that follows picks up those writes.
    ts = now_iso()

    # Re-read the ticket fresh immediately before writing and apply only the
    # fields we own (status + a history entry). Reading the whole object,
    # mutating, then writing it all back would clobber any comment/question/
    # commitGate another writer (a sub-agent) wrote in between. Mirrors the
    # re-read-before-write pattern in orch_kill/orch_answer. (Ticket #42.)
    try:
        with open(tp, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e)}, 500

    old_status = task.get("status", "todo")
    new_status = COLUMN_STATUS[new_column]

    # Ticket #100: prevent moving to "ready" without a real model specified.
    # An empty model field displays as "(default)" in the UI, which is confusing
    # and should not be allowed to dispatch.
    if new_status == "ready" and not (task.get("model") or "").strip():
        return {"error": "cannot move to ready: no model specified (model cannot be '(default)')"}, 400

    entry = {
        "action": "status_change",
        "from": old_status,
        "to": new_status,
        "timestamp": ts,
    }

    # UI-based session management (ticket #44):
    # Moving OUT of in_progress while a session is live → kill it first.
    if old_status == "in_progress" and new_status != "in_progress":
        _ui_kill_session(KANBAN_DIR, slug, task)

    # Ticket #97: moving OUT of blocked clears orchestrator.question so the
    # notification bell stops showing this ticket as needing human attention.
    # If the ticket re-blocks later, the agent writes a fresh question which
    # naturally re-triggers the bell.
    if old_status == "blocked" and new_status != "blocked":
        orch = task.get("orchestrator")
        if isinstance(orch, dict) and "question" in orch:
            del orch["question"]

    task["status"] = new_status
    task.setdefault("history", []).append(entry)

    try:
        write_ticket(tp, task)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    # Moving INTO in_progress and not already in-flight → spawn a session.
    if new_status == "in_progress" and old_status != "in_progress":
        if not _oc.is_in_flight(task):
            marker = _ui_dispatch_session(KANBAN_DIR, slug, task)
            if marker:
                # Re-read to pick up any write the spawn itself may have done,
                # then apply only the fields we own (marker + sessionId).
                try:
                    with open(tp, "r", encoding="utf-8") as f:
                        task = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass
                _oc.set_marker(task, marker)
                if marker.get("sessionId"):
                    task["claudeSessionId"] = marker["sessionId"]
                if marker.get("logFile"):
                    task["runLogFile"] = marker["logFile"]
                try:
                    write_ticket(tp, task)
                except OSError:
                    pass
                touch_meta(path)
                _oc.append_activity(KANBAN_DIR, {
                    "ts": now_iso(), "kind": "dispatch", "board": slug,
                    "ticket": task_id, "profile": marker.get("profile"),
                    "model": marker.get("model"), "reason": "ui-drag",
                })

    return {"ok": True, "taskId": task_id, "newStatus": new_status}, 200


def update_task_order(slug, task_id, order):
    """Set a ticket's top-level "order" field (integer priority within its column).
    Lower order = higher priority. Mirrors the re-read-before-write pattern."""
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404

    tp = ticket_path(path, task_id)
    if tp is None or not os.path.isfile(tp):
        return {"error": f"task {task_id} not found"}, 404

    try:
        with open(tp, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e)}, 500

    if order is None:
        task.pop("order", None)
    else:
        try:
            task["order"] = int(order)
        except (TypeError, ValueError):
            return {"error": "order must be an integer"}, 400

    try:
        write_ticket(tp, task)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    return {"ok": True, "taskId": task_id, "order": order}, 200


def update_task_model(slug, task_id, model):
    """Set (or clear) a ticket's top-level "model" override. Mirrors the
    re-read-before-write pattern in update_task_status so a concurrent sub-agent
    write (comment/question/commitGate) isn't clobbered. An empty/None model
    removes the override."""
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404
    if model and model not in MODEL_VALUES:
        return {"error": f"unknown model: {model}"}, 400

    tp = ticket_path(path, task_id)
    if tp is None or not os.path.isfile(tp):
        return {"error": f"task {task_id} not found"}, 404

    try:
        with open(tp, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e)}, 500

    if model:
        task["model"] = model
    else:
        task.pop("model", None)

    try:
        write_ticket(tp, task)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    return {"ok": True, "taskId": task_id, "model": model}, 200


def update_task_fields(slug, task_id, title, detail):
    """Update a ticket's title and/or detail text. Either may be None to leave unchanged.
    Follows the re-read-before-write pattern so concurrent writes aren't clobbered."""
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404

    tp = ticket_path(path, task_id)
    if tp is None or not os.path.isfile(tp):
        return {"error": f"task {task_id} not found"}, 404

    if title is not None and not title.strip():
        return {"error": "title cannot be empty"}, 400

    try:
        with open(tp, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e)}, 500

    if title is not None:
        task["title"] = title.strip()
    if detail is not None:
        if detail.strip():
            task["detail"] = detail.strip()
        else:
            task.pop("detail", None)

    try:
        write_ticket(tp, task)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    return {"ok": True, "taskId": task_id}, 200


def create_task(slug, payload):
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404

    title = (payload.get("title") or "").strip()
    if not title:
        return {"error": "title is required"}, 400

    # Auto-increment ID across existing ticket files.
    max_id = 0
    for tp in list_ticket_files(path):
        stem = os.path.splitext(os.path.basename(tp))[0]
        try:
            max_id = max(max_id, int(stem))
        except ValueError:
            pass
    new_id = str(max_id + 1)

    column = payload.get("column", "todo")
    if column not in COLUMN_STATUS:
        column = "todo"

    new_task = {"id": new_id, "title": title, "status": COLUMN_STATUS[column]}
    new_task["createdAt"] = now_iso()
    new_task["_kanbanGuide"] = KANBAN_GUIDE

    detail = (payload.get("detail") or "").strip()
    if detail:
        new_task["detail"] = detail

    depends = payload.get("dependsOn")
    if depends:
        if isinstance(depends, list):
            new_task["dependsOn"] = depends
        elif isinstance(depends, str) and depends.strip():
            new_task["dependsOn"] = [d.strip() for d in depends.split(",") if d.strip()]

    if payload.get("optional"):
        new_task["optional"] = True

    model = (payload.get("model") or "").strip()
    if model:
        new_task["model"] = model

    tp = ticket_path(path, new_id)
    if tp is None:
        return {"error": "could not build ticket path"}, 500
    try:
        write_ticket(tp, new_task)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    return {"ok": True, "task": new_task}, 201


def delete_task(slug, task_id):
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404

    tp = ticket_path(path, task_id)
    if tp is None or not os.path.isfile(tp):
        return {"error": f"task {task_id} not found"}, 404

    try:
        os.remove(tp)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    return {"ok": True, "deletedId": task_id}, 200


def add_comment(slug, task_id, payload):
    path, _ = board_dir(slug)
    if path is None or not is_board(path):
        return {"error": "board not found"}, 404

    writer = (payload.get("writer") or "").strip() or "Anonymous"
    message = (payload.get("message") or "").strip()
    if not message:
        return {"error": "message is required"}, 400

    tp = ticket_path(path, task_id)
    if tp is None or not os.path.isfile(tp):
        return {"error": f"task {task_id} not found"}, 404

    comment = {"writer": writer, "message": message, "timestamp": now_iso()}

    # Re-read the ticket fresh immediately before writing and only append our
    # comment, so a concurrent status/history/question write (the orchestrator
    # loop) or another comment is not clobbered by a stale whole-object write.
    # Mirrors orch_kill/orch_answer's re-read-before-write. (Ticket #42.)
    try:
        with open(tp, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return {"error": str(e)}, 500

    task.setdefault("comments", []).append(comment)

    try:
        write_ticket(tp, task)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    return {"ok": True, "comment": comment}, 201


# --- Task 5: Profiles CRUD --------------------------------------------------

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


# --- Task 6: Orchestrator state ---------------------------------------------

# Background orchestrator tick loop. The server starts it on boot and whenever
# the "Orchestrator: ON" button flips `enabled` true. A single-instance file
# lock (in _oc) guarantees only one loop runs even with several servers up, and
# this guard guarantees we never start a second thread within one process.
_orch_thread = None
_orch_stop = None


def ensure_orchestrator_running():
    """Idempotently start the orchestrator tick loop in a daemon thread.

    No-op if this process already has a live loop thread, or if another live
    process holds the single-instance lock. Safe to call repeatedly (on boot
    and on each enable toggle). The loop itself only *dispatches* when state's
    `enabled` is true, so running it continuously is harmless when toggled off —
    it still reaps in-flight agents.
    """
    global _orch_thread, _orch_stop
    if _orch_thread is not None and _orch_thread.is_alive():
        return False  # already running in this process
    if not _oc.acquire_lock(KANBAN_DIR):
        return False  # another process owns the orchestrator
    import orchestrator as _orch
    _orch_stop = threading.Event()
    _orch_thread = threading.Thread(
        target=_orch.run_loop,
        kwargs={"kanban_dir": KANBAN_DIR, "stop_event": _orch_stop},
        name="orchestrator-loop",
        daemon=True,
    )
    _orch_thread.start()
    print("Orchestrator tick loop started (single-instance lock acquired).")
    return True


def stop_orchestrator():
    """Signal the loop to stop and release the lock (used on server shutdown)."""
    global _orch_thread, _orch_stop
    if _orch_stop is not None:
        _orch_stop.set()
    _oc.release_lock(KANBAN_DIR)


def server_restart():
    """Restart this server process in place.

    We cannot both respond and terminate in one step, so we release the
    orchestrator lock now and re-exec on a short timer — after the 200 has been
    flushed to the client. os.execv replaces the process image (same PID); the
    in-flight `claude -p` agents are separate OS processes and are untouched,
    and the re-exec'd image re-acquires the lock and starts a fresh loop on boot.
    """
    stop_orchestrator()  # release single-instance lock for the new image

    def _reexec():
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Timer(0.3, _reexec).start()
    return {"ok": True}, 200


def orch_state_get():
    state = _oc.read_state(KANBAN_DIR)
    # Ticket #60: surface an active usage-limit pause so the live UI can show
    # "usage limited" and when it will resume. Omitted entirely when not paused,
    # keeping the default-state shape unchanged for callers that don't care.
    pause = _oc.read_usage_pause(KANBAN_DIR)
    if pause.get("pausedUntil"):
        remaining = _oc.usage_pause_remaining(KANBAN_DIR, time.time())
        state = dict(state)
        state["usagePause"] = {
            "pausedUntil": pause["pausedUntil"],
            "remainingSeconds": remaining,
            "reason": pause.get("reason", ""),
            "since": pause.get("since"),
        }
    return state, 200


def orch_state_put(payload):
    state = _oc.read_state(KANBAN_DIR)
    for k in ("enabled", "concurrencyCap", "stopAllRequested", "idleSeconds",
              "tickSeconds", "maxAgentSeconds", "triageTimeoutSeconds",
              "triageModel", "summarizerModel", "autoCommit", "autoPush"):
        if k in (payload or {}):
            state[k] = payload[k]
    _oc.write_state(KANBAN_DIR, state)
    # When the "on" button flips enabled true, make sure a loop is actually
    # running to act on it (it may not be if no server held the lock at boot).
    if state.get("enabled"):
        ensure_orchestrator_running()
    return state, 200


# --- Task 7: Activity, kill, answer -----------------------------------------

def orch_activity():
    return {"entries": _oc.read_activity(KANBAN_DIR)}, 200


def _ticket_file(board, task_id):
    bsafe = _oc.safe_name(board)
    isafe = _oc.safe_name(f"{task_id}.json")
    if bsafe is None or isafe is None:
        return None
    return os.path.join(boards_root(), bsafe, isafe)


# --- Live logs -------------------------------------------------------------

# How many bytes from the END of a run-log we read each poll. The log is
# stream-json (one JSON object per line) and can grow to hundreds of KB; we only
# render the last handful of turns, so reading the whole file every 2s is wasteful.
_LOG_TAIL_BYTES = 256 * 1024
# Re-export the preview cap and parsing helpers from orchestrator_core so existing
# tests that reference `ks._TOOL_RESULT_PREVIEW` and `ks.parse_log_turns` still work.
# (parse_log_turns moved to orchestrator_core in #58; it carries the #45 tool_use_id
# result-matching and the #57 per-turn timestamp.)
from orchestrator_core import (  # noqa: E402
    _TOOL_RESULT_PREVIEW,
    _tool_summary,
    _result_preview,
    parse_log_turns,
)


def _resolve_run_log(log_file):
    """Map a ticket's `orchestrator.logFile` to an absolute path INSIDE runs/.

    `logFile` is stored relative to the workspace root (parent of .kanban), e.g.
    `.kanban/_orchestrator/runs/45-….log`. Returns the absolute path only if it
    stays within RUNS_DIR; anything escaping it (traversal) returns None.
    """
    if not log_file:
        return None
    rel = str(log_file).replace("\\", "/").lstrip("/")
    abs_path = os.path.normpath(os.path.join(WORKSPACE_ROOT, rel))
    try:
        if os.path.commonpath([abs_path, RUNS_DIR]) != RUNS_DIR:
            return None
    except ValueError:
        return None  # different drives on Windows, etc.
    return abs_path


def task_log(board, task_id, n=20):
    """Return the recent agent turns for a ticket's current run-log.

    Shape: {turns, running, hasLog, status}. Never errors on a missing log — an
    absent/not-yet-dispatched ticket simply has hasLog=false and no turns.

    For completed tickets, falls back to the `completedLog` field saved on the
    ticket at completion time (ticket #58) when the live log file is absent or
    inaccessible.
    """
    path = _ticket_file(board, task_id)
    if path is None or not os.path.isfile(path):
        return {"error": "not found"}, 404
    try:
        with open(path, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (OSError, ValueError):
        return {"error": "could not read ticket"}, 500
    marker = task.get("orchestrator") or {}
    status = task.get("status", "")
    running = marker.get("state") == "dispatched" and status == "in_progress"

    # For a done ticket that has a saved completedLog, return it directly without
    # needing the (possibly deleted/archived) run-log file.
    if status in ("completed", "done") and isinstance(task.get("completedLog"), list):
        turns = task["completedLog"]
        if n and len(turns) > n:
            turns = turns[-n:]
        return {"turns": turns, "running": False, "hasLog": True, "status": status}, 200

    # Fall back to top-level runLogFile when the marker is absent (cleared after reap).
    log_path = _resolve_run_log(marker.get("logFile") or task.get("runLogFile"))
    if log_path is None:
        # No marker / no log yet, or a path escaping runs/ — show empty state.
        return {"turns": [], "running": running, "hasLog": False, "status": status}, 200
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            if size > _LOG_TAIL_BYTES:
                f.seek(size - _LOG_TAIL_BYTES)
                f.readline()  # drop the partial first line after the seek
            text = f.read()
    except OSError:
        return {"turns": [], "running": running, "hasLog": True, "status": status}, 200
    turns = parse_log_turns(text, n)
    return {"turns": turns, "running": running, "hasLog": True, "status": status}, 200


def orch_kill(board, task_id):
    path = _ticket_file(board, task_id)
    if path is None or not os.path.isfile(path):
        return {"error": "not found"}, 404
    with open(path, "r", encoding="utf-8") as f:
        task = json.load(f)
    marker = task.get("orchestrator") or {}
    pid = marker.get("pid")

    # Lazy import with graceful fallback: if orchestrator.py is absent or the
    # process is not alive, take the queued path (set killRequested flag).
    killed_directly = False
    try:
        import orchestrator as _orch
        if pid and _orch._process_alive(pid):
            _orch.kill_pid(pid)
            # Docker-mode agents (ticket #16) run inside a container; killing the
            # host client isn't enough, so tear the container down by name too.
            _orch._kill_container(marker.get("containerName"))
            killed_directly = True
    except ImportError:
        pass

    if not killed_directly:
        # RE-READ fresh copy immediately before writing to minimise clobber window.
        with open(path, "r", encoding="utf-8") as f:
            fresh = json.load(f)
        fresh.setdefault("orchestrator", {})["killRequested"] = True
        _atomic_write_json(path, fresh)
    # Direct kill: process is gone; the orchestrator loop will reap and update
    # the ticket on the next tick.  Do NOT rewrite the file here — we own no
    # fields at this point and touching the file risks clobbering sub-agent writes.

    return {"ok": True, "queued": not killed_directly}, 200


def orch_answer(board, task_id, payload):
    path = _ticket_file(board, task_id)
    if path is None or not os.path.isfile(path):
        return {"error": "not found"}, 404
    # Initial read: check that a question exists (400 guard).
    with open(path, "r", encoding="utf-8") as f:
        task = json.load(f)
    marker = task.get("orchestrator") or {}
    q = marker.get("question")
    if not q:
        return {"error": "no question"}, 400

    new_question = _oc.apply_answer(q, (payload or {}).get("value"),
                                    (payload or {}).get("notes", ""))

    # RE-READ immediately before writing to pick up any concurrent sub-agent
    # writes (e.g. new comments) that arrived between the initial read above
    # and now.  Apply only the narrow field we own.
    with open(path, "r", encoding="utf-8") as f:
        fresh = json.load(f)
    fresh.setdefault("orchestrator", {})["question"] = new_question
    _atomic_write_json(path, fresh)
    return {"ok": True}, 200


def orch_chat(board, task_id, payload):
    """Append a chat message to a RUNNING ticket's inbox (agent chat).

    Spec: docs/specs/2026-07-03-agent-chat-design.md, Component 2. Stores the
    raw {"message","writer","ts"} fields — the orchestrator pump does the
    writer-attribution wrapping when it relays to the agent's stdin. Does NOT
    check the PID is alive: that race belongs to the pump/reap side (a message
    posted just as the run dies is silently dropped with the inbox file).
    """
    path = _ticket_file(board, task_id)
    if path is None or not os.path.isfile(path):
        return {"error": "not found"}, 404
    msg = (payload or {}).get("message")
    if not isinstance(msg, str) or not msg.strip():
        return {"error": "message must be a non-empty string"}, 400
    try:
        with open(path, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (OSError, ValueError):
        return {"error": "could not read ticket"}, 500
    marker = task.get("orchestrator") or {}
    if not (marker.get("state") == "dispatched"
            and task.get("status") == "in_progress"):
        return {"error": "not running"}, 409
    if not _oc.CHAT_ENABLED:
        return {"error": "chat disabled"}, 409
    writer = str((payload or {}).get("writer") or "unknown")
    line = json.dumps({"message": msg, "writer": writer, "ts": _oc.now_iso()},
                      ensure_ascii=False) + "\n"
    inbox = _oc.chat_inbox_path(board, task_id)
    try:
        os.makedirs(os.path.dirname(inbox), exist_ok=True)
        # Single write of one whole line + flush: the pump tails by byte
        # offset and only consumes complete lines, so this append is atomic
        # enough — nothing partial is ever relayed.
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError:
        return {"error": "could not write inbox"}, 500
    return {"ok": True}, 200


# --- Performance monitor ----------------------------------------------------

# TTL cache for the _ticket_agent_pids disk scan. The perf sampler calls
# _owned_pids every ~3s; re-reading every board's ticket JSON that often burned
# ~15-20% of a core at idle (and each file open is also scanned by the
# endpoint-security filter driver, multiplying the cost in kernel time). The
# scan only exists to recover PIDs dispatched before a server restart — newly
# dispatched agents are tracked in-memory via _PROCS — so staleness up to the
# TTL is cosmetic (Performance-tab owned/external labeling only).
_TICKET_PIDS_TTL = 30.0
_ticket_pids_cache = {"at": None, "pids": set()}


def _ticket_pids_cache_clear():
    _ticket_pids_cache["at"] = None
    _ticket_pids_cache["pids"] = set()


def _ticket_agent_pids(now=None):
    """Scan all board ticket files for in_progress orchestrator PIDs.

    After a server reboot _PROCS is empty, so this recovers the PIDs that were
    dispatched before the restart.  Only in_progress / blocked tickets are
    included — completed tickets' PIDs may have been reused by the OS.

    The scan result is cached for _TICKET_PIDS_TTL seconds (see note above).
    """
    if now is None:
        now = time.monotonic()
    at = _ticket_pids_cache["at"]
    if at is not None and (now - at) < _TICKET_PIDS_TTL:
        return _ticket_pids_cache["pids"]
    pids = set()
    try:
        for entry in _scandir_boards():
            if not entry.is_dir() or not is_board(entry.path):
                continue
            for tfile in os.scandir(entry.path):
                if not tfile.is_file() or not tfile.name.endswith(".json") or tfile.name == META_FILE:
                    continue
                try:
                    with open(tfile.path, "r", encoding="utf-8") as f:
                        t = json.load(f)
                except Exception:
                    continue
                if t.get("status") not in ("in_progress", "blocked"):
                    continue
                orch = t.get("orchestrator") or {}
                pid = orch.get("pid")
                if isinstance(pid, int):
                    pids.add(pid)
    except Exception:
        pass
    _ticket_pids_cache["at"] = now
    _ticket_pids_cache["pids"] = pids
    return pids


def _owned_pids():
    """PIDs the orchestrator owns: ticket agents (_PROCS) + server background ops (_SERVER_OPS).

    Also recovers in_progress ticket PIDs from disk so sessions spawned before a
    server reboot are not shown as external in the Performance tab.
    """
    try:
        import orchestrator as _orch
        return set(_orch._PROCS.keys()) | set(_orch._SERVER_OPS.keys()) | _ticket_agent_pids()
    except Exception:
        return _ticket_agent_pids()


def _server_op_labels():
    """Label map for server-owned background processes (triage, summarize)."""
    try:
        import orchestrator as _orch
        return dict(_orch._SERVER_OPS)
    except Exception:
        return {}


_PERF_SAMPLER = perf_monitor.PerfSampler(owned_pids_fn=_owned_pids,
                                          labels_fn=_server_op_labels)


def ensure_perf_sampler_running():
    try:
        _PERF_SAMPLER.start()
    except Exception:
        pass


def perf_snapshot():
    return _PERF_SAMPLER.snapshot(), 200


def perf_kill(pid_str):
    try:
        pid = int(pid_str)
    except (TypeError, ValueError):
        return {"error": "bad pid"}, 400
    return perf_monitor.kill_session(pid), 200


# --- UI-based session management (ticket #44) --------------------------------
#
# Dragging a ticket to in_progress via the UI should behave exactly like the
# orchestrator picking it up: spawn a headless `claude -p` sub-agent, write the
# orchestrator marker + claudeSessionId, and log to the activity feed. Dragging
# it back out should kill the agent, write an Opus-generated progress summary,
# clear the marker, and log the kill.
#
# Both seams (_ui_summarize_progress, _ui_dispatch_session, _ui_kill_session)
# are exposed as module-level names so tests can monkeypatch them.


def _ui_summarize_progress(kanban_dir, task, reason):
    """Summarise an in-flight agent's progress before a UI-triggered kill.

    Delegates to the real orchestrator summarizer. Exposed as a module-level
    name so tests can monkeypatch it without touching orchestrator internals.
    """
    import orchestrator as _orch
    return _orch._summarize_progress(kanban_dir, task, reason)


def _ui_dispatch_session(kanban_dir, slug, task):
    """Spawn a sub-agent for a ticket moved to in_progress from the UI.

    Picks the best-fit profile (first profile with a whenToUse, else first
    profile — same heuristic as backfill_dispatch). No-ops silently when
    there are no profiles so a board with no profiles still allows manual drags.
    Returns the marker dict on success, or None when no profile is available.
    """
    import orchestrator as _orch
    profiles = _oc.list_profiles(kanban_dir)
    if not profiles:
        return None
    profile = next((p for p in profiles if p.get("whenToUse")), profiles[0])
    model = task.get("model") or profile.get("model")
    try:
        return _orch.spawn_agent(kanban_dir, slug, task, profile, model)
    except Exception as e:
        _oc.append_activity(kanban_dir, {
            "ts": now_iso(), "kind": "error", "board": slug,
            "ticket": task.get("id"), "message": f"ui spawn failed: {e}",
        })
        return None


def _ui_kill_session(kanban_dir, slug, task):
    """Kill the sub-agent for a ticket moved out of in_progress from the UI.

    Summarises progress first (so the kill leaves a resumable checkpoint),
    kills the process, writes the summary as a comment, clears the marker,
    and logs to the activity feed. No-ops when there is no dispatched marker.
    Mutates *task* in place (comments + orchestrator). Caller is responsible
    for writing the task to disk.
    """
    import orchestrator as _orch
    marker = _oc.get_marker(task)
    if not marker or marker.get("state") != "dispatched":
        return
    pid = marker.get("pid")
    summary = _ui_summarize_progress(kanban_dir, task, "kill")
    task.setdefault("comments", []).append({
        "writer": "Orchestrator",
        "message": f"Killed by UI drag.\n{summary}",
        "timestamp": now_iso(),
    })
    if pid:
        _orch.kill_pid(pid)
    _oc.clear_marker(task)
    _oc.append_activity(kanban_dir, {
        "ts": now_iso(), "kind": "kill", "board": slug,
        "ticket": task.get("id"), "reason": "ui-drag",
    })


# --- Nudge: immediate tick trigger ------------------------------------------

def _nudge_opus_triage(prompt, eligible, profiles, free):
    """Triage callable used by the nudge tick.

    Reads triageModel and triageTimeoutSeconds fresh so live config changes
    take effect, then delegates to the real Opus call. Exposed as a module-level
    name so tests can monkeypatch it without touching orchestrator internals.
    """
    import orchestrator as _orch
    state = _oc.read_state(KANBAN_DIR)
    model = state.get("triageModel") or _oc.DEFAULT_LOOP_MODEL
    timeout = state.get("triageTimeoutSeconds") or 120
    return _orch._real_opus_triage(prompt, eligible, profiles, free,
                                   model=model, timeout=timeout)


def _nudge_initial_triage(kanban_dir, task, all_tasks):
    """Initial-triage callable used by the nudge tick.

    Delegates to the real Sonnet triage. Exposed as a module-level name so
    tests can monkeypatch it without touching orchestrator internals.
    """
    import orchestrator as _orch
    return _orch._real_sonnet_triage(kanban_dir, task, all_tasks)


# Serialization state for nudge: at most one tick runs at a time; at most one
# follow-up tick waits in the backlog.
_nudge_lock = threading.Lock()
_nudge_running = False   # True while a tick thread is executing
_nudge_queued = False    # True when a follow-up tick is waiting


def orch_nudge_tick():
    """Execute one nudge tick (real implementation).

    Exposed as a module-level name so tests can monkeypatch it.
    """
    import orchestrator as _orch
    _orch.tick(KANBAN_DIR, opus_triage=_nudge_opus_triage,
               initial_triage=_nudge_initial_triage)


def orch_nudge():
    """Trigger an immediate orchestrator tick in a background daemon thread.

    Only one tick runs at a time.  If nudge is called while a tick is running,
    a single follow-up tick is queued (additional calls are no-ops — the cap is
    1 queued item).  The queued tick fires automatically when the current one
    finishes.

    The call always returns immediately so the HTTP response is not held open for
    the full tick duration (which can be tens of seconds when triage calls Opus).
    """
    global _nudge_running, _nudge_queued

    with _nudge_lock:
        if _nudge_running:
            # Cap backlog at 1.
            _nudge_queued = True
            return {"ok": True, "queued": True}, 200
        _nudge_running = True

    def _run():
        global _nudge_running, _nudge_queued
        while True:
            try:
                orch_nudge_tick()
            except Exception as e:
                _oc.append_activity(KANBAN_DIR, {
                    "ts": now_iso(), "kind": "error",
                    "message": f"nudge tick failed: {e}",
                })
            with _nudge_lock:
                if _nudge_queued:
                    _nudge_queued = False
                    # Loop around to run the queued tick.
                    continue
                _nudge_running = False
                break

    t = threading.Thread(target=_run, name="nudge-tick", daemon=True)
    t.start()
    return {"ok": True, "queued": True}, 200


class KanbanHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/files":
            self._json(scan_boards())
        # GET /api/board/<slug>/task/<id>/log[?n=] — recent agent turns (live logs)
        elif path.startswith("/api/board/") and path.endswith("/log"):
            parts = path.split("/")
            if len(parts) == 7 and parts[4] == "task":
                slug = unquote(parts[3])
                task_id = unquote(parts[5])
                qs = parse_qs(parsed.query)
                try:
                    n = max(1, min(100, int(qs.get("n", ["20"])[0])))
                except (ValueError, TypeError):
                    n = 20
                self._json(*task_log(slug, task_id, n))
            else:
                self.send_error(404)
        elif path.startswith("/api/board/"):
            slug = unquote(path[len("/api/board/"):])
            since = parse_qs(parsed.query).get("since", [None])[0]
            data, status = board_get(slug, since)
            self._json(data, status)
        elif path == "/api/models":
            self._json(*models_list())
        elif path == "/api/profiles":
            self._json(*profiles_list())
        elif path.startswith("/api/profiles/"):
            name = unquote(path[len("/api/profiles/"):])
            self._json(*profile_get(name))
        elif path == "/api/orchestrator/state":
            self._json(*orch_state_get())
        elif path == "/api/orchestrator/activity":
            self._json(*orch_activity())
        elif path == "/api/performance":
            self._json(*perf_snapshot())
        elif path.startswith("/api/doc/"):
            text, status = read_doc(path[len("/api/doc/"):])
            if text is None:
                self.send_error(status)
            else:
                self._text(text)
        elif path in ("/", "/index.html"):
            self._serve_html()
        elif path == "/kanban.css":
            self._serve_static(CSS_PATH, "text/css; charset=utf-8")
        elif path == "/kanban.js":
            self._serve_static(JS_PATH, "application/javascript; charset=utf-8")
        else:
            self.send_error(404)

    def _authorized(self):
        """Gate state-changing requests against cross-origin (CSRF) abuse.

        A valid `X-Kanban-Token` always authorizes (CLI tools, scripts). Failing
        that, the request must not be a *cross-origin browser* request: requests
        with no Origin (non-browser clients, reaching us only over loopback) and
        same-origin requests pass; a foreign Origin is refused. Returns True if
        the request may proceed, else emits a 403 and returns False.
        """
        token = self.headers.get("X-Kanban-Token", "")
        if token and AUTH_TOKEN and secrets.compare_digest(token, AUTH_TOKEN):
            return True
        origin = self.headers.get("Origin")
        if origin is None or allowed_origin(origin) is not None:
            return True
        self._json({"error": "forbidden: cross-origin request requires auth token"}, 403)
        return False

    def do_PUT(self):
        if not self._authorized():
            return
        parts = urlparse(self.path).path.rstrip("/").split("/")
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "profiles":
            name = unquote(parts[3])
            payload = self._read_json()
            if payload is None:
                return
            self._json(*profile_put(name, payload))
        elif len(parts) == 4 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "state":
            payload = self._read_json()
            if payload is None:
                return
            self._json(*orch_state_put(payload))
        # PUT /api/board/<slug>/meta — edit board-level metadata
        elif len(parts) == 5 and parts[1] == "api" and parts[2] == "board" and parts[4] == "meta":
            slug = unquote(parts[3])
            payload = self._read_json()
            if payload is None:
                return
            self._json(*update_board_meta(slug, payload))
        else:
            self.send_error(404)

    def do_PATCH(self):
        if not self._authorized():
            return
        # PATCH /api/board/<slug>/task/<id>
        #   body: {"column": "in_progress"}  -> move status
        #   body: {"model": "claude-opus-4-8"} -> set/clear model override
        parts = urlparse(self.path).path.rstrip("/").split("/")
        if len(parts) == 6 and parts[1] == "api" and parts[2] == "board" and parts[4] == "task":
            slug = unquote(parts[3])
            task_id = unquote(parts[5])
            payload = self._read_json()
            if payload is None:
                return
            if "model" in payload:
                result, status = update_task_model(slug, task_id, payload.get("model", ""))
            elif "order" in payload:
                result, status = update_task_order(slug, task_id, payload.get("order"))
            elif "title" in payload or "detail" in payload:
                result, status = update_task_fields(
                    slug, task_id,
                    payload.get("title"),
                    payload.get("detail"),
                )
            else:
                result, status = update_task_status(slug, task_id, payload.get("column", ""))
            self._json(result, status)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._authorized():
            return
        parts = urlparse(self.path).path.rstrip("/").split("/")

        # POST /api/board/<slug>/task — create task
        if len(parts) == 5 and parts[1] == "api" and parts[2] == "board" and parts[4] == "task":
            slug = unquote(parts[3])
            payload = self._read_json()
            if payload is None:
                return
            result, status = create_task(slug, payload)
            self._json(result, status)

        # POST /api/board/<slug>/task/<id>/comment — add comment
        elif len(parts) == 7 and parts[1] == "api" and parts[2] == "board" and parts[4] == "task" and parts[6] == "comment":
            slug = unquote(parts[3])
            task_id = unquote(parts[5])
            payload = self._read_json()
            if payload is None:
                return
            result, status = add_comment(slug, task_id, payload)
            self._json(result, status)

        # POST /api/orchestrator/kill/<board>/<id>
        elif len(parts) == 6 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "kill":
            self._json(*orch_kill(unquote(parts[4]), unquote(parts[5])))

        # POST /api/orchestrator/answer/<board>/<id>
        elif len(parts) == 6 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "answer":
            payload = self._read_json()
            if payload is None:
                return
            self._json(*orch_answer(unquote(parts[4]), unquote(parts[5]), payload))

        # POST /api/orchestrator/chat/<board>/<id> — message a running agent
        elif len(parts) == 6 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "chat":
            payload = self._read_json()
            if payload is None:
                return
            self._json(*orch_chat(unquote(parts[4]), unquote(parts[5]), payload))

        # POST /api/orchestrator/nudge — immediate tick
        elif len(parts) == 4 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "nudge":
            self._json(*orch_nudge())

        # POST /api/server/restart
        elif len(parts) == 4 and parts[1] == "api" and parts[2] == "server" and parts[3] == "restart":
            self._json(*server_restart())

        # POST /api/performance/kill/<pid>
        elif len(parts) == 5 and parts[1] == "api" and parts[2] == "performance" and parts[3] == "kill":
            self._json(*perf_kill(unquote(parts[4])))

        else:
            self.send_error(404)

    def do_DELETE(self):
        if not self._authorized():
            return
        parts = urlparse(self.path).path.rstrip("/").split("/")

        # DELETE /api/profiles/<name>
        if len(parts) == 4 and parts[1] == "api" and parts[2] == "profiles":
            self._json(*profile_delete(unquote(parts[3])))
            return

        # DELETE /api/board/<slug>/task/<id>
        if len(parts) == 6 and parts[1] == "api" and parts[2] == "board" and parts[4] == "task":
            slug = unquote(parts[3])
            task_id = unquote(parts[5])
            result, status = delete_task(slug, task_id)
            self._json(result, status)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _read_json(self):
        """Read and parse a JSON request body, or send 400 and return None."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, 400)
            return None

    def _cors_headers(self):
        # Grant CORS only to a permitted (loopback) Origin, echoed back — never a
        # wildcard. A foreign Origin gets no Allow-Origin header, so the browser
        # refuses to expose the response to the calling page.
        origin = allowed_origin(self.headers.get("Origin"))
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, PATCH, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Kanban-Token")

    def _safe_write(self, body):
        # The client (browser poll) may close the connection before we finish
        # writing — e.g. a tab refresh or a cancelled 1.5s poll. That surfaces as
        # ConnectionAbortedError/BrokenPipeError on Windows/Unix; it's harmless,
        # so swallow it instead of dumping a traceback.
        try:
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError):
            pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
        except (ConnectionError, BrokenPipeError):
            return
        self._safe_write(body)

    def _text(self, text, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self._cors_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
        except (ConnectionError, BrokenPipeError):
            return
        self._safe_write(body)

    def _serve_html(self):
        try:
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                html = f.read()
            # Hand the same-origin UI its auth token. A cross-origin page cannot
            # read this document body, so the token stays out of attackers' reach.
            inject = f'<script>window.KANBAN_TOKEN={json.dumps(AUTH_TOKEN)};</script>'
            if "</head>" in html:
                html = html.replace("</head>", inject + "</head>", 1)
            else:
                html = inject + html
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._safe_write(body)
        except FileNotFoundError:
            self.send_error(504, "kanban.html not found")

    def _serve_static(self, path, content_type):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._safe_write(body)
        except FileNotFoundError:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def main():
    # Kernel-level CPU cap (Windows Job Object hard cap) on the server process
    # only — the orchestrator tick loop and perf sampler are threads in this
    # process and share the cap; child processes (dispatched agents, git)
    # break away from the job at spawn and run uncapped. See cpu_limiter.py
    # for the KANBAN_CPU_LIMIT / cpuLimitPercent resolution rules.
    import cpu_limiter

    cpu_pct = cpu_limiter.resolve_limit_percent(SERVER_CONFIG_PATH)
    if cpu_limiter.apply_cpu_limit(cpu_pct):
        print(f"CPU hard-capped at {cpu_pct}% of total system CPU (kernel job object)")
    elif cpu_pct:
        print(f"WARNING: could not apply {cpu_pct}% CPU cap; running uncapped")

    cfg = load_server_config()
    # Precedence, most explicit wins:
    #   host: KANBAN_HOST env  >  server.json  >  built-in loopback default
    #   port: argv[1]  >  KANBAN_PORT env  >  server.json  >  built-in default
    host = os.environ.get("KANBAN_HOST") or cfg["host"]
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    elif os.environ.get("KANBAN_PORT"):
        port = int(os.environ["KANBAN_PORT"])
    else:
        port = cfg["port"]
    server = HTTPServer((host, port), KanbanHandler)
    print(f"Kanban server running at http://localhost:{port} (bound to {host})")
    print(f"Serving boards from: {KANBAN_DIR}")
    if not os.environ.get("KANBAN_TOKEN"):
        print(f"Auth token (also injected into the UI): {AUTH_TOKEN}")
    # Discover the live model picklist for this run (falls back to
    # DEFAULT_MODELS with no ANTHROPIC_API_KEY or on a failed request).
    refresh_models()
    # Start the orchestrator tick loop alongside the server. Idempotent and
    # lock-guarded, so running several servers only ever yields one loop.
    ensure_orchestrator_running()
    # Start the system-wide Claude session performance sampler.
    ensure_perf_sampler_running()
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        stop_orchestrator()
        server.server_close()


if __name__ == "__main__":
    main()
