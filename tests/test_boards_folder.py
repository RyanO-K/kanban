"""Ticket #94: boards live in a dedicated `boards/` folder under .kanban.

Board directories used to sit loose at the top level of `.kanban/`, mixed in
with source files (kanban_server.py, orchestrator.py, config/, docs/, ...).
They are now collected under a single dedicated, gitignored `boards/` folder.
Discovery and per-board path resolution must therefore look under
`<kanban_dir>/boards/<slug>`, and must NOT treat a loose top-level directory
as a board.
"""

import json
import os

import pytest

import kanban_server as ks
import orchestrator_core as oc
import orchestrator as orch


BOARDS_SUBDIR = "boards"


def _make_board(root, slug, meta=None, tickets=None):
    """Create a board under <root>/boards/<slug>."""
    bdir = os.path.join(root, BOARDS_SUBDIR, slug)
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta or {"project": slug}, f)
    for tid, task in (tickets or {}).items():
        with open(os.path.join(bdir, f"{tid}.json"), "w", encoding="utf-8") as f:
            json.dump(task, f)
    return bdir


# --- kanban_server board root ------------------------------------------------

def test_boards_root_is_boards_subdir(tmp_path, monkeypatch):
    root = str(tmp_path / ".kanban")
    monkeypatch.setattr(ks, "KANBAN_DIR", root)
    assert ks.boards_root() == os.path.join(root, BOARDS_SUBDIR)


def test_board_dir_resolves_under_boards(tmp_path, monkeypatch):
    root = str(tmp_path / ".kanban")
    monkeypatch.setattr(ks, "KANBAN_DIR", root)
    path, safe = ks.board_dir("demo")
    assert path == os.path.join(root, BOARDS_SUBDIR, "demo")
    assert safe == "demo"


def test_scan_boards_finds_boards_under_boards_folder(tmp_path, monkeypatch):
    root = str(tmp_path / ".kanban")
    monkeypatch.setattr(ks, "KANBAN_DIR", root)
    _make_board(root, "demo", {"project": "Demo"}, {"1": {"id": "1", "status": "todo"}})
    boards = ks.scan_boards()
    slugs = {b["filename"] for b in boards}
    assert slugs == {"demo"}


def test_scan_boards_ignores_loose_top_level_board(tmp_path, monkeypatch):
    """A directory with a _meta.json at the OLD top-level location is not a board."""
    root = str(tmp_path / ".kanban")
    monkeypatch.setattr(ks, "KANBAN_DIR", root)
    # Legacy layout: board dir sitting directly under .kanban/, not under boards/.
    legacy = os.path.join(root, "legacy-board")
    os.makedirs(legacy)
    with open(os.path.join(legacy, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"project": "Legacy"}, f)
    _make_board(root, "demo", {"project": "Demo"})
    slugs = {b["filename"] for b in ks.scan_boards()}
    assert slugs == {"demo"}


# --- orchestrator_core.read_board_meta ---------------------------------------

def test_read_board_meta_reads_under_boards(tmp_path):
    root = str(tmp_path / ".kanban")
    _make_board(root, "demo", {"project": "Demo"})
    assert oc.read_board_meta(root, "demo").get("project") == "Demo"


# --- orchestrator.load_all_tasks ---------------------------------------------

def test_load_all_tasks_scans_boards_folder(tmp_path):
    root = str(tmp_path / ".kanban")
    _make_board(
        root, "demo", {"project": "Demo"},
        {"1": {"id": "1", "status": "todo"}, "2": {"id": "2", "status": "ready"}},
    )
    tasks = orch.load_all_tasks(root)
    assert {t["id"] for t in tasks} == {"1", "2"}
    assert all(t["_board"] == "demo" for t in tasks)
