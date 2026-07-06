#!/usr/bin/env python3
"""One-shot migration: relocate loose board dirs into the dedicated `boards/` folder.

Ticket #94 moved board directories out of the .kanban root (where they sat mixed
in with source files) into a dedicated, gitignored `boards/` subfolder. Board
discovery in kanban_server.py / orchestrator*.py now looks ONLY under `boards/`,
so existing boards must be physically moved once, at deploy time.

A board is any top-level .kanban subdirectory containing a `_meta.json` (the same
rule the server uses). Non-board dirs (`config/`, `docs/`, `skills/`, `tests/`,
`_orchestrator/`, `__pycache__/`, and `boards/` itself) are left untouched.

DEPLOY ORDER (important — do NOT run this against a live old-code orchestrator):
  1. Stop the orchestrator + server (they scan the old flat layout and would lose
     every board the instant it moves).
  2. Deploy the ticket #94 code (merge to release).
  3. Run this script once:  python .kanban/migrate_boards_folder.py
  4. Restart the orchestrator + server.

Idempotent: a board already under `boards/` is skipped; re-running is a no-op.
Refuses to clobber an existing destination. `--dry-run` prints the plan only.
"""

import os
import shutil
import sys

# This script lives directly inside .kanban/, so the board root is its own dir.
KANBAN_DIR = os.path.dirname(os.path.abspath(__file__))
BOARDS_SUBDIR = "boards"
META_FILE = "_meta.json"

# Top-level dirs that are never boards (mirrors the server's scan exclusions).
NON_BOARD_DIRS = {
    BOARDS_SUBDIR, "config", "docs", "skills", "tests",
    "_orchestrator", "__pycache__", ".git", ".claude",
}


def find_loose_boards(kanban_dir):
    """Return sorted names of board dirs still sitting loose at the root."""
    out = []
    for entry in os.scandir(kanban_dir):
        if not entry.is_dir() or entry.name in NON_BOARD_DIRS:
            continue
        if os.path.isfile(os.path.join(entry.path, META_FILE)):
            out.append(entry.name)
    return sorted(out)


def migrate(kanban_dir, dry_run=False):
    boards_root = os.path.join(kanban_dir, BOARDS_SUBDIR)
    loose = find_loose_boards(kanban_dir)
    if not loose:
        print("Nothing to migrate: no loose boards at the .kanban root.")
        return 0

    if not dry_run:
        os.makedirs(boards_root, exist_ok=True)

    moved, errors = 0, 0
    for name in loose:
        src = os.path.join(kanban_dir, name)
        dst = os.path.join(boards_root, name)
        if os.path.exists(dst):
            print(f"  SKIP {name}: destination already exists at boards/{name}")
            errors += 1
            continue
        if dry_run:
            print(f"  would move {name} -> boards/{name}")
            moved += 1
            continue
        try:
            shutil.move(src, dst)
        except OSError as e:
            print(f"  ERROR moving {name}: {e}")
            errors += 1
            continue
        print(f"  moved {name} -> boards/{name}")
        moved += 1

    verb = "would move" if dry_run else "moved"
    print(f"\nDone: {verb} {moved} board(s), {errors} skipped/error(s).")
    return errors


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv[1:]
    raise SystemExit(1 if migrate(KANBAN_DIR, dry_run=dry) else 0)
