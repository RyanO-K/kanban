import json
import os
import tempfile

import kanban_server as ks


def test_perf_snapshot_returns_sampler_cache(monkeypatch):
    fake = {"available": True, "sampledAt": "t", "totals": {}, "sessions": []}

    class FakeSampler:
        def snapshot(self):
            return fake

    monkeypatch.setattr(ks, "_PERF_SAMPLER", FakeSampler())
    data, status = ks.perf_snapshot()
    assert status == 200
    assert data is fake


def test_perf_kill_validates_pid(monkeypatch):
    monkeypatch.setattr(ks.perf_monitor, "kill_session",
                        lambda pid: {"killed": [pid], "ok": True})
    data, status = ks.perf_kill("10")
    assert status == 200 and data["killed"] == [10]
    data, status = ks.perf_kill("notanint")
    assert status == 400


# --- _owned_pids after reboot ---

def _write_ticket(board_dir, ticket_id, data):
    path = os.path.join(board_dir, f"{ticket_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def test_owned_pids_includes_in_progress_ticket_pids_after_reboot(monkeypatch, tmp_path):
    """After a server reboot _PROCS is empty, but in_progress tickets with
    orchestrator.pid must still be counted as owned so the Performance tab
    does not show them as external."""
    board_dir = tmp_path / "kanban-dev"
    board_dir.mkdir()
    # Write a _meta.json so is_board() recognises it.
    (board_dir / "_meta.json").write_text(json.dumps({"project": "Test"}), encoding="utf-8")
    # An in_progress ticket with an orchestrator PID — this is the session the
    # server spawned but lost track of after the reboot.
    _write_ticket(str(board_dir), "7", {
        "id": "7",
        "status": "in_progress",
        "orchestrator": {"pid": 1234, "state": "dispatched"},
    })
    # A completed ticket should NOT contribute its pid (pid may be reused).
    _write_ticket(str(board_dir), "5", {
        "id": "5",
        "status": "completed",
        "orchestrator": {"pid": 9999, "state": "done"},
    })
    # A ticket without orchestrator block should be skipped safely.
    _write_ticket(str(board_dir), "3", {
        "id": "3",
        "status": "in_progress",
    })

    monkeypatch.setattr(ks, "KANBAN_DIR", str(tmp_path))

    # Simulate empty _PROCS / _SERVER_OPS (fresh boot).
    import orchestrator as _orch
    original_procs = dict(_orch._PROCS)
    original_ops = dict(_orch._SERVER_OPS)
    _orch._PROCS.clear()
    _orch._SERVER_OPS.clear()
    try:
        pids = ks._owned_pids()
    finally:
        _orch._PROCS.update(original_procs)
        _orch._SERVER_OPS.update(original_ops)

    assert 1234 in pids, "in_progress orchestrator PID must be owned after reboot"
    assert 9999 not in pids, "completed ticket PIDs must not be owned"


def test_owned_pids_includes_procs_and_server_ops(monkeypatch, tmp_path):
    """_PROCS and _SERVER_OPS still contribute even when no tickets are on disk."""
    monkeypatch.setattr(ks, "KANBAN_DIR", str(tmp_path))

    import orchestrator as _orch

    class FakeProc:
        pass

    original_procs = dict(_orch._PROCS)
    original_ops = dict(_orch._SERVER_OPS)
    _orch._PROCS.clear()
    _orch._SERVER_OPS.clear()
    _orch._PROCS[42] = FakeProc()
    _orch._SERVER_OPS[99] = "triage"
    try:
        pids = ks._owned_pids()
    finally:
        _orch._PROCS.clear()
        _orch._SERVER_OPS.clear()
        _orch._PROCS.update(original_procs)
        _orch._SERVER_OPS.update(original_ops)

    assert 42 in pids
    assert 99 in pids


def test_owned_pids_tolerates_malformed_ticket(monkeypatch, tmp_path):
    """Malformed or unreadable ticket files must not crash _owned_pids."""
    board_dir = tmp_path / "kanban-dev"
    board_dir.mkdir()
    (board_dir / "_meta.json").write_text(json.dumps({"project": "Test"}), encoding="utf-8")
    # Write a ticket with invalid JSON.
    (board_dir / "1.json").write_text("not valid json", encoding="utf-8")

    monkeypatch.setattr(ks, "KANBAN_DIR", str(tmp_path))

    import orchestrator as _orch
    original_procs = dict(_orch._PROCS)
    original_ops = dict(_orch._SERVER_OPS)
    _orch._PROCS.clear()
    _orch._SERVER_OPS.clear()
    try:
        pids = ks._owned_pids()  # must not raise
    finally:
        _orch._PROCS.update(original_procs)
        _orch._SERVER_OPS.update(original_ops)

    assert isinstance(pids, set)
