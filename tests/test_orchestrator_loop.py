import json
import os
import threading
import time

import orchestrator_core as oc
import orchestrator as orch


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_tick_disabled_does_not_dispatch(kanban, monkeypatch):
    calls = []
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **k: calls.append(a) or {"state": "dispatched"})
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})
    assert calls == []


def test_tick_enabled_dispatches_eligible(kanban, monkeypatch):
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3, "stopAllRequested": False})

    def fake_spawn(kanban_dir, board, task, profile, model):
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 4242, "dispatchedAt": oc.now_iso(), "killRequested": False,
                "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    # Triage picks ticket 1 (ticket 2 depends on 1, so not eligible).
    orch.tick(kanban, opus_triage=lambda prompt, elig, profs, free: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1["status"] == "in_progress"
    assert t1["orchestrator"]["pid"] == 4242


def test_tick_respects_concurrency_cap(kanban, monkeypatch):
    # Ticket 1 already in-flight; cap is 1 → no new dispatch.
    p = os.path.join(kanban, "demo", "1.json")
    t = _read(p)
    t["status"] = "in_progress"
    t["orchestrator"] = {"state": "dispatched", "pid": 1, "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 1, "stopAllRequested": False})

    calls = []
    monkeypatch.setattr(orch, "spawn_agent", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})
    assert calls == []


def test_tick_stop_all_kills(kanban, monkeypatch):
    p = os.path.join(kanban, "demo", "1.json")
    t = _read(p)
    t["status"] = "in_progress"
    t["orchestrator"] = {"state": "dispatched", "pid": 777, "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3, "stopAllRequested": True})

    killed = []
    monkeypatch.setattr(orch, "kill_pid", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert killed == [777]
    t1 = _read(p)
    assert t1["status"] == "blocked"
    assert "orchestrator" not in t1 or t1["orchestrator"].get("state") != "dispatched"
    assert oc.read_state(kanban)["stopAllRequested"] is False


# --- Ticket #56: maxAgentSeconds stalls an alive agent via state ---

def test_tick_max_agent_seconds_stalls_alive_agent(kanban, monkeypatch):
    """When state.maxAgentSeconds is set, an alive agent past that wall-clock cap
    is reaped as stalled even if its log is still growing."""
    p = os.path.join(kanban, "demo", "1.json")
    t = _read(p)
    t["status"] = "in_progress"
    # dispatchedAt far in the past so any cap in seconds will trigger
    from orchestrator_core import now_iso
    import datetime
    past = (datetime.datetime.fromisoformat(now_iso()) -
            datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    t["orchestrator"] = {"state": "dispatched", "pid": 888,
                         "killRequested": False, "dispatchedAt": past}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                             "stopAllRequested": False, "maxAgentSeconds": 60})

    killed = []
    monkeypatch.setattr(orch, "kill_pid", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(orch, "_process_alive", lambda pid: True)
    monkeypatch.setattr(orch, "_summarize_progress", lambda *a, **k: "checkpoint")
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert 888 in killed
    t1 = _read(p)
    assert t1["status"] == "blocked"


# --- Ticket 23: TODO -> Ready promotion in the tick loop ---

def test_tick_promotes_todo_with_met_deps_to_ready(kanban, monkeypatch):
    """Each tick promotes todo tickets whose deps are met into `ready`, appending
    a status_change history entry. Ticket 1 has no deps so it is promoted; ticket
    2 depends on the still-todo ticket 1 so it stays todo."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    t2 = _read(os.path.join(kanban, "demo", "2.json"))
    assert t1["status"] == "ready", f"ticket 1 should be promoted, got {t1['status']}"
    assert t2["status"] == "todo", f"ticket 2 dep unmet, should stay todo, got {t2['status']}"
    # History records the promotion.
    promos = [h for h in t1.get("history", [])
              if h.get("action") == "status_change" and h.get("to") == "ready"]
    assert len(promos) == 1, f"expected one todo->ready history entry, got {t1.get('history')}"
    assert promos[0]["from"] == "todo"


def test_tick_promotion_runs_even_when_disabled(kanban, monkeypatch):
    """Promotion is part of board housekeeping and runs regardless of the
    enabled flag (dispatch is what's gated by enabled, not promotion)."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})
    assert _read(os.path.join(kanban, "demo", "1.json"))["status"] == "ready"


def test_tick_promotes_then_dispatches_from_ready(kanban, monkeypatch):
    """A todo ticket with met deps is promoted to ready and then dispatched in
    the same tick. Triage sees the freshly-promoted ticket as eligible."""
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3, "stopAllRequested": False})

    seen_eligible = []

    def fake_triage(prompt, eligible, profiles, free):
        seen_eligible.extend(str(t["id"]) for t in eligible)
        return {"dispatch": [{"ticket": "1", "profile": "frontend",
                              "model": "m", "reason": "x"}]}

    def fake_spawn(kanban_dir, board, task, profile, model):
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 4242, "dispatchedAt": oc.now_iso(), "killRequested": False,
                "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    orch.tick(kanban, opus_triage=fake_triage)

    # Triage was offered ticket 1 as eligible (it was promoted before dispatch).
    assert "1" in seen_eligible
    # Ticket 2 (dep unmet) was never offered.
    assert "2" not in seen_eligible
    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1["status"] == "in_progress"
    assert t1["orchestrator"]["pid"] == 4242


# --- NEW TESTS (must be RED against current code) ---

def _set_dispatched(kanban, ticket_id, pid, dispatched_at=None):
    """Helper: put a ticket into dispatched state with a marker."""
    p = os.path.join(kanban, "demo", f"{ticket_id}.json")
    t = _read(p)
    t["status"] = "in_progress"
    t["orchestrator"] = {
        "state": "dispatched",
        "pid": pid,
        "killRequested": False,
        "dispatchedAt": dispatched_at or oc.now_iso(),
        "logFile": ".kanban/_orchestrator/runs/fake.log",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    return p


def test_tick_crash_sets_blocked_with_error_activity(kanban, monkeypatch):
    """When a dispatched agent exits with non-zero code, tick must classify it
    as 'crashed' and transition the ticket to 'blocked' with an 'error' activity
    entry — NOT mark it as 'completed'.

    This test is RED against current code because _exit_code() always returns 0,
    so the crash path is unreachable and the ticket is wrongly marked 'completed'.
    """
    pid = 9999
    p = _set_dispatched(kanban, "1", pid)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})

    # Monkeypatch: process is dead, exited with code 1 (crash).
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 1)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "blocked", (
        f"Expected 'blocked' after crash, got '{t1['status']}' — "
        "crash path unreachable because _exit_code always returned 0"
    )

    activity = oc.read_activity(kanban)
    kinds = [e.get("kind") for e in activity]
    assert "error" in kinds, (
        f"Expected an 'error' activity entry after crash, got: {kinds}"
    )
    # Must NOT have a 'complete' activity entry for this ticket.
    complete_entries = [e for e in activity if e.get("kind") == "complete"
                        and str(e.get("ticket")) == "1"]
    assert complete_entries == [], (
        f"Ticket should NOT have been marked complete after a crash: {complete_entries}"
    )


def test_tick_idle_stalled_kills_and_blocks(kanban, monkeypatch):
    """An agent whose log has not grown past idle_seconds is reaped: kill + block.
    (Replaces the removed productivity_check tests — idle detection is the gate now.)"""
    pid = 6666
    p = _set_dispatched(kanban, "1", pid, dispatched_at="2020-01-01T00:00:00+00:00")
    t = _read(p)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    # Idle tracking lives in the sidecar now (ticket #42): log already seen at
    # size 50 with a long-stale lastGrowthAt → this tick sees no growth.
    orch.write_idle(kanban, "demo", "1",
                    {"logSize": 50, "lastGrowthAt": "2020-01-01T00:00:00+00:00"})
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False, "idleSeconds": 600})

    killed = []
    monkeypatch.setattr(orch, "kill_pid", lambda _pid: killed.append(_pid) or True)
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(orch, "_summarize_progress", lambda *a, **k: "checkpoint")

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert killed == [pid], f"Idle agent should have been killed, got killed={killed}"
    assert _read(p)["status"] == "blocked"


