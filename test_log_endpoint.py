"""Tests for the live-logs endpoint (ticket #45).

Covers the pure stream-json -> turns parser, the run-log path confinement, and
the task_log assembly (running flag + empty/absent states).
"""
import json
import os

import pytest

import kanban_server as ks


# --- parse_log_turns -------------------------------------------------------

def _line(obj):
    return json.dumps(obj)


def test_assistant_text_turn():
    log = _line({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Hello, working on it."}
    ]}})
    turns = ks.parse_log_turns(log)
    assert len(turns) == 1
    assert turns[0]["role"] == "assistant"
    assert turns[0]["text"] == "Hello, working on it."
    assert turns[0]["tools"] == []
    assert turns[0]["seq"] == 0


def test_thinking_block_included():
    log = _line({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "Let me reason about this."}
    ]}})
    turns = ks.parse_log_turns(log)
    assert turns[0]["text"] == "Let me reason about this."


def test_tool_use_becomes_chip_with_summary():
    log = "\n".join([
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "foo.py"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ]}}),
    ])
    turns = ks.parse_log_turns(log)
    assert len(turns) == 1
    names = [(t["name"], t["summary"]) for t in turns[0]["tools"]]
    assert ("Read", "foo.py") in names
    assert ("Bash", "ls -la") in names


def test_tool_result_attaches_to_chip_not_own_turn():
    # A tool_result is folded into the chip of the tool_use it answers (matched
    # by tool_use_id) rather than producing a standalone `user` turn.
    log = "\n".join([
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Read",
             "input": {"file_path": "foo.py"}},
        ]}}),
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "file body here"}
        ]}}),
    ])
    turns = ks.parse_log_turns(log)
    # Only the assistant turn survives — no `user` turn.
    assert [t["role"] for t in turns] == ["assistant"]
    chip = turns[0]["tools"][0]
    assert chip["name"] == "Read"
    assert chip["result"] == "file body here"


def test_tool_result_truncated_in_chip():
    big = "x" * 9000
    log = "\n".join([
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash",
             "input": {"command": "cat big"}},
        ]}}),
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": big}
        ]}}),
    ])
    turns = ks.parse_log_turns(log)
    result = turns[0]["tools"][0]["result"]
    assert result.endswith("…")  # ellipsis
    assert len(result) <= ks._TOOL_RESULT_PREVIEW + 1


def test_tool_result_list_content_in_chip():
    log = "\n".join([
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Grep", "input": {"pattern": "x"}},
        ]}}),
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "content": [{"type": "text", "text": "result body"}]}
        ]}}),
    ])
    turns = ks.parse_log_turns(log)
    assert turns[0]["tools"][0]["result"] == "result body"


def test_tool_use_without_result_has_empty_result():
    # An in-flight tool call (no matching tool_result yet) carries result="".
    log = _line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu_1", "name": "Bash",
         "input": {"command": "sleep 100"}},
    ]}})
    turns = ks.parse_log_turns(log)
    assert turns[0]["tools"][0]["result"] == ""


def test_malformed_and_housekeeping_lines_skipped():
    log = "\n".join([
        "not json at all",
        _line({"type": "system", "subtype": "init"}),
        _line({"type": "result", "subtype": "success"}),
        _line({"type": "rate_limit_event"}),
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the only real turn"}
        ]}}),
        "",
    ])
    turns = ks.parse_log_turns(log)
    assert len(turns) == 1
    assert turns[0]["text"] == "the only real turn"


def test_empty_turns_dropped():
    # An assistant line with no text and no tools (e.g. an empty content block)
    # should not produce a turn.
    log = "\n".join([
        _line({"type": "assistant", "message": {"content": []}}),
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "   "}
        ]}}),
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "kept"}
        ]}}),
    ])
    turns = ks.parse_log_turns(log)
    assert len(turns) == 1
    assert turns[0]["text"] == "kept"


def test_tail_limit_and_seq():
    lines = [
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"turn {i}"}
        ]}})
        for i in range(10)
    ]
    turns = ks.parse_log_turns("\n".join(lines), n=3)
    assert [t["text"] for t in turns] == ["turn 7", "turn 8", "turn 9"]
    assert [t["seq"] for t in turns] == [0, 1, 2]  # seq re-based after tailing


# --- path confinement ------------------------------------------------------

