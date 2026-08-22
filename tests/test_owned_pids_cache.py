"""_ticket_agent_pids must not re-scan every ticket file on every call.

The perf sampler calls _owned_pids -> _ticket_agent_pids every 3 seconds; the
disk scan exists only to recover PIDs dispatched before a server restart, so it
is cached with a TTL. Re-reading every board's ticket JSON (10+ MB across ~200
files) 20x/minute burned ~15-20% of a core at idle — and every file open was
also inspected by the endpoint-security filter driver, multiplying the cost in
kernel time (the Task Manager "System" process).
"""
import json

import kanban_server as ks


def _make_board(kanban, pid=4242):
    board_dir = f"{kanban}/boards/demo"
    with open(f"{board_dir}/1.json", "w", encoding="utf-8") as f:
        json.dump({"id": "1", "status": "in_progress",
                   "orchestrator": {"pid": pid, "state": "dispatched"}}, f)


def test_scan_cached_within_ttl(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks._ticket_pids_cache_clear()
    _make_board(kanban)

    calls = {"n": 0}
    real_scandir = ks._scandir_boards

    def counting_scandir():
        calls["n"] += 1
        return real_scandir()

    monkeypatch.setattr(ks, "_scandir_boards", counting_scandir)

    assert ks._ticket_agent_pids(now=100.0) == {4242}
    assert ks._ticket_agent_pids(now=101.0) == {4242}
    assert ks._ticket_agent_pids(now=102.0) == {4242}
    assert calls["n"] == 1, "calls inside the TTL must reuse the cached scan"


def test_scan_refreshes_after_ttl(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks._ticket_pids_cache_clear()
    _make_board(kanban)

    assert ks._ticket_agent_pids(now=100.0) == {4242}
    # Ticket picked up by a new PID; visible only after the TTL lapses.
    _make_board(kanban, pid=5555)
    assert ks._ticket_agent_pids(now=101.0) == {4242}
    assert ks._ticket_agent_pids(now=100.0 + ks._TICKET_PIDS_TTL + 1) == {5555}