# --- I2: unknown-pid dead process after restart => crashed ---

def test_unknown_dead_pid_after_restart_treated_as_crashed(kanban, monkeypatch):
    """After an orchestrator restart, _PROCS is empty.  A dispatched ticket whose
    pid is NOT in _PROCS and is NOT alive must be treated as CRASHED, not completed.

    RED: current _exit_code() returns 0 for unknown pids → reap_decision says
    "completed" → ticket is wrongly marked 'completed' instead of 'blocked'.
    """
    pid = 31337  # some pre-restart pid — definitely NOT in orch._PROCS
    # Ensure the pid is not registered in _PROCS (restart simulation).
    orch._PROCS.pop(pid, None)

    p = _set_dispatched(kanban, "1", pid)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})

    # Process is dead (not alive).
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "blocked", (
        f"Expected 'blocked' (crashed) for unknown dead pid after restart, "
        f"got '{t1['status']}' — _exit_code() returned 0 for unknown pid, "
        f"causing false 'completed' classification."
    )

    # Must have an 'error' activity entry (crash path), not a 'complete' entry.
    activity = oc.read_activity(kanban)
    kinds = [e.get("kind") for e in activity]
    assert "error" in kinds, (
        f"Expected 'error' activity after crash, got: {kinds}"
    )
    complete_entries = [e for e in activity if e.get("kind") == "complete"
                        and str(e.get("ticket")) == "1"]
    assert complete_entries == [], (
        f"Ticket must NOT be completed for unknown dead pid, got: {complete_entries}"
    )


def test_spawn_agent_uses_valid_cwd(kanban, monkeypatch):
    """spawn_agent must launch with a valid working directory even when
    kanban_dir is given relatively. Regression for WinError 123 caused by
    cwd=os.path.dirname('.') == '' (an invalid directory)."""
    captured = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)

    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    profile = {"name": "general", "systemPrompt": "p", "model": "m"}

    # Pass a RELATIVE kanban dir — this is what triggered the empty-string cwd.
    rel = os.path.relpath(kanban)
    marker = orch.spawn_agent(rel, "demo", task, profile, "m")

    assert marker["state"] == "dispatched"
    # cwd must be a real, existing directory (never "" or None).
    assert captured["cwd"], "cwd must not be empty/None"
    assert os.path.isdir(captured["cwd"]), f"cwd must exist: {captured['cwd']!r}"
    # First arg is the resolved claude executable, then -p.
    assert captured["cmd"][1] == "-p"


def test_tick_adopted_dead_with_comment_completes(kanban, monkeypatch):
    """After restart (_PROCS empty), a dead agent that left a Claude comment is
    reaped to 'completed', not 'blocked'."""
    pid = 54321
    orch._PROCS.pop(pid, None)
    p = _set_dispatched(kanban, "1", pid)
    t = _read(p)
    t.setdefault("comments", []).append({"writer": "Claude", "message": "did it"})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert _read(p)["status"] == "completed"
    kinds = [e.get("kind") for e in oc.read_activity(kanban)]
    assert "complete" in kinds


def test_tick_adopted_dead_with_question_needs_human(kanban, monkeypatch):
    pid = 54322
    orch._PROCS.pop(pid, None)
    p = _set_dispatched(kanban, "1", pid)
    t = _read(p)
    t["orchestrator"]["question"] = {"prompt": "which env?"}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "blocked"
    assert t1["orchestrator"]["question"]["prompt"] == "which env?"
    kinds = [e.get("kind") for e in oc.read_activity(kanban)]
    assert "needs_human" in kinds


# --- T13: interpret-progress-before-kill (must be RED against current code) ---

def _write_log(kanban, ticket_id, text):
    """Write the agent run-log that the ticket's marker logFile points at."""
    p = os.path.join(kanban, "demo", f"{ticket_id}.json")
    t = _read(p)
    log_rel = t["orchestrator"]["logFile"]  # e.g. .kanban/_orchestrator/runs/fake.log
    # logFile is repo-relative (.kanban/...); the loop reads it as kanban_dir/../<logFile>.
    log_abs = os.path.join(kanban, "..", log_rel)
    os.makedirs(os.path.dirname(log_abs), exist_ok=True)
    with open(log_abs, "w", encoding="utf-8") as f:
        f.write(text)
    return log_abs


def test_kill_requested_interprets_progress_before_kill(kanban, monkeypatch):
    """A kill-requested agent must get its progress interpreted into a checkpoint
    comment (writer 'Orchestrator') BEFORE the process is killed.

    RED against current code: the kill_requested path writes only the static
    'Killed by request.' comment and never calls a progress summarizer."""
    pid = 5151
    p = _set_dispatched(kanban, "1", pid)
    # Mark a queued kill so reap_decision routes to kill_requested.
    t = _read(p)
    t["orchestrator"]["killRequested"] = True
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    _write_log(kanban, "1", "AGENT LOG\nrefactored module X\nrunning tests...\n")
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: True)

    order = []
    monkeypatch.setattr(orch, "kill_pid",
                        lambda _pid: order.append(("kill", _pid)) or True)

    seen = {}

    def fake_summarize(kanban_dir, task, reason):
        order.append(("summarize", task["id"]))
        seen["reason"] = reason
        seen["id"] = task["id"]
        return "CHECKPOINT: refactored X; NEXT: finish tests."

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []},
              summarize_progress=fake_summarize)

    # Summary must have been produced for THIS ticket, before the kill.
    assert seen.get("id") == "1"
    assert order.index(("summarize", "1")) < order.index(("kill", pid)), (
        f"Summary must be written before the kill, got order={order}"
    )

    t1 = _read(p)
    msgs = [c["message"] for c in t1.get("comments", [])]
    assert any("CHECKPOINT: refactored X" in m for m in msgs), (
        f"Interpreted checkpoint summary must be on the ticket, got {msgs}"
    )
    assert t1["status"] == "blocked"


def test_stalled_interprets_progress_before_kill(kanban, monkeypatch):
    """A stalled agent that gets reaped must also get its progress interpreted
    into a checkpoint comment BEFORE the kill — not just the static
    'Reaped: no progress (stalled).' note."""
    pid = 5252
    p = _set_dispatched(kanban, "1", pid, dispatched_at="2020-01-01T00:00:00+00:00")
    logp = _write_log(kanban, "1", "AGENT LOG\nstuck waiting on a prompt\n")
    # Mark the log as already-seen at its current size with a stale lastGrowthAt,
    # so this tick observes NO growth and the idle clock has long expired.
    orch.write_idle(kanban, "demo", "1",
                    {"logSize": os.path.getsize(logp),
                     "lastGrowthAt": "2020-01-01T00:00:00+00:00"})
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False, "idleSeconds": 600})

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: True)

    order = []
    monkeypatch.setattr(orch, "kill_pid",
                        lambda _pid: order.append(("kill", _pid)) or True)
    monkeypatch.setattr(orch, "_summarize_progress",
                        lambda kd, task, reason:
                            order.append(("summarize", task["id"]))
                            or "CHECKPOINT: stalled mid-prompt; NEXT: answer prompt.")

    # No summarize_progress arg → must fall back to the module default.
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert order.index(("summarize", "1")) < order.index(("kill", pid)), (
        f"Summary must be written before the kill, got order={order}"
    )
    t1 = _read(p)
    msgs = [c["message"] for c in t1.get("comments", [])]
    assert any("CHECKPOINT: stalled mid-prompt" in m for m in msgs), (
        f"Interpreted checkpoint summary must be on the ticket, got {msgs}"
    )
    assert t1["status"] == "blocked"