def test_resolve_run_log_confines_to_runs(monkeypatch, tmp_path):
    kanban = tmp_path / ".kanban"
    runs = kanban / "_orchestrator" / "runs"
    runs.mkdir(parents=True)
    monkeypatch.setattr(ks, "KANBAN_DIR", str(kanban))
    monkeypatch.setattr(ks, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(ks, "RUNS_DIR", str(runs))

    # A legit run-log resolves.
    ok = ks._resolve_run_log(".kanban/_orchestrator/runs/5-now.log")
    assert ok == os.path.normpath(str(runs / "5-now.log"))

    # Traversal out of runs/ is rejected.
    assert ks._resolve_run_log(".kanban/_orchestrator/runs/../../kanban.html") is None
    # A path outside runs/ entirely is rejected.
    assert ks._resolve_run_log(".kanban/kanban.html") is None
    # Empty / missing logFile -> None.
    assert ks._resolve_run_log("") is None
    assert ks._resolve_run_log(None) is None


# --- task_log assembly -----------------------------------------------------

def _make_tree(monkeypatch, tmp_path, marker, status="in_progress", log_text=None):
    kanban = tmp_path / ".kanban"
    board = kanban / "demo"
    runs = kanban / "_orchestrator" / "runs"
    board.mkdir(parents=True)
    runs.mkdir(parents=True)
    ticket = {"id": "1", "title": "T", "status": status}
    if marker is not None:
        ticket["orchestrator"] = marker
    (board / "1.json").write_text(json.dumps(ticket), encoding="utf-8")
    if log_text is not None and marker and marker.get("logFile"):
        # logFile is relative to workspace root; write the file there.
        p = tmp_path / marker["logFile"].replace("/", os.sep)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(log_text, encoding="utf-8")
    monkeypatch.setattr(ks, "KANBAN_DIR", str(kanban))
    monkeypatch.setattr(ks, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(ks, "RUNS_DIR", str(runs))
    return str(kanban)


def test_task_log_no_marker_empty_state(monkeypatch, tmp_path):
    _make_tree(monkeypatch, tmp_path, marker=None, status="todo")
    body, code = ks.task_log("demo", "1")
    assert code == 200
    assert body == {"turns": [], "running": False, "hasLog": False, "status": "todo"}


def test_task_log_running_flag(monkeypatch, tmp_path):
    marker = {"state": "dispatched", "logFile": ".kanban/_orchestrator/runs/1-x.log"}
    log = _line({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "doing the thing"}
    ]}})
    _make_tree(monkeypatch, tmp_path, marker, status="in_progress", log_text=log)
    body, code = ks.task_log("demo", "1")
    assert code == 200
    assert body["running"] is True
    assert body["hasLog"] is True
    assert len(body["turns"]) == 1
    assert body["turns"][0]["text"] == "doing the thing"


def test_task_log_finished_not_running(monkeypatch, tmp_path):
    # Marker present but ticket completed -> running False (UI stops polling).
    marker = {"state": "dispatched", "logFile": ".kanban/_orchestrator/runs/1-x.log"}
    _make_tree(monkeypatch, tmp_path, marker, status="completed", log_text="")
    body, code = ks.task_log("demo", "1")
    assert body["running"] is False
    assert body["hasLog"] is True


