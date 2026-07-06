"""Ticket #94: the one-shot board-folder migration script."""

import json
import os

import migrate_boards_folder as mig


def _mk_board(root, name, under_boards=False):
    base = os.path.join(root, "boards", name) if under_boards else os.path.join(root, name)
    os.makedirs(base)
    with open(os.path.join(base, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"project": name}, f)
    with open(os.path.join(base, "1.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "1", "status": "todo"}, f)
    return base


def _mk_nonboard(root, name):
    d = os.path.join(root, name)
    os.makedirs(d)
    return d


def test_find_loose_boards_only_returns_board_dirs(tmp_path):
    root = str(tmp_path)
    _mk_board(root, "alpha")
    _mk_board(root, "beta")
    _mk_nonboard(root, "config")        # not a board (no _meta.json)
    _mk_nonboard(root, "_orchestrator")
    _mk_nonboard(root, "plain-dir")     # no _meta.json
    assert mig.find_loose_boards(root) == ["alpha", "beta"]


def test_migrate_moves_loose_boards_under_boards(tmp_path):
    root = str(tmp_path)
    _mk_board(root, "alpha")
    _mk_board(root, "beta")
    errors = mig.migrate(root)
    assert errors == 0
    # Boards now live under boards/, gone from the root.
    assert not os.path.isdir(os.path.join(root, "alpha"))
    assert os.path.isfile(os.path.join(root, "boards", "alpha", "_meta.json"))
    assert os.path.isfile(os.path.join(root, "boards", "alpha", "1.json"))
    assert os.path.isfile(os.path.join(root, "boards", "beta", "_meta.json"))


def test_migrate_is_idempotent(tmp_path):
    root = str(tmp_path)
    _mk_board(root, "alpha")
    assert mig.migrate(root) == 0
    # Second run: nothing loose left, no-op, no errors.
    assert mig.migrate(root) == 0
    assert os.path.isfile(os.path.join(root, "boards", "alpha", "_meta.json"))


def test_migrate_refuses_to_clobber_existing_destination(tmp_path):
    root = str(tmp_path)
    _mk_board(root, "alpha")                 # loose
    _mk_board(root, "alpha", under_boards=True)  # already exists under boards/
    errors = mig.migrate(root)
    assert errors == 1  # refused to overwrite
    # The loose copy is left in place (not destroyed).
    assert os.path.isdir(os.path.join(root, "alpha"))


def test_migrate_dry_run_moves_nothing(tmp_path):
    root = str(tmp_path)
    _mk_board(root, "alpha")
    mig.migrate(root, dry_run=True)
    assert os.path.isdir(os.path.join(root, "alpha"))
    assert not os.path.isdir(os.path.join(root, "boards", "alpha"))