def test_summarize_progress_interprets_log_and_ticket(kanban, monkeypatch):
    """_summarize_progress feeds the agent log tail + ticket to the model and
    returns its interpreted summary; on model failure it falls back to a
    non-empty string (never blocks the kill)."""
    pid = 5353
    p = _set_dispatched(kanban, "1", pid)
    _write_log(kanban, "1", "line A\nline B\nIMPLEMENTED feature Z\n")

    captured = {}

    class FakeOut:
        stdout = "CHECKPOINT: implemented Z. NEXT: write docs."

    def fake_run_tracked(cmd, label, **kwargs):
        captured["cmd"] = cmd
        return FakeOut()

    monkeypatch.setattr(orch, "_run_tracked", fake_run_tracked)

    t = _read(p)
    t["_path"] = p
    out = orch._summarize_progress(kanban, t, "kill")
    # The model was asked to interpret, and the prompt carried the log content.
    prompt = captured["cmd"][2]
    assert "IMPLEMENTED feature Z" in prompt, "log tail must be in the prompt"
    assert out and "implemented Z" in out

    # On model failure, return a non-empty fallback (does not raise).
    def boom(cmd, label, **kwargs):
        raise orch.subprocess.SubprocessError("model down")

    monkeypatch.setattr(orch, "_run_tracked", boom)
    fallback = orch._summarize_progress(kanban, t, "kill")
    assert fallback, "must return a non-empty fallback when the model is unavailable"


def test_summarize_progress_empty_log_still_records_ticket_context(kanban, monkeypatch):
    """When a stalled/hung agent left an EMPTY run-log (the real-world case the
    human reported: '0-byte logs, no record of what the agent was doing'), the
    summary must still be useful:

      1. The model prompt must carry the ticket's own progress signal — its prior
         comments — not just the (empty) log tail, since that's the only surviving
         record of what the agent did.
      2. The model-unavailable fallback must NOT be a bare '(no log output)'; it
         must name the empty-log situation so a human knows WHY there's no detail.
    """
    pid = 5454
    p = _set_dispatched(kanban, "1", pid)
    # The agent left a real progress note as a comment, but its stdout log is empty
    # (it hung before flushing anything) — exactly the reported failure mode.
    t = _read(p)
    t.setdefault("comments", []).append(
        {"writer": "Claude", "message": "Started refactor of module Q; tests not yet run."})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    _write_log(kanban, "1", "")  # 0-byte log

    captured = {}

    class FakeOut:
        stdout = "CHECKPOINT: refactor of Q started. NEXT: run tests."

    def fake_run_tracked(cmd, label, **kwargs):
        captured["cmd"] = cmd
        return FakeOut()

    monkeypatch.setattr(orch, "_run_tracked", fake_run_tracked)

    t2 = _read(p)
    t2["_path"] = p
    out = orch._summarize_progress(kanban, t2, "stalled")

    prompt = captured["cmd"][2]
    assert "Started refactor of module Q" in prompt, (
        "the agent's prior comments are the only surviving progress signal when "
        "the log is empty — they must be in the model prompt"
    )
    assert out and "refactor of Q" in out

    # Model unavailable → fallback must be a non-empty string directing to the run log.
    def boom(cmd, label, **kwargs):
        raise orch.subprocess.SubprocessError("model down")

    monkeypatch.setattr(orch, "_run_tracked", boom)
    fb = orch._summarize_progress(kanban, t2, "stalled")
    assert fb, f"empty-log fallback must be non-empty, got: {fb!r}"
    assert "run log" in fb.lower() or "log" in fb.lower(), (
        f"fallback should direct to the run log for details, got: {fb!r}"
    )


# --- T26: record the sub-agent session id so a human can `claude --resume` it ---

def test_spawn_agent_sets_resumable_session_id(kanban, monkeypatch):
    """spawn_agent must mint a session id, pass it to the CLI via --session-id,
    and return it on the marker so the ticket can record a resumable id.

    RED against current code: spawn_agent neither passes --session-id nor returns
    a sessionId, so a blocked ticket has no way to be manually taken over."""
    captured = {}

    class FakeProc:
        pid = 7777

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)

    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    profile = {"name": "general", "systemPrompt": "p", "model": "m"}

    marker = orch.spawn_agent(kanban, "demo", task, profile, "m")

    sid = marker.get("sessionId")
    assert sid, "marker must carry a sessionId"
    # The same id must be handed to the CLI so `claude --resume <sid>` reattaches.
    cmd = captured["cmd"]
    assert "--session-id" in cmd, f"--session-id must be passed, got {cmd}"
    assert cmd[cmd.index("--session-id") + 1] == sid, (
        "the id passed to the CLI must match the one recorded on the marker"
    )


def test_tick_dispatch_records_claude_session_id(kanban, monkeypatch):
    """On dispatch, the marker's sessionId must be promoted to the ticket's
    top-level `claudeSessionId` — the field the board UI reads to build the
    'claude --resume <id>' takeover command.

    RED against current code: tick writes the marker but never sets
    claudeSessionId, so the UI's resume button has nothing to copy."""
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3, "stopAllRequested": False})

    def fake_spawn(kanban_dir, board, task, profile, model):
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 4242, "dispatchedAt": oc.now_iso(), "killRequested": False,
                "sessionId": "abc-123-session", "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    orch.tick(kanban, opus_triage=lambda prompt, elig, profs, free: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1.get("claudeSessionId") == "abc-123-session", (
        f"dispatch must record claudeSessionId, got {t1.get('claudeSessionId')!r}"
    )


# --- Ticket #54: resume command must include the correct working directory ---

def test_spawn_agent_returns_cwd_in_marker(kanban, monkeypatch):
    """spawn_agent must include 'cwd' in the marker it returns so the board UI
    can build 'cd '<dir>'; claude --resume <id>' rather than bare '--resume <id>'.

    Without cd the resume session runs in whatever directory the terminal happens
    to be in, which is almost never the workspace root the agent needs."""
    captured = {}

    class FakeProc:
        pid = 8888

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cwd"] = cwd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)

    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    profile = {"name": "general", "systemPrompt": "p", "model": "m"}

    marker = orch.spawn_agent(kanban, "demo", task, profile, "m")

    assert "cwd" in marker, "marker must carry the 'cwd' the agent ran in"
    assert marker["cwd"] == captured["cwd"], (
        "marker['cwd'] must match the actual cwd passed to Popen"
    )


def test_tick_dispatch_records_claude_session_dir(kanban, monkeypatch):
    """On dispatch, the marker's 'cwd' must be promoted to the ticket's top-level
    'claudeSessionDir' so the UI can build a directory-aware resume command."""
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3, "stopAllRequested": False})

    def fake_spawn(kanban_dir, board, task, profile, model):
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 4242, "dispatchedAt": oc.now_iso(), "killRequested": False,
                "sessionId": "abc-123-session", "cwd": "/workspace/root",
                "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    orch.tick(kanban, opus_triage=lambda prompt, elig, profs, free: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1.get("claudeSessionDir") == "/workspace/root", (
        f"dispatch must record claudeSessionDir, got {t1.get('claudeSessionDir')!r}"
    )


# --- #27: agent prompt must teach the human-input / block escalation path ---

def test_agent_prompt_teaches_question_schema():
    """The dispatch prompt must tell a sub-agent HOW to escalate to a human,
    not just that it can. To date no agent has used the mechanism because the
    prompt only said 'write an orchestrator.question object' without the shape.

    The prompt must spell out: when to block, that it sets status 'blocked',
    and the orchestrator.question JSON shape the dashboard + reap path consume
    (id, type input|choice, prompt, answer:null, and the choice options field).

    RED against current code: _build_agent_prompt's escalation line names the
    object but never describes its fields or the input/choice types."""
    task = {"id": "9", "title": "Do a thing", "detail": "details here",
            "_path": ".kanban/demo/9.json"}
    profile = {"name": "backend", "systemPrompt": "You are a backend dev."}
    prompt = orch._build_agent_prompt(task, profile)

    low = prompt.lower()
    # It must still tell the agent to block + write a question.
    assert "blocked" in low
    assert "orchestrator.question" in low
    # It must describe the question SHAPE the dashboard/reap path require, so
    # the agent produces a renderable, detectable question rather than guessing.
    assert '"type"' in prompt and "input" in low and "choice" in low
    assert '"prompt"' in prompt
    assert '"answer"' in prompt and "null" in low
    assert '"options"' in prompt  # the choice variant's field
    # It must tell the agent WHEN to escalate (blocked / can't proceed), not
    # just the mechanics.
    assert "block" in low and ("cannot" in low or "can't" in low
                               or "unable" in low or "need" in low)


