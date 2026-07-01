#!/usr/bin/env python3
"""Kanban board server — serves task boards from .AI-kanban/ as a kanban API.

A *board* is a subdirectory of .AI-kanban/ that contains a `_meta.json` file.
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
from urllib.parse import urlparse, unquote

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


# This script lives directly inside .AI-kanban/, so the board root is its own dir.
KANBAN_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(KANBAN_DIR, "kanban.html")
META_FILE = "_meta.json"
# Specs / plans live as markdown under .AI-kanban/docs/. A doc associates itself
# with a ticket via a `**Ticket:** `.AI-kanban/<board>/<id>.json`` line in its header
# (the convention used by the brainstorming/writing-plans skills).
DOCS_DIR = os.path.join(KANBAN_DIR, "docs")

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
    "This is a .AI-kanban board ticket (file-based kanban). To learn how to use this "
    "board, read .AI-kanban/CLAUDE.md (the agent guide: status values, history/session "
    "conventions, server API) and this ticket's sibling _meta.json (project context). "
    "Tickets are JSON; agents read and edit them in place. "
    "Reusable skills for board work live in .AI-kanban/skills/<skill-name>/SKILL.md; "
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
# Mirrored in kanban.html (MODEL_OPTIONS). An empty string clears the override.
MODEL_VALUES = {
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-8",
}

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
    return os.path.join(KANBAN_DIR, safe), safe


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
# A spec or plan is a markdown file under .AI-kanban/docs/ that links itself to a
# ticket. The link is a header line of the form
#     **Ticket:** `.AI-kanban/<board>/<id>.json`
# (the convention written by the brainstorming / writing-plans skills). We scan
# docs/ once per board load, build a {board/id -> [docs]} index, and attach the
# matching docs to each ticket as `_specs`. A ticket may also opt in explicitly
# via a `spec` / `specs` field holding doc path(s) relative to .AI-kanban/.

_TICKET_REF_RE = re.compile(
    r"\*\*Ticket:\*\*\s*`?\.AI-kanban[\\/]([^\s`/\\]+)[\\/](\d+)\.json`?", re.IGNORECASE
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
    """Map 'board/id' -> [doc descriptor, ...] by scanning .AI-kanban/docs/.

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
    field holding a doc path (string or list) relative to .AI-kanban/. Discovered
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
            for prefix in (".AI-kanban/", ".AI-kanban\\"):
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                    break
            rel = rel.replace("\\", "/").lstrip("/")
            abs_path = os.path.normpath(os.path.join(KANBAN_DIR, rel))
            # Confine explicit refs to the .AI-kanban/ tree.
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
    """Return the raw text of a doc under .AI-kanban/docs/, or (None, status).

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
    for entry in sorted(os.scandir(KANBAN_DIR), key=lambda e: e.name):
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
    for entry in sorted(os.scandir(KANBAN_DIR), key=lambda e: e.name):
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
        "mtime": latest_mtime,
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
        task["_filePath"] = os.path.abspath(tp).replace("\\", "/")
        attach_specs(task, safe, spec_index)
        tasks.append(task)
    tasks.sort(key=task_sort_key)

    result = {
        "project": meta.get("project", safe),
        "updated": meta.get("updated", ""),
        "filename": safe,
        "tasks": tasks,
        "mtime": board_mtime(path),
        "columns": COLUMNS,
    }
    # Pass through optional board-level metadata if present.
    for key in ("context", "openQuestions", "outOfScope", "commitRequirements",
                "directory", "useWorktrees"):
        if key in meta:
            result[key] = meta[key]
    # Surface the one-paragraph context blurb as a flat field for the settings
    # UI (the rest of `context` may hold arbitrary structured keys).
    if isinstance(meta.get("context"), dict) and meta["context"].get("description"):
        result["description"] = meta["context"]["description"]
    return result, 200


# Board-level metadata fields the UI is allowed to edit. `commitRequirements`
# is a free-text, natural-language statement of what must hold before an agent
# commits/completes work (e.g. "all tests must pass") — agents read it from
# _meta.json. `directory` is the project's working directory on disk.
# `useWorktrees` is the per-project boolean (ticket #40) gating whether tickets
# are worked in a git worktree or in place on a branch. The flat `description`
# field is handled specially (merged into `context.description`).
EDITABLE_META_FIELDS = ("project", "context", "openQuestions", "outOfScope",
                        "commitRequirements", "directory", "useWorktrees")


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
        if isinstance(value, str):
            value = value.strip()
        if value == "" or value is None:
            meta.pop(key, None)
        else:
            meta[key] = value

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

    entry = {
        "action": "status_change",
        "from": None,  # filled from the fresh read below
        "to": COLUMN_STATUS[new_column],
        "timestamp": now_iso(),
    }

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

    entry["from"] = task.get("status", "todo")
    task["status"] = COLUMN_STATUS[new_column]
    task.setdefault("history", []).append(entry)

    try:
        write_ticket(tp, task)
    except OSError as e:
        return {"error": str(e)}, 500
    touch_meta(path)

    return {"ok": True, "taskId": task_id, "newStatus": COLUMN_STATUS[new_column]}, 200


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
    if model and model in MODEL_VALUES:
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
    return os.path.join(KANBAN_DIR, bsafe, isafe)


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


# --- Performance monitor ----------------------------------------------------

def _owned_pids():
    """PIDs the orchestrator spawned, for owned/external tagging."""
    try:
        import orchestrator as _orch
        return set(_orch._PROCS.keys())
    except Exception:
        return set()


_PERF_SAMPLER = perf_monitor.PerfSampler(owned_pids_fn=_owned_pids)


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


class KanbanHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/files":
            self._json(scan_boards())
        elif path.startswith("/api/board/"):
            slug = unquote(path[len("/api/board/"):])
            data, status = load_board(slug)
            self._json(data, status)
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

    def log_message(self, format, *args):
        pass


def main():
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