def test_task_log_completed_no_orchestrator_uses_completed_log(monkeypatch, tmp_path):
    """A completed ticket whose orchestrator block was cleared (production state after
    clear_marker) but has a completedLog must return those turns with hasLog=True.

    This was the real production gap: the orchestrator clears the marker on completion,
    so task.orchestrator is absent on done tickets. The server must still serve the
    saved completedLog turns in that state.
    """
    kanban = tmp_path / ".kanban"
    board = kanban / "demo"
    runs = kanban / "_orchestrator" / "runs"
    board.mkdir(parents=True)
    runs.mkdir(parents=True)
    saved_turns = [
        {"seq": 0, "role": "assistant", "text": "Step one done.", "tools": []},
        {"seq": 1, "role": "assistant", "text": "All finished!", "tools": []},
    ]
    ticket = {
        "id": "1", "title": "T", "status": "completed",
        # No "orchestrator" key — this is what the ticket looks like after clear_marker.
        "completedLog": saved_turns,
    }
    (board / "1.json").write_text(json.dumps(ticket), encoding="utf-8")
    monkeypatch.setattr(ks, "KANBAN_DIR", str(kanban))
    monkeypatch.setattr(ks, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(ks, "RUNS_DIR", str(runs))

    body, code = ks.task_log("demo", "1")
    assert code == 200
    assert body["running"] is False
    assert body["hasLog"] is True, "completedLog must cause hasLog=True even without orchestrator block"
    turns = body["turns"]
    assert len(turns) == 2, f"expected 2 saved turns, got {turns}"
    assert turns[0]["text"] == "Step one done."
    assert turns[1]["text"] == "All finished!"


def test_task_log_runlogfile_survives_clear_marker(monkeypatch, tmp_path):
    """runLogFile at the top level is readable even after the orchestrator block is cleared.

    The orchestrator calls clear_marker (removes `orchestrator`) on completion/crash/stall,
    but the log file is still on disk and the UI should still show it. Moving logFile to
    top-level `runLogFile` makes it survive clear_marker so task_log can serve it.
    """
    kanban = tmp_path / ".kanban"
    board = kanban / "demo"
    runs = kanban / "_orchestrator" / "runs"
    board.mkdir(parents=True)
    runs.mkdir(parents=True)
    log_text = _line({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "final output after crash"}
    ]}})
    log_rel = ".kanban/_orchestrator/runs/1-crash.log"
    (tmp_path / log_rel.replace("/", os.sep)).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / log_rel.replace("/", os.sep)).write_text(log_text, encoding="utf-8")
    # Ticket with NO orchestrator block (marker was cleared) but runLogFile at top level.
    ticket = {
        "id": "1", "title": "T", "status": "blocked",
        "runLogFile": log_rel,
    }
    (board / "1.json").write_text(json.dumps(ticket), encoding="utf-8")
    monkeypatch.setattr(ks, "KANBAN_DIR", str(kanban))
    monkeypatch.setattr(ks, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(ks, "RUNS_DIR", str(runs))

    body, code = ks.task_log("demo", "1")
    assert code == 200
    assert body["hasLog"] is True, "runLogFile at top-level must be served even after orchestrator block is cleared"
    assert len(body["turns"]) == 1
    assert body["turns"][0]["text"] == "final output after crash"


def test_task_log_missing_ticket_404(monkeypatch, tmp_path):
    _make_tree(monkeypatch, tmp_path, marker=None)
    body, code = ks.task_log("demo", "999")
    assert code == 404


def test_turn_timestamp_preserved():
    """Timestamps in the stream-json are preserved in parsed turns."""
    log = _line({"type": "assistant", "timestamp": "2026-06-30T12:34:56+00:00",
                 "message": {"content": [
        {"type": "text", "text": "Hello"}
    ]}})
    turns = ks.parse_log_turns(log)
    assert len(turns) == 1
    assert turns[0].get("timestamp") == "2026-06-30T12:34:56+00:00"


def test_turn_without_timestamp():
    """Turns without timestamps don't get a timestamp field."""
    log = _line({"type": "assistant",
                 "message": {"content": [
        {"type": "text", "text": "Hello"}
    ]}})
    turns = ks.parse_log_turns(log)
    assert len(turns) == 1
    assert "timestamp" not in turns[0]


def test_turn_timestamp_borrowed_from_tool_result_line():
    """The CLI's stream-json stamps only `user` (tool_result) lines with a
    top-level timestamp — assistant lines carry none. Each assistant turn must
    borrow the nearest following line's timestamp (the tool result that answers
    it), falling back to the nearest preceding one for trailing turns."""
    log = "\n".join([
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the file."},
            {"type": "tool_use", "id": "tu_1", "name": "Read",
             "input": {"file_path": "foo.py"}},
        ]}}),
        _line({"type": "user", "timestamp": "2026-07-01T19:20:40.324Z",
               "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "data"},
        ]}}),
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Done."},
        ]}}),
    ])
    turns = ks.parse_log_turns(log)
    assert len(turns) == 2
    assert turns[0].get("timestamp") == "2026-07-01T19:20:40.324Z", \
        "turn must borrow the timestamp of the tool_result line that follows it"
    assert turns[1].get("timestamp") == "2026-07-01T19:20:40.324Z", \
        "trailing turn must fall back to the nearest preceding timestamp"


def test_turn_own_timestamp_wins_over_borrowed():
    """A line's own top-level timestamp always beats a borrowed neighbour's."""
    log = "\n".join([
        _line({"type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
               "message": {"content": [{"type": "text", "text": "Mine."}]}}),
        _line({"type": "user", "timestamp": "2026-07-01T11:00:00Z",
               "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_x", "content": "r"},
        ]}}),
    ])
    turns = ks.parse_log_turns(log)
    assert turns[0].get("timestamp") == "2026-07-01T10:00:00Z"