# --- streaming output + idle tracking + concurrency backfill ---

def test_spawn_agent_uses_stream_json(kanban, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 4343
        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")
    cmd = captured["cmd"]
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--verbose" in cmd


def test_tick_busy_agent_not_reaped(kanban, monkeypatch):
    """A log that grew since last tick keeps the agent running (idle clock reset)."""
    pid = 6001
    p = _set_dispatched(kanban, "1", pid)
    t = _read(p)
    t["orchestrator"]["dispatchedAt"] = "2020-01-01T00:00:00+00:00"
    t["orchestrator"]["logSize"] = 0
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    logp = os.path.join(kanban, "_orchestrator", "runs", "fake.log")
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    with open(logp, "w", encoding="utf-8") as f:
        f.write("some streamed output\n")
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False, "idleSeconds": 600})
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: True)
    killed = []
    monkeypatch.setattr(orch, "kill_pid", lambda _pid: killed.append(_pid) or True)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert killed == [], "busy agent (growing log) must not be killed"
    assert _read(p)["status"] == "in_progress"


def test_tick_idle_agent_reaped(kanban, monkeypatch):
    """A flat log past idle_seconds is reaped stalled."""
    pid = 6002
    p = _set_dispatched(kanban, "1", pid)
    t = _read(p)
    t["orchestrator"]["dispatchedAt"] = "2020-01-01T00:00:00+00:00"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    orch.write_idle(kanban, "demo", "1",
                    {"logSize": 50, "lastGrowthAt": "2020-01-01T00:00:00+00:00"})
    logp = os.path.join(kanban, "_orchestrator", "runs", "fake.log")
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    with open(logp, "wb") as f:
        f.write(b"x" * 50)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False, "idleSeconds": 600})
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(orch, "kill_pid", lambda _pid: True)
    monkeypatch.setattr(orch, "_summarize_progress", lambda *a, **k: "checkpoint")

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    assert _read(p)["status"] == "blocked"


# --- #29: on completion, push the agent's output to a branch + record it ---

def test_completed_pushes_output_branch_and_records_it(kanban, monkeypatch):
    """When a dispatched agent completes, the orchestrator must publish its output
    to the ticket's `ticket/<id>-<slug>` branch and record that branch on the
    ticket — both as a top-level `outputBranch` field and in a comment — so a
    human can find where the ticket's output lives.

    RED against current code: the completed reap path marks the ticket completed
    but never publishes a branch nor records one."""
    pid = 8100
    p = _set_dispatched(kanban, "1", pid)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})

    # Make the agent look like one WE spawned that exited cleanly (non-adopted,
    # exit 0 → 'completed'), so the completion path runs.
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 0)
    monkeypatch.setitem(orch._PROCS, pid, object())

    calls = []

    def fake_publish(kanban_dir, task, branch, board_meta=None, push=True):
        calls.append((str(task["id"]), branch))
        return {"branch": branch, "pushed": True, "detail": "pushed to origin"}

    monkeypatch.setattr(orch, "publish_output_branch", fake_publish)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    # The publish seam was invoked with the ticket-convention branch name.
    assert calls and calls[0][0] == "1"
    assert calls[0][1].startswith("ticket/1-")
    # The branch is recorded on the ticket itself.
    assert t1.get("outputBranch", "").startswith("ticket/1-"), (
        f"outputBranch must be recorded on the ticket, got {t1.get('outputBranch')!r}"
    )
    # ...and surfaced in a comment so it's visible in the board UI.
    msgs = [c["message"] for c in t1.get("comments", [])]
    assert any("ticket/1-" in m for m in msgs), (
        f"branch must be noted in a comment, got {msgs}"
    )


def test_completed_branch_publish_failure_still_completes(kanban, monkeypatch):
    """Publishing is best-effort: if the push fails (no remote, no git, nothing to
    commit), the ticket must STILL be marked completed and the failure recorded in
    a comment — a publish error must never wedge completion or crash the tick."""
    pid = 8101
    p = _set_dispatched(kanban, "1", pid)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 0)
    monkeypatch.setitem(orch._PROCS, pid, object())

    def failing_publish(kanban_dir, task, branch, board_meta=None, push=True):
        return {"branch": branch, "pushed": False, "detail": "no remote configured"}

    monkeypatch.setattr(orch, "publish_output_branch", failing_publish)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed", "completion must not depend on a successful push"
    msgs = " ".join(c["message"] for c in t1.get("comments", []))
    assert "no remote configured" in msgs, (
        f"the publish failure detail must be recorded for the human, got {msgs!r}"
    )


# --- #48: a completed ticket must be terminal — never re-reaped/re-completed ---

def test_completed_ticket_with_stale_marker_not_re_reaped(kanban, monkeypatch):
    """A ticket already `completed` whose `dispatched` marker reappeared (a
    re-dispatch/adoption write race re-adds it) must NOT be reaped again. The
    adopted-agent path would otherwise see its Claude comment, classify it as
    `progress` -> `completed`, and re-run `_finish_completion` (and its
    `git checkout`) on EVERY tick (ticket #48).

    The stale marker on a completed ticket is cleared, completion is NOT redone:
    no publish, no duplicate `complete` activity entry.

    RED against current code: the reap loop keys only off the marker state and
    ignores that the ticket is already completed."""
    pid = 4800
    orch._PROCS.pop(pid, None)  # dead PID we never held -> adopted
    p = _set_dispatched(kanban, "1", pid)
    t = _read(p)
    t["status"] = "completed"  # already done; marker is stale
    t.setdefault("comments", []).append({"writer": "Claude", "message": "did it"})
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3, "stopAllRequested": False})
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)

    published = []
    monkeypatch.setattr(orch, "_finish_completion",
                        lambda kd, task: published.append(str(task["id"])))

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    # The stale marker must be cleared so it can't be re-reaped next tick.
    assert "orchestrator" not in t1 or t1["orchestrator"].get("state") != "dispatched", (
        f"stale dispatched marker must be cleared, got {t1.get('orchestrator')!r}"
    )
    # Completion must NOT be redone (no publish/checkout, no duplicate activity).
    assert published == [], "a completed ticket must not be re-completed/re-published"
    completes = [e for e in oc.read_activity(kanban) if e.get("kind") == "complete"]
    assert completes == [], f"a completed ticket must not log a new 'complete', got {completes}"


