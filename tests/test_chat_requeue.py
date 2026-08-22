"""Reap-time handling of chat messages a run never received (bot messaging).

When a run ends with undelivered inbox messages (the pump's `.pos` sidecar
records how far delivery got), the reap path must surface them onto the ticket
(`pendingChat` + a human-visible comment) instead of losing them, re-queue a
would-be-completed ticket to `ready`, and the next dispatch must inject them
into the agent's prompt and consume the field.
"""
import json
import os

import orchestrator_core as oc
import orchestrator as orch


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _set_dispatched(kanban, ticket_id, pid):
    p = os.path.join(kanban, "boards", "demo", f"{ticket_id}.json")
    t = _read(p)
    t["status"] = "in_progress"
    t["orchestrator"] = {
        "state": "dispatched",
        "pid": pid,
        "killRequested": False,
        "dispatchedAt": oc.now_iso(),
        "logFile": ".kanban/_orchestrator/runs/fake.log",
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    return p


def _queue_pending(kanban, monkeypatch, board, ticket_id, messages):
    """Point CHAT_DIR at the temp tree and drop an undelivered inbox."""
    monkeypatch.setattr(oc, "CHAT_DIR",
                        os.path.join(kanban, "_orchestrator", "chat"))
    inbox = oc.chat_inbox_path(board, ticket_id)
    os.makedirs(os.path.dirname(inbox), exist_ok=True)
    with open(inbox, "a", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps({"message": m, "writer": "ryan",
                                "ts": oc.now_iso()}, ensure_ascii=False) + "\n")
    return inbox


def _stub_reap(monkeypatch, exit_code=0):
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(orch, "_exit_code", lambda _pid: exit_code)
    monkeypatch.setattr(orch, "_finish_completion", lambda kd, task: None)


def test_completed_run_with_pending_chat_requeues_to_ready(kanban, monkeypatch):
    pid = 9001
    p = _set_dispatched(kanban, "1", pid)
    inbox = _queue_pending(kanban, monkeypatch, "demo", "1",
                           ["also update the docs"])
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    _stub_reap(monkeypatch)
    monkeypatch.setitem(orch._PROCS, pid, object())

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "ready", \
        "a completed run with undelivered chat must re-queue, not finish"
    assert t1["pendingChat"] == [{"message": "also update the docs",
                                  "writer": "ryan",
                                  "ts": t1["pendingChat"][0]["ts"]}]
    # Human-visible record: the undelivered-message comment plus the re-queue note.
    msgs = [c["message"] for c in t1.get("comments", [])
            if c.get("writer") == "Orchestrator"]
    assert any("NOT delivered" in m for m in msgs)
    assert any("Re-queued to `ready`" in m for m in msgs)
    # Inbox consumed onto the ticket.
    assert not os.path.exists(inbox)
    assert not os.path.exists(oc.chat_offset_path(inbox))
    # Activity feed records the re-queue.
    acts = oc.read_activity(kanban)
    assert any(a.get("kind") == "chat_requeue" for a in acts)


def test_completed_run_without_pending_chat_completes(kanban, monkeypatch):
    pid = 9002
    p = _set_dispatched(kanban, "1", pid)
    monkeypatch.setattr(oc, "CHAT_DIR",
                        os.path.join(kanban, "_orchestrator", "chat"))
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    _stub_reap(monkeypatch)
    monkeypatch.setitem(orch._PROCS, pid, object())

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    assert "pendingChat" not in t1


def test_crashed_run_preserves_pending_chat_on_blocked_ticket(kanban, monkeypatch):
    pid = 9003
    p = _set_dispatched(kanban, "1", pid)
    inbox = _queue_pending(kanban, monkeypatch, "demo", "1",
                           ["first note", "second note"])
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    _stub_reap(monkeypatch, exit_code=1)
    monkeypatch.setitem(orch._PROCS, pid, object())

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "blocked"
    assert [m["message"] for m in t1["pendingChat"]] == ["first note",
                                                         "second note"]
    msgs = [c["message"] for c in t1.get("comments", [])
            if c.get("writer") == "Orchestrator"]
    assert any("NOT delivered" in m for m in msgs)
    assert not os.path.exists(inbox)


def test_self_completed_ticket_with_pending_chat_requeues(kanban, monkeypatch):
    """The usual completion path: the agent moved its OWN ticket to completed
    (ticket #48 stale-marker guard). With a dead process and undelivered chat,
    the guard must surface the messages and re-queue instead of quietly
    clearing the marker."""
    pid = 9004
    p = _set_dispatched(kanban, "1", pid)
    inbox = _queue_pending(kanban, monkeypatch, "demo", "1", ["one more thing"])
    t = _read(p)
    t["status"] = "completed"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    _stub_reap(monkeypatch)
    monkeypatch.setitem(orch._PROCS, pid, object())

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "ready"
    assert [m["message"] for m in t1["pendingChat"]] == ["one more thing"]
    assert "orchestrator" not in t1, "stale marker must still be cleared"
    assert not os.path.exists(inbox)


def test_self_completed_alive_process_defers_marker_clear(kanban, monkeypatch):
    """#48 guard, process still ALIVE with an undelivered inbox: the pump may
    still deliver, so the tick must NOT clear the marker yet — a marker-less
    ticket is never re-reaped, so the inbox would be orphaned (then wiped by
    the next dispatch) if the child died before delivery. Once the process is
    dead, the normal surface-and-requeue path takes over."""
    pid = 9007
    p = _set_dispatched(kanban, "1", pid)
    inbox = _queue_pending(kanban, monkeypatch, "demo", "1", ["late steer"])
    t = _read(p)
    t["status"] = "completed"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: True)
    monkeypatch.setitem(orch._PROCS, pid, object())

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    assert (t1.get("orchestrator") or {}).get("state") == "dispatched", \
        "marker must survive while the child may still receive the inbox"
    assert os.path.exists(inbox), "inbox must not be consumed while alive"
    assert "pendingChat" not in t1

    # Child dies without delivering: dead-process straggler path requeues.
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: False)
    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t2 = _read(p)
    assert t2["status"] == "ready"
    assert [m["message"] for m in t2["pendingChat"]] == ["late steer"]
    assert "orchestrator" not in t2
    assert not os.path.exists(inbox)


