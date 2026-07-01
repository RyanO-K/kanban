import json
import os
import sys

import pytest

# Make the .AI-kanban dir importable (orchestrator_core.py lives there).
KANBAN_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KANBAN_SRC)


@pytest.fixture
def kanban(tmp_path):
    """A temp .AI-kanban tree with one board and two tickets."""
    root = tmp_path / ".AI-kanban"
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