def test_publish_output_branch_is_best_effort_without_git(kanban, monkeypatch):
    """publish_output_branch must never raise. In a non-git tree (the real current
    state of this workspace) it returns pushed=False with an explanatory detail
    rather than throwing."""
    # Simulate every git invocation failing (not a repo / git absent).
    def boom(cmd, **kwargs):
        raise orch.subprocess.CalledProcessError(128, cmd)

    monkeypatch.setattr(orch, "_run_git", boom)

    task = {"id": "1", "title": "First", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    result = orch.publish_output_branch(kanban, task, "ticket/1-first")
    assert result["pushed"] is False
    assert result["detail"]  # non-empty explanation


# --- #34: ticket branches must be cut from the default branch, not current HEAD ---

def test_publish_output_branch_bases_branch_on_default_branch(kanban, monkeypatch):
    """The ticket branch must be created off the repo's DEFAULT branch (e.g. main),
    not whatever the orchestrator happens to have checked out. Otherwise every
    ticket inherits the unrelated work of the previous ticket's branch.

    RED against current code: it runs `checkout -B <branch>` with no start-point,
    so the branch is cut from current HEAD."""
    calls = []

    def fake_git(cmd, **kwargs):
        calls.append(cmd)
        # Resolve the default branch via origin/HEAD.
        if cmd[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)

    task = {"id": "1", "title": "First", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    result = orch.publish_output_branch(kanban, task, "ticket/1-first")

    assert result["pushed"] is True
    # The branch must be created with the default branch as its start-point.
    checkout = next((c for c in calls if c[:2] == ["checkout", "-B"]), None)
    assert checkout is not None, f"expected a `checkout -B`, got {calls}"
    assert checkout[2] == "ticket/1-first"
    assert "origin/main" in checkout, (
        f"branch must be cut from the default branch (origin/main), got {checkout}"
    )


def test_publish_output_branch_falls_back_when_no_origin_head(kanban, monkeypatch):
    """If `origin/HEAD` is not set (no remote tracking), publishing must still cut
    the branch off a sensible default (local main/master) rather than crashing —
    and must never raise."""
    def fake_git(cmd, **kwargs):
        if cmd[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
            raise orch.subprocess.CalledProcessError(128, cmd)
        if cmd[:1] == ["rev-parse"]:
            # `main` exists, `master` does not.
            if cmd[-1] == "refs/heads/main" or cmd[-1] == "main":
                return "abc123"
            raise orch.subprocess.CalledProcessError(128, cmd)
        return ""

    monkeypatch.setattr(orch, "_run_git", fake_git)

    task = {"id": "1", "title": "First", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    result = orch.publish_output_branch(kanban, task, "ticket/1-first")
    # Whatever base it picks, it must complete (best-effort) without raising.
    assert result["pushed"] is True


def test_tick_backfills_to_cap(kanban, monkeypatch):
    """Triage names one ticket but two slots are free -> backfill dispatches both."""
    for tid in ("1", "2"):
        pp = os.path.join(kanban, "demo", f"{tid}.json")
        tt = _read(pp)
        tt["status"] = "ready"
        tt.pop("dependsOn", None)
        with open(pp, "w", encoding="utf-8") as f:
            json.dump(tt, f)
    oc.write_profile(kanban, {"name": "g", "whenToUse": "x", "model": "m"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "stopAllRequested": False, "idleSeconds": 600})

    seen_free = []

    def fake_triage(prompt, eligible, profiles, free):
        seen_free.append(free)
        return {"dispatch": [{"ticket": "1", "profile": "g", "model": "m",
                              "reason": "r"}]}

    dispatched = []

    def fake_spawn(kanban_dir, board, task, profile, model):
        dispatched.append(str(task["id"]))
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 7000 + int(task["id"]), "dispatchedAt": oc.now_iso(),
                "killRequested": False, "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    orch.tick(kanban, opus_triage=fake_triage)

    assert seen_free and seen_free[0] >= 2, "triage must receive free slot count"
    assert set(dispatched) == {"1", "2"}, f"backfill should add ticket 2: {dispatched}"


# --- #44: kill_pid must terminate the whole child process tree ---

def test_kill_pid_windows_kills_tree(monkeypatch):
    """On Windows, `claude -p` spawns node/MCP/tool children. kill_pid must pass
    /T to taskkill so the descendant tree is killed, not just the parent —
    otherwise the children are orphaned and keep burning CPU/RAM."""
    monkeypatch.setattr(orch.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd))

    assert orch.kill_pid(4242) is True
    assert calls, "taskkill should have been invoked"
    cmd = calls[0]
    assert "/T" in cmd, f"taskkill must include /T to kill the tree, got {cmd}"
    assert "/F" in cmd and "4242" in cmd


# --- #52: single-instance lock in run_loop ---

def test_run_loop_skips_tick_when_lock_held(kanban, monkeypatch):
    """A second loop must NOT tick while another live process holds the lock.

    RED against current code: run_loop never acquires the lock, so both the
    server's background loop and a manual `python orchestrator.py` tick the same
    board concurrently → double dispatch + cap breaches."""
    lock = oc._lock_path(kanban)
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "w", encoding="utf-8") as f:
        f.write("99999")  # some other process's pid
    # That owner is alive, so the lock cannot be reclaimed.
    monkeypatch.setattr(oc, "_pid_alive", lambda pid: True)

    ticks = []
    ev = threading.Event()
    monkeypatch.setattr(orch, "tick",
                        lambda *a, **k: (ticks.append(1), ev.set()))

    orch.run_loop(kanban, stop_event=ev, tick_seconds=0.01)

    assert ticks == [], "a loop without the lock must not tick"


def test_run_loop_acquires_lock_ticks_and_releases(kanban, monkeypatch):
    """When the lock is free, run_loop acquires it, ticks, and releases it on exit
    so the next process can take over."""
    ticks = []
    ev = threading.Event()

    def fake_tick(*a, **k):
        ticks.append(1)
        ev.set()  # stop after one tick

    monkeypatch.setattr(orch, "tick", fake_tick)

    orch.run_loop(kanban, stop_event=ev, tick_seconds=0.01)

    assert ticks == [1], "run_loop must tick when it holds the lock"
    assert not os.path.exists(oc._lock_path(kanban)), "lock must be released on exit"


# --- #52: dispatch keys by (board, id) so same-id tickets on two boards collide ---

def _add_board(kanban, board, ticket_id, **fields):
    bdir = os.path.join(kanban, board)
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"project": board}, f)
    t = {"id": str(ticket_id), "title": f"{board}-{ticket_id}", "status": "ready"}
    t.update(fields)
    p = os.path.join(bdir, f"{ticket_id}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    return p


def test_tick_dispatches_same_id_on_two_boards(kanban, monkeypatch):
    """Two boards each carry a ticket id "5"; both must be dispatchable. The old
    bare-id keying (by_id={str(t['id']):t}) collapsed them so only one could ever
    dispatch."""
    pa = _add_board(kanban, "alpha", "5")
    pb = _add_board(kanban, "beta", "5")
    oc.write_profile(kanban, {"name": "g", "whenToUse": "x", "model": "m"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "stopAllRequested": False, "idleSeconds": 600})

    dispatched = []

    def fake_spawn(kanban_dir, board, task, profile, model):
        dispatched.append((board, str(task["id"])))
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 9000 + len(dispatched), "dispatchedAt": oc.now_iso(),
                "killRequested": False, "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    # Triage names both, each disambiguated by board.
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": [
        {"ticket": "5", "board": "alpha", "profile": "g", "model": "m", "reason": "a"},
        {"ticket": "5", "board": "beta", "profile": "g", "model": "m", "reason": "b"},
    ]})

    assert {("alpha", "5"), ("beta", "5")} <= set(dispatched), (
        f"both same-id tickets must dispatch, got {dispatched}"
    )
    assert _read(pa)["status"] == "in_progress"
    assert _read(pb)["status"] == "in_progress"


def test_kill_pid_posix_kills_process_group(monkeypatch):
    """On POSIX, agents are spawned in their own session (start_new_session), so
    kill_pid must signal the whole process group (os.killpg), not just the pid."""
    monkeypatch.setattr(orch.sys, "platform", "linux")
    killpg_calls = []
    monkeypatch.setattr(orch.os, "killpg",
                        lambda pgid, sig: killpg_calls.append((pgid, sig)),
                        raising=False)
    monkeypatch.setattr(orch.os, "getpgid", lambda pid: pid, raising=False)

    assert orch.kill_pid(4242) is True
    assert killpg_calls == [(4242, 15)], (
        f"must killpg the group with SIGTERM, got {killpg_calls}"
    )


# --- #54: configurable triage + summarizer model ---