def test_self_completed_ticket_no_pending_stays_completed(kanban, monkeypatch):
    pid = 9005
    p = _set_dispatched(kanban, "1", pid)
    monkeypatch.setattr(oc, "CHAT_DIR",
                        os.path.join(kanban, "_orchestrator", "chat"))
    t = _read(p)
    t["status"] = "completed"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False})
    _stub_reap(monkeypatch)
    monkeypatch.setitem(orch._PROCS, pid, object())

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "completed"
    assert "pendingChat" not in t1
    assert "orchestrator" not in t1


def test_stop_all_preserves_pending_chat(kanban, monkeypatch):
    pid = 9006
    p = _set_dispatched(kanban, "1", pid)
    inbox = _queue_pending(kanban, monkeypatch, "demo", "1", ["urgent steer"])
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 3,
                            "stopAllRequested": True})
    monkeypatch.setattr(orch, "kill_pid", lambda _pid: True)
    monkeypatch.setattr(orch, "_process_alive", lambda _pid: True)

    orch.tick(kanban, opus_triage=lambda *a, **k: {"dispatch": []})

    t1 = _read(p)
    assert t1["status"] == "blocked"
    assert [m["message"] for m in t1["pendingChat"]] == ["urgent steer"]
    assert not os.path.exists(inbox)


# --- follow-up delivery: prompt injection + consumption at dispatch ---


def test_build_agent_prompt_injects_pending_chat(kanban):
    task = {"id": "1", "title": "First", "status": "ready",
            "_path": os.path.join(kanban, "boards", "demo", "1.json"), "_board": "demo",
            "pendingChat": [{"message": "use the py launcher", "writer": "ryan",
                             "ts": "2026-08-03T12:00:00+00:00"}]}
    prompt = orch._build_agent_prompt(task, {"name": "p", "systemPrompt": "sys"})
    assert "User guidance received mid-run" in prompt
    assert "use the py launcher" in prompt


def test_build_resume_prompt_injects_pending_chat(kanban):
    task = {"id": "1", "title": "First", "status": "blocked",
            "_path": os.path.join(kanban, "boards", "demo", "1.json"), "_board": "demo",
            "orchestrator": {"question": {"prompt": "q?",
                                          "answer": {"value": "v", "notes": ""}}},
            "pendingChat": [{"message": "prefer approach B", "writer": "ryan",
                             "ts": ""}]}
    prompt = orch._build_resume_prompt(task, {"name": "p"})
    assert "User guidance received mid-run" in prompt
    assert "prefer approach B" in prompt


def test_build_agent_prompt_without_pending_chat_unchanged(kanban):
    task = {"id": "1", "title": "First", "status": "ready",
            "_path": os.path.join(kanban, "boards", "demo", "1.json"), "_board": "demo"}
    prompt = orch._build_agent_prompt(task, {"name": "p", "systemPrompt": "sys"})
    assert "User guidance received mid-run" not in prompt


def test_dispatch_consumes_pending_chat(kanban, monkeypatch):
    p = os.path.join(kanban, "boards", "demo", "1.json")
    t = _read(p)
    t["status"] = "ready"
    t["pendingChat"] = [{"message": "queued guidance", "writer": "ryan",
                         "ts": oc.now_iso()}]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    t["_path"] = p
    t["_board"] = "demo"

    def fake_spawn(kanban_dir, board, task, profile, model):
        return {"state": "dispatched", "profile": profile.get("name"),
                "model": model, "pid": 4242, "dispatchedAt": oc.now_iso(),
                "killRequested": False,
                "logFile": ".kanban/_orchestrator/runs/x.log"}

    monkeypatch.setattr(orch, "spawn_agent", fake_spawn)
    assert orch._dispatch_one(kanban, t, {"name": "p"}, "m") is True

    t1 = _read(p)
    assert t1["status"] == "in_progress"
    assert "pendingChat" not in t1, \
        "dispatch must consume pendingChat once it is in the run's prompt"
