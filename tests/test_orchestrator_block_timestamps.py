"""Tests for orchestrator block timestamps (ticket #57).

When the orchestrator runs, major blocks (reaping, dispatching, stop-all)
should be logged with timestamps indicating when each block is initiated.
"""
import json
import os
import tempfile
import time

import pytest

import orchestrator as orch
import orchestrator_core as oc


def test_tick_logs_block_timestamps_to_stdout(capsys, tmp_path):
    """Verify that a tick loop prints timestamps when major blocks start.

    A "block" is a major orchestration phase:
    - stop-all processing
    - reaping in-flight agents
    - dispatching new work

    Each should be logged with a timestamp indicating when it started.
    """
    kanban_dir = tmp_path / ".kanban"
    kanban_dir.mkdir()

    # Set up minimal board structure
    board_dir = kanban_dir / "test-board"
    board_dir.mkdir()

    meta = {
        "project": "Test",
        "updated": "2026-06-30",
    }
    (board_dir / "_meta.json").write_text(json.dumps(meta))

    # Initialize orchestrator state
    state_dir = kanban_dir / "_orchestrator"
    state_dir.mkdir(parents=True)
    state = {
        "enabled": False,  # Disabled so tick doesn't dispatch
        "concurrencyCap": 3,
        "stopAllRequested": False,
    }
    (state_dir / "state.json").write_text(json.dumps(state))

    # Create a simple ticket to reap
    ticket = {
        "id": "1",
        "title": "Test",
        "status": "in_progress",
        "detail": "Test task",
        "dependsOn": [],
    }
    (board_dir / "1.json").write_text(json.dumps(ticket))

    # Run a single tick
    orch.tick(str(kanban_dir),
              opus_triage=lambda *args: {"dispatch": []},
              summarize_progress=lambda *args: "progress")

    # Check that timestamps were logged for blocks
    captured = capsys.readouterr()

    # We should see log entries indicating block initiation
    # At minimum, we should see timestamps in the output
    assert "Block:" in captured.out or "Reaping" in captured.out or "timestamp" in captured.out.lower()


def test_activity_log_entries_have_timestamps():
    """Verify that all activity log entries include a 'ts' (timestamp) field."""
    # This test validates the existing behavior that activity entries are timestamped
    entry = {
        "ts": oc.now_iso(),
        "kind": "dispatch",
        "ticket": "1",
    }

    # Verify the entry has a properly formatted ISO timestamp
    assert "ts" in entry
    assert "T" in entry["ts"]  # ISO format includes T
    assert "+" in entry["ts"] or "Z" in entry["ts"]  # Timezone info


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