def test_default_state_has_triage_and_summarizer_model():
    """The loop's own brain models default to Opus but are now first-class state."""
    assert oc.DEFAULT_STATE.get("triageModel") == "claude-opus-4-8"
    assert oc.DEFAULT_STATE.get("summarizerModel") == "claude-opus-4-8"


def test_summarize_progress_uses_configured_model(kanban, monkeypatch):
    """_summarize_progress reads summarizerModel from state for the model call,
    falling back to Opus when unset."""
    pid = 6262
    p = _set_dispatched(kanban, "1", pid)
    _write_log(kanban, "1", "did stuff\n")

    captured = {}

    class FakeOut:
        stdout = "CHECKPOINT: x. NEXT: y."

    def fake_run_tracked(cmd, label, **kwargs):
        captured["cmd"] = cmd
        return FakeOut()

    monkeypatch.setattr(orch, "_run_tracked", fake_run_tracked)

    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "summarizerModel": "claude-haiku-4-5-20251001"})
    t = _read(p)
    t["_path"] = p
    orch._summarize_progress(kanban, t, "kill")
    assert "--model" in captured["cmd"]
    mi = captured["cmd"].index("--model")
    assert captured["cmd"][mi + 1] == "claude-haiku-4-5-20251001"


def test_summarize_progress_falls_back_to_opus_when_unset(kanban, monkeypatch):
    pid = 6363
    p = _set_dispatched(kanban, "1", pid)
    _write_log(kanban, "1", "did stuff\n")
    captured = {}

    class FakeOut:
        stdout = "ok"

    monkeypatch.setattr(orch, "_run_tracked",
                        lambda cmd, label, **k: captured.update(cmd=cmd) or FakeOut())
    # state.json with no summarizerModel key
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3})
    t = _read(p)
    t["_path"] = p
    orch._summarize_progress(kanban, t, "kill")
    mi = captured["cmd"].index("--model")
    assert captured["cmd"][mi + 1] == "claude-opus-4-8"


def test_real_opus_triage_uses_given_model(monkeypatch):
    """_real_opus_triage parametrizes the model, defaulting to Opus."""
    captured = {}

    class FakeOut:
        stdout = '{"dispatch": []}'

    monkeypatch.setattr(orch, "_run_tracked",
                        lambda cmd, label, **k: captured.update(cmd=cmd) or FakeOut())
    orch._real_opus_triage("p", [], [], 1, model="claude-haiku-4-5-20251001")
    mi = captured["cmd"].index("--model")
    assert captured["cmd"][mi + 1] == "claude-haiku-4-5-20251001"

    orch._real_opus_triage("p", [], [], 1)
    mi = captured["cmd"].index("--model")
    assert captured["cmd"][mi + 1] == "claude-opus-4-8"


def test_run_loop_default_triage_reads_triage_model(kanban, monkeypatch):
    """The default triage callable built by run_loop passes state.triageModel to
    _real_opus_triage on each tick."""
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "triageModel": "claude-sonnet-4-6"})
    captured = {}

    def fake_real(prompt, eligible, profiles, free, model=None, timeout=120):
        captured["model"] = model
        return {"dispatch": []}

    monkeypatch.setattr(orch, "_real_opus_triage", fake_real)

    def fake_tick(kanban_dir, *, opus_triage, summarize_progress=None):
        opus_triage("prompt", [], [], 1)
        ev.set()

    ev = threading.Event()
    monkeypatch.setattr(orch, "tick", fake_tick)
    orch.run_loop(kanban, stop_event=ev, tick_seconds=0.01)
    assert captured["model"] == "claude-sonnet-4-6"


# --- Ticket #60: usage limits — graceful pause + auto-resume ---

def test_tick_usage_limit_requeues_ticket_and_pauses(kanban, monkeypatch):
    """A dispatched agent that died on a usage limit must NOT be blocked as a
    crash. The ticket is re-queued to `ready` and a usage-limit pause is recorded
    so the orchestrator resumes automatically once the limit resets."""
    pid = 5151
    reset = int(time.time()) + 1800  # a real reset is in the future
    p = _set_dispatched(kanban, "1", pid)
    _write_log(kanban, "1", f"Claude AI usage limit reached|{reset}")
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 1)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "ready", (
        f"usage-limited ticket should be re-queued to 'ready', got '{t1['status']}'"
    )
    assert "orchestrator" not in t1 or t1["orchestrator"].get("state") != "dispatched"

    pause = oc.read_usage_pause(kanban)
    assert pause.get("pausedUntil") == reset

    kinds = [e.get("kind") for e in oc.read_activity(kanban)]
    assert "usage_limit" in kinds, f"expected a 'usage_limit' activity, got {kinds}"
    assert "error" not in kinds, "usage limit must not be logged as a crash 'error'"


def test_tick_paused_skips_dispatch(kanban, monkeypatch):
    """While a usage-limit pause is active, no new tickets are dispatched even
    though the board is enabled and has eligible work."""
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "stopAllRequested": False})
    # Park dispatch far into the future.
    oc.set_usage_pause(kanban, reset_at=time.time() + 3600, now_ts=time.time())

    calls = []
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **k: calls.append(a) or {"state": "dispatched"})
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})

    assert calls == [], "no dispatch should happen while usage-limit paused"
    # Pause still present (not yet expired).
    assert oc.is_usage_paused(kanban, now_ts=time.time()) is True


def test_tick_resumes_after_pause_expires(kanban, monkeypatch):
    """Once the reset time has passed the pause is cleared, a 'usage_resume'
    activity is logged, and dispatch proceeds — the orchestrator self-restarts."""
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "stopAllRequested": False})
    # A pause whose reset time is already in the past.
    oc.set_usage_pause(kanban, reset_at=time.time() - 10, now_ts=time.time() - 20)

    def fake_spawn(kanban_dir, board, task, profile, model):
        return {"state": "dispatched", "profile": profile, "model": model,
                "pid": 4243, "dispatchedAt": oc.now_iso(), "killRequested": False,
                "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    orch.tick(kanban, opus_triage=lambda prompt, elig, profs, free: {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "m", "reason": "x"}]})

    assert _read(os.path.join(kanban, "demo", "1.json"))["status"] == "in_progress"
    assert oc.read_usage_pause(kanban) == {}, "expired pause should be cleared"
    kinds = [e.get("kind") for e in oc.read_activity(kanban)]
    assert "usage_resume" in kinds, f"expected a 'usage_resume' activity, got {kinds}"


# --- Ticket #12: usage limit hit by the dispatch-triage call itself ---

def test_tick_triage_usage_limit_pauses_dispatch(kanban, monkeypatch):
    """The FIRST claude call each tick is the dispatch-triage call. If IT hits a
    usage limit (before any ticket agent is spawned), the tick must park dispatch
    and flip the top status to "usage limited" — not silently return and keep
    hammering the limit every tick with the pill still showing "live"."""
    reset = int(time.time()) + 1800
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "ui"})
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "stopAllRequested": False})

    calls = []
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **k: calls.append(a) or {"state": "dispatched"})
    # Triage signals it hit a usage limit instead of returning a dispatch plan.
    orch.tick(kanban, opus_triage=lambda *a, **k: {"usageLimit": {"resetAt": reset}})

    assert calls == [], "no dispatch should happen when triage itself is rate-limited"
    assert oc.is_usage_paused(kanban, now_ts=time.time()) is True
    assert oc.read_usage_pause(kanban).get("pausedUntil") == reset
    kinds = [e.get("kind") for e in oc.read_activity(kanban)]
    assert "usage_limit" in kinds, f"expected a 'usage_limit' activity, got {kinds}"


def test_real_opus_triage_detects_usage_limit(monkeypatch):
    """When the triage CLI exits on a usage limit (no JSON, just the limit line),
    _real_opus_triage surfaces a {'usageLimit': ...} signal with the reset epoch
    parsed from the output — rather than swallowing it as an empty dispatch."""
    reset = int(time.time()) + 1800

    class FakeOut:
        stdout = f"Claude AI usage limit reached|{reset}"
        stderr = ""

    monkeypatch.setattr(orch, "_run_tracked", lambda cmd, label, **k: FakeOut())
    out = orch._real_opus_triage("p", [], [], 1)
    assert out.get("usageLimit") == {"resetAt": reset}
    assert "dispatch" not in out


