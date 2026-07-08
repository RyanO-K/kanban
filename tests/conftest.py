import json
import os
import sys

import pytest

# Make the app modules (app/kanban_server.py, app/orchestrator*.py, ...) and the
# one-shot scripts (scripts/migrate_boards_folder.py) importable by bare name.
KANBAN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KANBAN_ROOT, "scripts"))
sys.path.insert(0, os.path.join(KANBAN_ROOT, "app"))


@pytest.fixture
def kanban(tmp_path):
    """A temp .kanban tree with one board and two tickets.

    Boards live under the dedicated `boards/` folder (ticket #94), so the demo
    board is created at `<root>/boards/demo`.
    """
    root = tmp_path / ".kanban"
    board = root / "boards" / "demo"
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
