#!/usr/bin/env python3
"""Backfill the `_kanbanGuide` field onto every existing ticket.

Stamps each ticket JSON with the self-describing guide string so an LLM handed a
single ticket file path can locate the board docs and learn how to use the board.

Idempotent: tickets that already have `_kanbanGuide` are left untouched (the value
is never overwritten, so re-running never causes drift). New tickets created via
the server get the field automatically; this script is only for ones that predate
that change or were created by hand.

Run from anywhere:  python .kanban/backfill_kanban_guide.py
"""

import json
import os

from kanban_server import KANBAN_DIR, KANBAN_GUIDE, META_FILE, boards_root


def iter_ticket_files(kanban_dir):
    """Yield every ticket file path across all boards (dirs with a _meta.json).

    Boards live under the dedicated `boards/` folder (ticket #94), so scan there.
    A missing folder just means no boards.
    """
    try:
        entries = os.scandir(boards_root())
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if not os.path.isfile(os.path.join(entry.path, META_FILE)):
            continue  # not a board
        for f in os.scandir(entry.path):
            if f.is_file() and f.name.endswith(".json") and f.name != META_FILE:
                yield f.path


def backfill(kanban_dir):
    updated, skipped, errors = 0, 0, 0
    for path in iter_ticket_files(kanban_dir):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                task = json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ERROR reading {path}: {e}")
            errors += 1
            continue

        if task.get("_kanbanGuide"):
            skipped += 1
            continue

        task["_kanbanGuide"] = KANBAN_GUIDE
        try:
            # Match the server's exact on-disk formatting.
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(task, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        except OSError as e:
            print(f"  ERROR writing {path}: {e}")
            errors += 1
            continue

        rel = os.path.relpath(path, kanban_dir)
        print(f"  stamped {rel}")
        updated += 1

    print(f"\nDone: {updated} stamped, {skipped} already had it, {errors} errors.")
    return errors


if __name__ == "__main__":
    raise SystemExit(1 if backfill(KANBAN_DIR) else 0)