# --- Ticket #42: initial triage on TODO -> Ready promotion ---

def test_tick_promotion_calls_initial_triage(kanban, monkeypatch):
    """When a ticket is promoted from todo to ready, the tick loop must call the
    initial_triage callable so Sonnet can set dependsOn and model on the ticket
    before it enters the Ready queue.

    RED against current code: the promotion loop has no initial_triage hook."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    triage_calls = []

    def fake_initial_triage(kanban_dir, task, all_tasks):
        triage_calls.append(str(task["id"]))
        return {"dependsOn": [], "model": "claude-sonnet-4-6"}

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []},
              initial_triage=fake_initial_triage)

    # Ticket 1 (no deps) was promoted — triage must have been called.
    assert "1" in triage_calls, (
        f"initial_triage must be called for ticket 1 on promotion, got {triage_calls}"
    )


def test_tick_promotion_writes_triage_result_to_ticket(kanban, monkeypatch):
    """The result of initial_triage (dependsOn + model) must be written to the
    ticket file before it is marked ready.

    RED against current code: the promotion loop never calls initial_triage and
    never writes model/dependsOn to the ticket."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    def fake_initial_triage(kanban_dir, task, all_tasks):
        return {"dependsOn": [], "model": "claude-haiku-4-5-20251001"}

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []},
              initial_triage=fake_initial_triage)

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1.get("model") == "claude-haiku-4-5-20251001", (
        f"model from triage must be written to the ticket, got {t1.get('model')!r}"
    )


def test_tick_promotion_triage_sets_depends_on(kanban, monkeypatch):
    """If initial_triage returns a non-empty dependsOn list, it is written to
    the ticket before promotion so future dep-checks see the correct graph.

    RED against current code: no triage hook exists."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    def fake_initial_triage(kanban_dir, task, all_tasks):
        return {"dependsOn": ["99"], "model": "claude-sonnet-4-6"}

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []},
              initial_triage=fake_initial_triage)

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1.get("dependsOn") == ["99"], (
        f"dependsOn from triage must be written to the ticket, got {t1.get('dependsOn')!r}"
    )


def test_tick_promotion_triage_failure_still_promotes(kanban, monkeypatch):
    """A failing initial_triage must not block promotion — the ticket should still
    move to ready even if Sonnet is unavailable.

    RED against current code: no hook, so no failure path either."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    def boom_triage(kanban_dir, task, all_tasks):
        raise RuntimeError("Sonnet unavailable")

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []},
              initial_triage=boom_triage)

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1["status"] == "ready", (
        f"triage failure must not block promotion to ready, got {t1['status']!r}"
    )


def test_tick_promotion_no_initial_triage_arg_still_works(kanban, monkeypatch):
    """When initial_triage is not supplied, the tick uses the real Sonnet triage
    (or a default no-op); promotion still happens normally.

    This test verifies backwards-compatibility: existing callers that pass only
    opus_triage are unaffected."""
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    # Patch the real Sonnet triage so no subprocess is spawned.
    monkeypatch.setattr(orch, "_real_sonnet_triage", lambda *a, **k: {})

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(os.path.join(kanban, "demo", "1.json"))
    assert t1["status"] == "ready", (
        f"promotion must still work when initial_triage is not supplied, "
        f"got {t1['status']!r}"
    )


def test_real_sonnet_triage_calls_sonnet_with_ticket_context(kanban, monkeypatch):
    """_real_sonnet_triage must call the claude CLI with the Sonnet model and
    include the ticket's title/detail in the prompt so it has context to classify.

    RED against current code: _real_sonnet_triage does not exist."""
    captured = {}

    class FakeOut:
        stdout = '{"dependsOn": [], "model": "claude-sonnet-4-6"}'

    def fake_run_tracked(cmd, label, **kwargs):
        captured["cmd"] = cmd
        return FakeOut()

    monkeypatch.setattr(orch, "_run_tracked", fake_run_tracked)

    task = {"id": "1", "title": "Build login page", "detail": "OAuth flow",
            "_board": "demo", "_path": os.path.join(kanban, "demo", "1.json")}
    all_tasks = [task]

    result = orch._real_sonnet_triage(kanban, task, all_tasks)

    assert captured.get("cmd"), "claude CLI must have been called"
    cmd = captured["cmd"]
    assert "--model" in cmd
    model_idx = cmd.index("--model")
    assert "sonnet" in cmd[model_idx + 1].lower(), (
        f"initial triage must use Sonnet, got {cmd[model_idx + 1]!r}"
    )
    # The prompt must carry the ticket's title and/or detail.
    prompt_arg = cmd[2]  # claude -p <prompt>
    assert "Build login page" in prompt_arg or "OAuth flow" in prompt_arg, (
        "ticket title/detail must be in the triage prompt"
    )
    assert result == {"dependsOn": [], "model": "claude-sonnet-4-6"}


def test_sonnet_triage_includes_fable_in_model_choices(kanban, monkeypatch):
    """_real_sonnet_triage must include claude-fable-5 in the list of model choices
    passed to the LLM so it can select fable for suitable tickets."""
    captured = {}

    class FakeOut:
        stdout = '{"dependsOn": [], "model": "claude-fable-5"}'

    def fake_run_tracked(cmd, label, **kwargs):
        captured["cmd"] = cmd
        return FakeOut()

    monkeypatch.setattr(orch, "_run_tracked", fake_run_tracked)

    task = {"id": "1", "title": "Complex creative task", "detail": "Needs Fable",
            "_board": "demo", "_path": os.path.join(kanban, "demo", "1.json")}
    result = orch._real_sonnet_triage(kanban, task, [task])

    prompt_arg = captured["cmd"][2]
    assert "fable" in prompt_arg.lower(), (
        "fable must appear in the triage prompt model choices"
    )
    assert result == {"dependsOn": [], "model": "claude-fable-5"}


def test_probe_fable_available_returns_true_on_success(monkeypatch):
    """_probe_fable_available returns True when the CLI exits 0."""
    class FakeResult:
        returncode = 0

    monkeypatch.setattr(orch, "_run_tracked",
                        lambda cmd, label, **kw: FakeResult())
    assert orch._probe_fable_available() is True


def test_probe_fable_available_returns_false_on_failure(monkeypatch):
    """_probe_fable_available returns False when the CLI exits non-zero."""
    import subprocess

    def fake_run(cmd, label, **kw):
        raise subprocess.SubprocessError("model not found")

    monkeypatch.setattr(orch, "_run_tracked", fake_run)
    assert orch._probe_fable_available() is False


def test_spawn_agent_uses_fable_when_available(kanban, monkeypatch):
    """When fable is available and the requested model is claude-fable-5,
    spawn_agent passes claude-fable-5 to the claude CLI --model flag."""
    monkeypatch.setattr(orch, "_probe_fable_available", lambda: True)
    launched = {}

    class FakeProc:
        pid = 9901
        _log_f = None
        def poll(self): return None

    def fake_popen(cmd, **kw):
        launched["cmd"] = cmd
        p = FakeProc()
        if kw.get("stdout"):
            kw["stdout"].write("")
        return p

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    oc.write_profile(kanban, {"name": "backend", "whenToUse": "x"})
    task = {"id": "1", "title": "T", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    profile = {"name": "backend", "systemPrompt": "x"}
    orch.spawn_agent(kanban, "demo", task, profile, "claude-fable-5")

    cmd = launched["cmd"]
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "claude-fable-5"


def test_spawn_agent_falls_back_to_opus_when_fable_unavailable(kanban, monkeypatch):
    """When fable is NOT available and the requested model is claude-fable-5,
    spawn_agent substitutes the fallback (opus) in the --model flag."""
    monkeypatch.setattr(orch, "_probe_fable_available", lambda: False)
    launched = {}

    class FakeProc:
        pid = 9902
        _log_f = None
        def poll(self): return None

    def fake_popen(cmd, **kw):
        launched["cmd"] = cmd
        p = FakeProc()
        if kw.get("stdout"):
            kw["stdout"].write("")
        return p

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    oc.write_profile(kanban, {"name": "backend", "whenToUse": "x"})
    task = {"id": "1", "title": "T", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    profile = {"name": "backend", "systemPrompt": "x"}
    orch.spawn_agent(kanban, "demo", task, profile, "claude-fable-5")

    cmd = launched["cmd"]
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == oc.FABLE_FALLBACK_MODEL, (
        f"expected opus fallback, got {cmd[idx + 1]!r}"
    )


# --- Ticket #58: save log turns to ticket on completion ---

def test_completed_ticket_has_completed_log(kanban, monkeypatch):
    """When a dispatched agent completes, the orchestrator must save the parsed
    log turns to the ticket as a `completedLog` field so the logs remain visible
    after the ticket is done (the live-log endpoint only works for in-progress
    tickets).

    RED against current code: the completion path never saves log data to the
    ticket, so `completedLog` is absent."""
    pid = 8200
    p = _set_dispatched(kanban, "1", pid)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    # Write a real run-log the completion path can read.
    log_text = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Working on the task."}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Done!"}
        ]}}),
    ])
    _write_log(kanban, "1", log_text)

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 0)
    monkeypatch.setitem(orch._PROCS, pid, object())
    # Stub out the git/branch side-effects so the test stays pure.
    monkeypatch.setattr(orch, "_finish_completion", lambda kd, task: None)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    assert "completedLog" in t1, (
        f"completed ticket must carry completedLog, got keys: {list(t1.keys())}"
    )
    log = t1["completedLog"]
    assert isinstance(log, list), f"completedLog must be a list, got {type(log)}"
    assert len(log) == 2, f"expected 2 turns, got {len(log)}"
    texts = [turn["text"] for turn in log]
    assert "Working on the task." in texts
    assert "Done!" in texts


def test_self_completed_ticket_saves_completed_log(kanban, monkeypatch):
    """An agent that moves its OWN ticket to `completed` before exiting (the
    normal flow — CLAUDE.md tells workers to do exactly this) hits the ticket
    #48 stale-marker guard, not the `action == "completed"` reap path. That
    guard must still save the run-log as `completedLog` before it clears the
    marker, or the log disappears from the UI for every well-behaved agent.

    RED against current code: the guard clears the marker without saving."""
    pid = 8300
    p = _set_dispatched(kanban, "1", pid)
    log_text = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Implementing the fix."}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "All done, marking completed."}
        ]}}),
    ])
    _write_log(kanban, "1", log_text)
    # The agent already moved the ticket to completed itself (marker still set).
    t = _read(p)
    t["status"] = "completed"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 0)
    monkeypatch.setitem(orch._PROCS, pid, object())
    monkeypatch.setattr(orch, "_finish_completion", lambda kd, task: None)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    assert "orchestrator" not in t1, "stale marker must still be cleared"
    log = t1.get("completedLog")
    assert isinstance(log, list) and len(log) == 2, (
        f"self-completed ticket must carry completedLog, got {log!r}"
    )
    texts = [turn["text"] for turn in log]
    assert "Implementing the fix." in texts
    assert "All done, marking completed." in texts


def test_completed_log_absent_when_no_log_file(kanban, monkeypatch):
    """When there is no run-log (e.g. ticket completed without a logFile on the
    marker), completedLog is simply omitted — the ticket must still complete."""
    pid = 8201
    p = _set_dispatched(kanban, "1", pid)
    # Remove logFile from the marker.
    t = _read(p)
    t["orchestrator"].pop("logFile", None)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 0)
    monkeypatch.setitem(orch._PROCS, pid, object())
    monkeypatch.setattr(orch, "_finish_completion", lambda kd, task: None)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    # No log file → completedLog should be absent (or empty list) — not an error.
    log = t1.get("completedLog")
    assert log is None or log == [], (
        f"completedLog should be absent/empty when there is no log file, got {log!r}"
    )


def test_task_log_uses_completed_log_when_done(monkeypatch, tmp_path):
    """When a ticket is completed and has a `completedLog` on it, the task_log
    endpoint must return those turns rather than trying to read the (now-stale or
    deleted) log file.

    RED against current code: task_log only checks the marker's logFile — it has
    no fallback to completedLog on a done ticket."""
    import kanban_server as ks
    kanban = tmp_path / ".kanban"
    board = kanban / "demo"
    runs = kanban / "_orchestrator" / "runs"
    board.mkdir(parents=True)
    runs.mkdir(parents=True)
    saved_turns = [
        {"seq": 0, "role": "assistant", "text": "I did the thing.", "tools": []},
        {"seq": 1, "role": "assistant", "text": "All done!", "tools": []},
    ]
    ticket = {
        "id": "1", "title": "T", "status": "completed",
        "orchestrator": {"state": "dispatched",
                         "logFile": ".kanban/_orchestrator/runs/gone.log"},
        "completedLog": saved_turns,
    }
    (board / "1.json").write_text(json.dumps(ticket), encoding="utf-8")
    monkeypatch.setattr(ks, "KANBAN_DIR", str(kanban))
    monkeypatch.setattr(ks, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(ks, "RUNS_DIR", str(runs))

    body, code = ks.task_log("demo", "1")
    assert code == 200
    assert body["running"] is False
    assert body["hasLog"] is True
    turns = body["turns"]
    assert len(turns) == 2, f"expected 2 turns from completedLog, got {turns}"
    assert turns[0]["text"] == "I did the thing."
    assert turns[1]["text"] == "All done!"


# --- Ticket #81: stop putting raw log tail on the ticket ---

def test_crash_comment_has_no_log_tail(kanban, monkeypatch):
    """When a dispatched agent crashes, the 'NEEDS HUMAN' comment must NOT include
    the raw log tail. Since logs are visible in the UI, appending raw log content
    to the ticket is redundant noise.

    RED against current code: the crash path appends `tail` to the comment."""
    pid = 8101
    p = _set_dispatched(kanban, "1", pid)
    _write_log(kanban, "1", "agent ran step A\nagent ran step B\nERROR: something went wrong\n")
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})

    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: 1)
    monkeypatch.setitem(orch._PROCS, pid, object())

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "blocked"
    msgs = " ".join(c["message"] for c in t1.get("comments", []))
    assert "NEEDS HUMAN" in msgs, "crash comment must still say NEEDS HUMAN"
    assert "agent ran step A" not in msgs, (
        "raw log tail must NOT be appended to the ticket comment — check the logs instead"
    )
    assert "ERROR: something went wrong" not in msgs, (
        "raw log tail must NOT be appended to the ticket comment"
    )


def test_summarize_progress_fallback_has_no_log_tail(kanban, monkeypatch):
    """When the summarizer model is unavailable, _summarize_progress's fallback
    must NOT include the raw log tail. Since logs are visible in the UI, embedding
    the tail in the comment is redundant.

    RED against current code: the fallback returns 'Last log tail before kill:\\n' + tail."""
    pid = 8102
    p = _set_dispatched(kanban, "1", pid)
    _write_log(kanban, "1", "line A\nline B\nstep X done\n")

    def boom(cmd, label, **kwargs):
        raise orch.subprocess.SubprocessError("model down")

    monkeypatch.setattr(orch, "_run_tracked", boom)

    t = _read(p)
    t["_path"] = p
    fallback = orch._summarize_progress(kanban, t, "kill")

    assert fallback, "fallback must still return a non-empty string"
    assert "line A" not in fallback, (
        "raw log tail must NOT be in the fallback — check the logs instead"
    )
    assert "step X done" not in fallback, (
        "raw log tail must NOT be in the fallback"
    )
