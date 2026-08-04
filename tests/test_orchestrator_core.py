import json
import os

import orchestrator_core as oc


def test_read_state_defaults(kanban):
    state = oc.read_state(kanban)
    assert state == {"enabled": False, "concurrencyCap": 3,
                     "stopAllRequested": False, "idleSeconds": 600,
                     "tickSeconds": 60, "maxAgentSeconds": 0, "triageTimeoutSeconds": 120,
                     "triageModel": "claude-opus-4-8",
                     "summarizerModel": "claude-opus-4-8",
                     "autoCommit": True, "autoPush": True}
    # File was created.
    assert os.path.isfile(os.path.join(kanban, "_orchestrator", "state.json"))


def test_write_then_read_state(kanban):
    oc.write_state(kanban, {"enabled": True, "concurrencyCap": 5, "stopAllRequested": False})
    assert oc.read_state(kanban)["concurrencyCap"] == 5


def test_append_and_read_activity(kanban):
    oc.append_activity(kanban, {"ts": oc.now_iso(), "kind": "dispatch", "ticket": "1"})
    oc.append_activity(kanban, {"ts": oc.now_iso(), "kind": "reap", "ticket": "1"})
    entries = oc.read_activity(kanban)
    assert len(entries) == 2
    assert entries[-1]["kind"] == "reap"


def test_profile_round_trip(kanban):
    oc.write_profile(kanban, {"name": "frontend", "whenToUse": "UI work"})
    assert oc.read_profile(kanban, "frontend")["whenToUse"] == "UI work"
    assert any(p["name"] == "frontend" for p in oc.list_profiles(kanban))
    assert oc.delete_profile(kanban, "frontend") is True
    assert oc.read_profile(kanban, "frontend") is None


def test_list_profiles_skips_malformed(kanban):
    oc.write_profile(kanban, {"name": "good", "whenToUse": "x"})
    with open(os.path.join(kanban, "config", "bad.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    names = [p["name"] for p in oc.list_profiles(kanban)]
    assert names == ["good"]


def test_marker_helpers():
    task = {"id": "1"}
    assert oc.get_marker(task) is None
    oc.set_marker(task, {"state": "dispatched", "pid": 999})
    assert oc.get_marker(task)["pid"] == 999
    oc.clear_marker(task)
    assert "orchestrator" not in task


def test_agent_left_signal_question():
    task = {"id": "1", "status": "in_progress",
            "orchestrator": {"question": {"prompt": "?"}}}
    assert oc.agent_left_signal(task) == "question"


def test_agent_left_signal_claude_comment():
    task = {"id": "1", "status": "in_progress",
            "comments": [{"writer": "Claude", "message": "done"}]}
    assert oc.agent_left_signal(task) == "progress"


def test_agent_left_signal_status_moved():
    task = {"id": "1", "status": "completed"}
    assert oc.agent_left_signal(task) == "progress"


# --- #29: output branch naming ---

def test_branch_name_follows_ticket_convention():
    """The output branch for a ticket is `ticket/<id>-<slug>` where the slug is a
    lowercased, hyphenated form of the title — matching the CLAUDE.md git workflow
    convention (e.g. ticket/25-worktree-guidance)."""
    task = {"id": "25", "title": "Worktree guidance"}
    assert oc.branch_name(task) == "ticket/25-worktree-guidance"


def test_branch_name_sanitizes_title():
    """Punctuation/spaces collapse to single hyphens; result is git-ref-safe and
    has no leading/trailing hyphens."""
    task = {"id": "7", "title": "  Fix the (weird) Title!! / output  "}
    name = oc.branch_name(task)
    assert name.startswith("ticket/7-")
    slug = name[len("ticket/7-"):]
    assert slug == "fix-the-weird-title-output"
    # git-ref-safe: no spaces, no double hyphens, no leading/trailing hyphen.
    assert " " not in slug and "--" not in slug
    assert not slug.startswith("-") and not slug.endswith("-")


def test_branch_name_empty_title_falls_back_to_id():
    """A ticket with no usable title still produces a stable, valid branch name."""
    assert oc.branch_name({"id": "9", "title": ""}) == "ticket/9"
    assert oc.branch_name({"id": "9", "title": "!!!"}) == "ticket/9"


def test_agent_left_signal_none():
    task = {"id": "1", "status": "in_progress"}
    assert oc.agent_left_signal(task) == "none"


def test_agent_left_signal_question_beats_comment():
    task = {"id": "1", "status": "completed",
            "comments": [{"writer": "Claude", "message": "x"}],
            "orchestrator": {"question": {"prompt": "?"}}}
    assert oc.agent_left_signal(task) == "question"


def _adopted(task, **kw):
    base = dict(alive=False, exit_code=None, now_ts=100.0, dispatched_ts=100.0,
                adopted=True)
    base.update(kw)
    return oc.reap_decision(task, **base)


def test_reap_adopted_dead_with_question_needs_human():
    task = {"orchestrator": {"question": {"prompt": "?"}}, "status": "in_progress"}
    assert _adopted(task)["action"] == "needs_human"


def test_reap_adopted_dead_with_comment_completed():
    task = {"comments": [{"writer": "Claude", "message": "done"}],
            "status": "in_progress"}
    assert _adopted(task)["action"] == "completed"


def test_reap_adopted_dead_no_signal_crashed():
    task = {"status": "in_progress"}
    assert _adopted(task)["action"] == "crashed"


def test_reap_adopted_still_alive_runs():
    task = {"status": "in_progress",
            "comments": [{"writer": "Claude", "message": "x"}]}
    assert _adopted(task, alive=True)["action"] == "running"


def test_reap_adopted_kill_requested_still_wins():
    task = {"status": "in_progress",
            "orchestrator": {"killRequested": True,
                             "question": {"prompt": "?"}}}
    assert _adopted(task)["action"] == "kill_requested"


def test_reap_non_adopted_unchanged_by_exit_code():
    task = {"status": "in_progress",
            "comments": [{"writer": "Claude", "message": "x"}]}
    d = oc.reap_decision(task, alive=False, exit_code=0, now_ts=1.0,
                         dispatched_ts=1.0)
    assert d["action"] == "completed"
    d2 = oc.reap_decision(task, alive=False, exit_code=1, now_ts=1.0,
                          dispatched_ts=1.0)
    assert d2["action"] == "crashed"


def test_safe_name():
    assert oc.safe_name("frontend") == "frontend"
    assert oc.safe_name("../etc") is None
    assert oc.safe_name("a/b") is None
    assert oc.safe_name("..") is None
    assert oc.safe_name("a\\b") is None
    assert oc.safe_name("") is None
    assert oc.safe_name(".") is None


def test_is_in_flight():
    assert oc.is_in_flight({"id": "1"}) is False
    assert oc.is_in_flight({"id": "1", "orchestrator": {"state": "done"}}) is False
    assert oc.is_in_flight({"id": "1", "orchestrator": {"state": "dispatched"}}) is True


def test_eligible_no_deps():
    # Tickets are dispatched from `ready`, not `todo` (ticket 23).
    tasks = [{"id": "1", "status": "ready"}]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["1"]


def test_eligible_blocked_by_incomplete_dep():
    # A todo ticket whose dep is incomplete is neither promotable nor eligible.
    tasks = [
        {"id": "1", "status": "todo"},
        {"id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    assert oc.eligible_tickets(tasks) == []


def test_eligible_skips_in_flight_and_completed():
    tasks = [
        {"id": "1", "status": "completed"},
        {"id": "2", "status": "in_progress", "orchestrator": {"state": "dispatched"}},
        {"id": "3", "status": "ready"},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["3"]


def test_eligible_answered_question_redispatch():
    tasks = [
        {"id": "1", "status": "blocked",
         "orchestrator": {"state": "blocked",
                          "question": {"id": "q1", "answer": {"value": "x", "notes": ""}}}},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["1"]


# --- Ticket #13: resume the SAME session when unblocking, not a fresh context ---

def test_resume_session_id_for_unblocked_ticket_with_prior_session():
    """A blocked ticket that has been unblocked (its question answered) and
    already ran once (records a claudeSessionId) must resume THAT session so it
    keeps its context, rather than starting fresh."""
    task = {"id": "1", "status": "blocked", "claudeSessionId": "sess-abc",
            "orchestrator": {"state": "blocked",
                             "question": {"id": "q1",
                                          "answer": {"value": "go", "notes": ""}}}}
    assert oc.resume_session_id(task) == "sess-abc"


def test_no_resume_without_prior_session():
    """A first-ever dispatch has no prior claudeSessionId, so there is nothing to
    resume — it must start a fresh session."""
    task = {"id": "1", "status": "blocked",
            "orchestrator": {"state": "blocked",
                             "question": {"id": "q1",
                                          "answer": {"value": "go", "notes": ""}}}}
    assert oc.resume_session_id(task) is None


def test_no_resume_when_not_unblocked():
    """A ticket carrying a prior session but no answered question is fresh `ready`
    work (or an unanswered block), not an unblock — it must not resume."""
    ready = {"id": "1", "status": "ready", "claudeSessionId": "sess-abc"}
    assert oc.resume_session_id(ready) is None
    unanswered = {"id": "2", "status": "blocked", "claudeSessionId": "sess-def",
                  "orchestrator": {"state": "blocked",
                                   "question": {"id": "q1", "answer": None}}}
    assert oc.resume_session_id(unanswered) is None


# --- Ticket 23: TODO -> Ready promotion + dispatch from ready ---

def test_promotable_todo_with_no_deps():
    """A todo ticket with no dependencies is promotable to ready."""
    tasks = [{"id": "1", "status": "todo"}]
    assert [t["id"] for t in oc.promotable_tickets(tasks)] == ["1"]


def test_promotable_todo_with_met_deps():
    """A todo ticket whose deps are all completed is promotable to ready."""
    tasks = [
        {"id": "1", "status": "completed"},
        {"id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    assert [t["id"] for t in oc.promotable_tickets(tasks)] == ["2"]


def test_not_promotable_todo_with_unmet_deps():
    """A todo ticket with an incomplete dependency is NOT promotable."""
    tasks = [
        {"id": "1", "status": "todo"},
        {"id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    assert [t["id"] for t in oc.promotable_tickets(tasks)] == ["1"]


def test_not_promotable_already_ready():
    """A ticket already in ready is not re-promoted (only todo is promotable)."""
    tasks = [{"id": "1", "status": "ready"}]
    assert oc.promotable_tickets(tasks) == []


def test_not_promotable_non_todo_states():
    """Only todo tickets are promotable; in_progress/blocked/completed are not."""
    tasks = [
        {"id": "1", "status": "in_progress"},
        {"id": "2", "status": "blocked"},
        {"id": "3", "status": "completed"},
        {"id": "4", "status": "ready"},
    ]
    assert oc.promotable_tickets(tasks) == []


def test_promotable_deps_resolved_per_board():
    """Promotion respects per-board dependency scoping like eligibility does."""
    tasks = [
        {"_board": "A", "id": "1", "status": "completed"},
        {"_board": "B", "id": "1", "status": "todo"},
        {"_board": "B", "id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    promo = {(t["_board"], t["id"]) for t in oc.promotable_tickets(tasks)}
    assert ("B", "2") not in promo  # its dep (B,1) is still todo
    assert ("B", "1") in promo


def test_eligible_dispatches_from_ready_not_todo():
    """The orchestrator dispatches from `ready`, not `todo`. A todo ticket with
    met deps is NOT eligible (it must be promoted to ready first)."""
    tasks = [
        {"id": "1", "status": "todo"},
        {"id": "2", "status": "ready"},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["2"]


def test_eligible_ready_ignores_deps():
    """A ready ticket is already past dependency gating, so it is eligible even
    if a (defensive) dependsOn lists an unfinished ticket."""
    tasks = [
        {"id": "1", "status": "todo"},
        {"id": "2", "status": "ready", "dependsOn": ["1"]},
    ]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["2"]


def test_validate_triage_filters():
    resp = {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "claude-opus-4-8", "reason": "ui"},
        {"ticket": "9", "profile": "frontend", "reason": "not eligible"},
        {"ticket": "2", "profile": "ghost", "reason": "bad profile"},
        {"profile": "frontend"},
    ]}
    out = oc.validate_triage(resp, {"frontend"}, {"1", "2"})
    assert len(out) == 1
    assert out[0]["ticket"] == "1"


def test_validate_triage_handles_garbage():
    assert oc.validate_triage({}, {"frontend"}, {"1"}) == []
    assert oc.validate_triage({"dispatch": "nope"}, {"frontend"}, {"1"}) == []


# --- Ticket #84: Fable model + opus fallback ---

def test_resolve_model_returns_fable_when_fable_available():
    """When fable is available, resolve_model returns claude-fable-5 unchanged."""
    assert oc.resolve_model("claude-fable-5", fable_available=True) == "claude-fable-5"


def test_resolve_model_falls_back_to_opus_when_fable_unavailable():
    """When fable is NOT available, resolve_model returns the fallback (opus)."""
    result = oc.resolve_model("claude-fable-5", fable_available=False)
    assert result == oc.FABLE_FALLBACK_MODEL


def test_resolve_model_passes_through_non_fable_models():
    """Non-fable models are returned unchanged regardless of fable availability."""
    assert oc.resolve_model("claude-opus-4-8", fable_available=False) == "claude-opus-4-8"
    assert oc.resolve_model("claude-sonnet-4-6", fable_available=True) == "claude-sonnet-4-6"
    assert oc.resolve_model(None, fable_available=False) is None


def test_resolve_model_fable_constant_is_fable_5():
    """The FABLE_MODEL constant names fable-5 and FABLE_FALLBACK_MODEL is opus."""
    assert oc.FABLE_MODEL == "claude-fable-5"
    assert "opus" in oc.FABLE_FALLBACK_MODEL


# --- FIX 1 regression: completed + answered question must NOT be eligible ---

def test_eligible_completed_with_answered_question_not_eligible():
    """A completed ticket that happens to carry an answered question must NOT re-dispatch."""
    tasks = [
        {"id": "1", "status": "completed",
         "orchestrator": {"state": "done",
                          "question": {"id": "q1", "answer": {"value": "x", "notes": ""}}}},
    ]
    assert oc.eligible_tickets(tasks) == []


# --- FIX 2: dependsOn as a plain string (not a list) ---
# These exercise dependency satisfaction, which now gates TODO -> Ready promotion.

def test_promotable_string_dep_completed():
    """dependsOn given as a plain string is treated as a single dependency."""
    tasks = [
        {"id": "1", "status": "completed"},
        {"id": "2", "status": "todo", "dependsOn": "1"},
    ]
    assert [t["id"] for t in oc.promotable_tickets(tasks)] == ["2"]


def test_promotable_string_dep_incomplete():
    """dependsOn as a string where the dep is not completed — not promotable."""
    tasks = [
        {"id": "1", "status": "todo"},
        {"id": "2", "status": "todo", "dependsOn": "1"},
    ]
    assert [t["id"] for t in oc.promotable_tickets(tasks)] == ["1"]


def test_eligible_done_status_excluded():
    """A ticket already `done` (not `completed`) is NOT eligible to start."""
    tasks = [{"id": "1", "status": "done"}, {"id": "2", "status": "ready"}]
    assert [t["id"] for t in oc.eligible_tickets(tasks)] == ["2"]


def test_promotable_dep_satisfied_by_done_status():
    """A dependency counts as met when the dep is `done` (not just `completed`)."""
    tasks = [
        {"id": "1", "status": "done"},
        {"id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    assert [t["id"] for t in oc.promotable_tickets(tasks)] == ["2"]


def test_promotable_deps_resolved_per_board_strict():
    """Ticket ids are unique only per board: a same-id ticket on another board
    must not satisfy a dependency. Ticket (B,2) depends on (B,1) which is todo,
    while (A,1) is completed — (B,2) must NOT be promotable."""
    tasks = [
        {"_board": "A", "id": "1", "status": "completed"},
        {"_board": "B", "id": "1", "status": "todo"},
        {"_board": "B", "id": "2", "status": "todo", "dependsOn": ["1"]},
    ]
    promo = {(t["_board"], t["id"]) for t in oc.promotable_tickets(tasks)}
    assert ("B", "2") not in promo
    assert ("B", "1") in promo


def test_promotable_missing_dep_not_met():
    """A dependency that points to a non-existent ticket is not satisfied."""
    tasks = [
        {"_board": "A", "id": "5", "status": "todo", "dependsOn": ["4"]},
    ]
    assert oc.promotable_tickets(tasks) == []


# --- FIX 3: validate_triage kept item has correct shape ---

def test_validate_triage_filters_full_shape():
    """The kept dispatch item has the expected profile, model, and reason values."""
    resp = {"dispatch": [
        {"ticket": "1", "profile": "frontend", "model": "claude-opus-4-8", "reason": "ui"},
        {"ticket": "9", "profile": "frontend", "reason": "not eligible"},
        {"ticket": "2", "profile": "ghost", "reason": "bad profile"},
        {"profile": "frontend"},
    ]}
    out = oc.validate_triage(resp, {"frontend"}, {"1", "2"})
    assert len(out) == 1
    assert out[0]["ticket"] == "1"
    assert out[0]["profile"] == "frontend"
    assert out[0]["model"] == "claude-opus-4-8"
    assert out[0]["reason"] == "ui"


def _mk(marker=None, **kw):
    t = {"id": "1", "status": "in_progress"}
    if marker is not None:
        t["orchestrator"] = marker
    t.update(kw)
    return t


def test_reap_kill_requested():
    t = _mk({"state": "dispatched", "killRequested": True})
    d = oc.reap_decision(t, alive=True, exit_code=None, now_ts=100, dispatched_ts=0)
    assert d["action"] == "kill_requested"


def test_reap_completed():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=False, exit_code=0, now_ts=100, dispatched_ts=0)
    assert d["action"] == "completed"


def test_reap_needs_human():
    t = _mk({"state": "dispatched", "question": {"id": "q1"}})
    d = oc.reap_decision(t, alive=False, exit_code=0, now_ts=100, dispatched_ts=0)
    assert d["action"] == "needs_human"


def test_reap_crashed():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=False, exit_code=1, now_ts=100, dispatched_ts=0)
    assert d["action"] == "crashed"


def test_reap_stalled():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=True, exit_code=None, now_ts=1000, dispatched_ts=0,
                         stall_seconds=900)
    assert d["action"] == "stalled"


def test_reap_running():
    t = _mk({"state": "dispatched"})
    d = oc.reap_decision(t, alive=True, exit_code=None, now_ts=100, dispatched_ts=0,
                         stall_seconds=900)
    assert d["action"] == "running"


def test_build_and_answer_question():
    q = oc.build_question("Pick one", "choice", options=["a", "b"], multi=False)
    assert q["type"] == "choice"
    assert q["options"] == ["a", "b"]
    assert q["answer"] is None
    answered = oc.apply_answer(q, "a", "use a")
    assert answered["answer"] == {"value": "a", "notes": "use a"}
    assert answered["answeredAt"]


# --- idle-stall tracking + concurrency backfill ---

def test_default_state_has_idle_seconds():
    assert oc.DEFAULT_STATE["idleSeconds"] == 600


def test_read_state_default_includes_idle(kanban):
    assert oc.read_state(kanban)["idleSeconds"] == 600


def test_read_state_keeps_set_idle(kanban):
    oc.write_state(kanban, {"enabled": False, "concurrencyCap": 3,
                            "stopAllRequested": False, "idleSeconds": 120})
    assert oc.read_state(kanban)["idleSeconds"] == 120


def test_note_log_growth_grew_updates_both():
    m = {"logSize": 100}
    out = oc.note_log_growth(m, 250, "2026-06-25T10:00:00+00:00")
    assert out["logSize"] == 250
    assert out["lastGrowthAt"] == "2026-06-25T10:00:00+00:00"


def test_note_log_growth_no_growth_keeps_lastgrowth():
    m = {"logSize": 250, "lastGrowthAt": "2026-06-25T09:00:00+00:00"}
    out = oc.note_log_growth(m, 250, "2026-06-25T10:00:00+00:00")
    assert out["logSize"] == 250
    assert out["lastGrowthAt"] == "2026-06-25T09:00:00+00:00"


def test_note_log_growth_seeds_lastgrowth_when_absent():
    m = {"logSize": 0}
    out = oc.note_log_growth(m, 0, "2026-06-25T10:00:00+00:00")
    assert out["lastGrowthAt"] == "2026-06-25T10:00:00+00:00"


def test_reap_idle_growing_log_runs():
    task = {"status": "in_progress", "orchestrator": {"state": "dispatched"}}
    d = oc.reap_decision(task, alive=True, exit_code=None, now_ts=1000.0,
                         dispatched_ts=0.0, idle_seconds=600, last_growth_ts=970.0)
    assert d["action"] == "running"


def test_reap_idle_flat_log_stalls():
    task = {"status": "in_progress", "orchestrator": {"state": "dispatched"}}
    d = oc.reap_decision(task, alive=True, exit_code=None, now_ts=1000.0,
                         dispatched_ts=0.0, idle_seconds=600, last_growth_ts=300.0)
    assert d["action"] == "stalled"


def test_reap_idle_falls_back_to_dispatched_when_no_growth_ts():
    task = {"status": "in_progress", "orchestrator": {"state": "dispatched"}}
    d = oc.reap_decision(task, alive=True, exit_code=None, now_ts=1000.0,
                         dispatched_ts=300.0, idle_seconds=600, last_growth_ts=None)
    assert d["action"] == "stalled"


def test_reap_legacy_wallclock_when_no_idle_seconds():
    task = {"status": "in_progress", "orchestrator": {"state": "dispatched"}}
    d = oc.reap_decision(task, alive=True, exit_code=None, now_ts=1000.0,
                         dispatched_ts=0.0, stall_seconds=900)
    assert d["action"] == "stalled"
    d2 = oc.reap_decision(task, alive=True, exit_code=None, now_ts=500.0,
                          dispatched_ts=0.0, stall_seconds=900)
    assert d2["action"] == "running"


# --- Ticket #56: tick interval and agent/triage timeouts as settings ---

def test_default_state_has_tick_seconds():
    assert oc.DEFAULT_STATE["tickSeconds"] == 60


def test_default_state_has_max_agent_seconds():
    assert oc.DEFAULT_STATE["maxAgentSeconds"] == 0


def test_default_state_has_triage_timeout_seconds():
    assert oc.DEFAULT_STATE["triageTimeoutSeconds"] == 120


def test_read_state_default_includes_new_fields(kanban):
    state = oc.read_state(kanban)
    assert state["tickSeconds"] == 60
    assert state["maxAgentSeconds"] == 0
    assert state["triageTimeoutSeconds"] == 120


def test_reap_decision_max_agent_seconds_stalls_alive_agent():
    task = {"status": "in_progress", "orchestrator": {"state": "dispatched", "killRequested": False}}
    d = oc.reap_decision(task, alive=True, exit_code=None, now_ts=3601.0,
                         dispatched_ts=0.0, idle_seconds=600, last_growth_ts=3600.0,
                         max_agent_seconds=3600)
    assert d["action"] == "stalled"


def test_reap_decision_max_agent_seconds_not_yet_stalled():
    task = {"status": "in_progress", "orchestrator": {"state": "dispatched", "killRequested": False}}
    d = oc.reap_decision(task, alive=True, exit_code=None, now_ts=3599.0,
                         dispatched_ts=0.0, idle_seconds=600, last_growth_ts=3598.0,
                         max_agent_seconds=3600)
    assert d["action"] == "running"


def test_reap_decision_max_agent_seconds_zero_disables_cap():
    task = {"status": "in_progress", "orchestrator": {"state": "dispatched", "killRequested": False}}
    d = oc.reap_decision(task, alive=True, exit_code=None, now_ts=99999.0,
                         dispatched_ts=0.0, idle_seconds=600, last_growth_ts=99998.0,
                         max_agent_seconds=0)
    assert d["action"] == "running"


def test_backfill_fills_remaining_slots():
    eligible = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    chosen = [{"ticket": "1", "profile": "p", "model": "m"}]
    profiles = [{"name": "p", "whenToUse": "x", "model": "m"}]
    extra = oc.backfill_dispatch(eligible, chosen, profiles, free=3)
    ids = [e["ticket"] for e in extra]
    assert ids == ["2", "3"]
    assert all(e["profile"] == "p" and e["model"] == "m" for e in extra)
    assert all(e["reason"] == "backfill" for e in extra)


def test_backfill_respects_free_cap():
    eligible = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    profiles = [{"name": "p", "whenToUse": "x", "model": "m"}]
    extra = oc.backfill_dispatch(eligible, [], profiles, free=2)
    assert [e["ticket"] for e in extra] == ["1", "2"]


def test_backfill_skips_already_chosen():
    eligible = [{"id": "1"}, {"id": "2"}]
    chosen = [{"ticket": "2", "profile": "p", "model": "m"}]
    profiles = [{"name": "p", "whenToUse": "x", "model": "m"}]
    extra = oc.backfill_dispatch(eligible, chosen, profiles, free=2)
    assert [e["ticket"] for e in extra] == ["1"]


def test_backfill_picks_profile_with_whenToUse():
    eligible = [{"id": "1"}]
    profiles = [{"name": "empty", "whenToUse": "", "model": "m1"},
                {"name": "real", "whenToUse": "ui", "model": "m2"}]
    extra = oc.backfill_dispatch(eligible, [], profiles, free=1)
    assert extra[0]["profile"] == "real"
    assert extra[0]["model"] == "m2"


def test_backfill_empty_when_no_profiles_or_full():
    assert oc.backfill_dispatch([{"id": "1"}], [], [], free=1) == []
    chosen = [{"ticket": "1", "profile": "p", "model": "m"}]
    assert oc.backfill_dispatch([{"id": "1"}], chosen,
                                [{"name": "p", "whenToUse": "x"}], free=1) == []


# --- #52: dispatch keying by (board, id) — ids collide across boards ---

def test_validate_triage_keys_by_board_id():
    """Ids are unique only within a board, so an ambiguous id must be matched by
    (board, id). Two boards both carry id "5"; each dispatch item names its board,
    and both are kept and tagged with the right board."""
    eligible_ids = {("A", "5"), ("B", "5")}
    resp = {"dispatch": [
        {"ticket": "5", "board": "A", "profile": "p", "reason": "a"},
        {"ticket": "5", "board": "B", "profile": "p", "reason": "b"},
    ]}
    out = oc.validate_triage(resp, {"p"}, eligible_ids)
    assert {(o["board"], o["ticket"]) for o in out} == {("A", "5"), ("B", "5")}


def test_validate_triage_ambiguous_id_without_board_rejected():
    """When an id is ambiguous across boards, a board-less dispatch item cannot be
    resolved to a single ticket and must be dropped (no bare-id fallback)."""
    eligible_ids = {("A", "5"), ("B", "5")}
    resp = {"dispatch": [{"ticket": "5", "profile": "p", "reason": "x"}]}
    assert oc.validate_triage(resp, {"p"}, eligible_ids) == []


def test_validate_triage_dedupes_duplicate_entries():
    """A triage response that names the same (board, id) twice yields one dispatch
    entry, so the spawn loop never double-dispatches one ticket."""
    eligible_ids = {("A", "5")}
    resp = {"dispatch": [
        {"ticket": "5", "board": "A", "profile": "p", "reason": "first"},
        {"ticket": "5", "board": "A", "profile": "p", "reason": "dup"},
    ]}
    out = oc.validate_triage(resp, {"p"}, eligible_ids)
    assert len(out) == 1


def test_backfill_keys_by_board_id():
    """Backfill keys by (board, id): two boards each with id "5" both get a slot
    rather than one masking the other."""
    eligible = [{"_board": "A", "id": "5"}, {"_board": "B", "id": "5"}]
    profiles = [{"name": "p", "whenToUse": "x", "model": "m"}]
    extra = oc.backfill_dispatch(eligible, [], profiles, free=3)
    assert {(e["board"], e["ticket"]) for e in extra} == {("A", "5"), ("B", "5")}


def test_backfill_skips_already_chosen_by_board():
    """A board-less chosen item for an unambiguous id is resolved to its board so
    backfill does not re-add that same ticket."""
    eligible = [{"_board": "A", "id": "1"}, {"_board": "A", "id": "2"}]
    chosen = [{"ticket": "1", "profile": "p", "model": "m"}]  # board-less, unambiguous
    profiles = [{"name": "p", "whenToUse": "x", "model": "m"}]
    extra = oc.backfill_dispatch(eligible, chosen, profiles, free=3)
    assert [e["ticket"] for e in extra] == ["2"]


# --- Ticket #60: usage-limit detection + auto-resume pause ---

def test_parse_usage_limit_pipe_epoch():
    """The canonical headless `claude -p` signal carries the reset epoch after a
    pipe: `Claude AI usage limit reached|<unix_seconds>`."""
    out = oc.parse_usage_limit("Claude AI usage limit reached|1719500000")
    assert out == {"resetAt": 1719500000}


def test_parse_usage_limit_milliseconds_epoch():
    """A 13-digit epoch (milliseconds) is normalised to whole seconds."""
    out = oc.parse_usage_limit("usage limit reached, resets at 1719500000000")
    assert out == {"resetAt": 1719500000}


def test_parse_usage_limit_phrase_without_epoch():
    """A usage-limit message with no parseable reset time is still detected
    (resetAt None), so the caller can fall back to the default window."""
    out = oc.parse_usage_limit("Claude usage limit reached. Please try again later.")
    assert out == {"resetAt": None}


def test_parse_usage_limit_case_insensitive_in_stream_json():
    """The phrase is matched case-insensitively anywhere in a stream-json blob."""
    blob = '{"type":"result","is_error":true,"result":"Claude AI Usage Limit Reached|1700000000"}'
    assert oc.parse_usage_limit(blob) == {"resetAt": 1700000000}


def test_parse_usage_limit_none_for_unrelated_text():
    """Ordinary log output is not mistaken for a usage limit (and a bare 10-digit
    number elsewhere is not parsed as a reset time)."""
    assert oc.parse_usage_limit("Tool ran fine, session 1719500000 done") is None
    assert oc.parse_usage_limit("") is None
    assert oc.parse_usage_limit(None) is None


def test_usage_pause_set_with_explicit_reset(kanban):
    """Setting a pause with a known future reset epoch parks dispatch until then."""
    oc.set_usage_pause(kanban, reset_at=1000, now_ts=100, reason="ticket 1")
    assert oc.is_usage_paused(kanban, now_ts=500) is True
    assert oc.usage_pause_remaining(kanban, now_ts=500) == 500
    assert oc.read_usage_pause(kanban)["pausedUntil"] == 1000
    assert oc.read_usage_pause(kanban)["reason"] == "ticket 1"


def test_usage_pause_expires_then_not_paused(kanban):
    """Once now passes the reset epoch the pause is no longer active (remaining 0)."""
    oc.set_usage_pause(kanban, reset_at=1000, now_ts=100)
    assert oc.is_usage_paused(kanban, now_ts=1000) is False
    assert oc.is_usage_paused(kanban, now_ts=2000) is False
    assert oc.usage_pause_remaining(kanban, now_ts=2000) == 0


def test_usage_pause_falls_back_to_default_window(kanban):
    """With no reset epoch (or a stale one) the pause uses now + default window."""
    oc.set_usage_pause(kanban, reset_at=None, now_ts=100, default_seconds=300)
    assert oc.read_usage_pause(kanban)["pausedUntil"] == 400
    # A reset already in the past also falls back to the default window.
    oc.set_usage_pause(kanban, reset_at=50, now_ts=100, default_seconds=300)
    assert oc.read_usage_pause(kanban)["pausedUntil"] == 400


def test_usage_pause_clear(kanban):
    """Clearing removes the pause entirely; reads return an empty dict."""
    oc.set_usage_pause(kanban, reset_at=1000, now_ts=100)
    oc.clear_usage_pause(kanban)
    assert oc.read_usage_pause(kanban) == {}
    assert oc.is_usage_paused(kanban, now_ts=500) is False
    # Clearing again is a harmless no-op.
    oc.clear_usage_pause(kanban)


def test_usage_pause_unset_reads_empty(kanban):
    """No pause file => not paused, zero remaining."""
    assert oc.read_usage_pause(kanban) == {}
    assert oc.is_usage_paused(kanban, now_ts=500) is False
    assert oc.usage_pause_remaining(kanban, now_ts=500) == 0


# --- Agent chat: pure helpers (spec docs/specs/2026-07-03-agent-chat-design.md) ---


def test_chat_constants():
    assert oc.CHAT_ENABLED is True
    assert oc.CHAT_DIR == os.path.join(oc.ORCH_DIR, "chat")


def test_chat_inbox_path_uses_chat_dir(monkeypatch):
    # chat_inbox_path must read the module-global CHAT_DIR at call time so
    # tests (and any future config) can repoint it.
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join("x", "chat"))
    assert oc.chat_inbox_path("demo", "7") == os.path.join("x", "chat", "demo__7.jsonl")
    assert oc.chat_inbox_path("my-board", "12") == os.path.join(
        "x", "chat", "my-board__12.jsonl")


def test_chat_encode_user_message_shape():
    line = oc.chat_encode_user_message("hello ünïcode")
    assert line.endswith("\n")
    assert "\n" not in line[:-1], "must be exactly one JSONL line"
    assert json.loads(line) == {
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": "hello ünïcode"}]},
    }


def test_chat_encode_user_message_preserves_newlines_in_text():
    # Wrapped chat messages contain a literal \n ([Message from ...]\n<msg>);
    # it must survive as an escaped newline inside the single JSONL line.
    line = oc.chat_encode_user_message("[Message from ryan via Discord]\nhi")
    assert "\n" not in line[:-1]
    obj = json.loads(line)
    assert obj["message"]["content"][0]["text"] == "[Message from ryan via Discord]\nhi"


def test_chat_parse_inbox_line_valid():
    raw = json.dumps({"message": "hi", "writer": "alice",
                      "ts": "2026-07-03T00:00:00+00:00"})
    assert oc.chat_parse_inbox_line(raw) == {
        "message": "hi", "writer": "alice", "ts": "2026-07-03T00:00:00+00:00"}


def test_chat_parse_inbox_line_defaults_missing_writer_and_ts():
    assert oc.chat_parse_inbox_line(json.dumps({"message": "hi"})) == {
        "message": "hi", "writer": "unknown", "ts": ""}


def test_chat_parse_inbox_line_rejects_malformed():
    assert oc.chat_parse_inbox_line("{not json") is None
    assert oc.chat_parse_inbox_line("") is None
    assert oc.chat_parse_inbox_line(json.dumps(["a", "list"])) is None
    assert oc.chat_parse_inbox_line(json.dumps({"writer": "a"})) is None          # no message
    assert oc.chat_parse_inbox_line(json.dumps({"message": ""})) is None          # empty
    assert oc.chat_parse_inbox_line(json.dumps({"message": "   "})) is None       # whitespace
    assert oc.chat_parse_inbox_line(json.dumps({"message": 42})) is None          # non-string


def test_chat_should_close_truth_table():
    # Close only when a result has been seen SINCE the last injected message
    # AND the inbox is drained.
    assert oc.chat_should_close(True, True) is True
    assert oc.chat_should_close(True, False) is False
    assert oc.chat_should_close(False, True) is False
    assert oc.chat_should_close(False, False) is False

# --- Agent chat: delivered-offset sidecar + queue visibility (bot messaging) ---


def _inbox_line(message, writer="ryan", ts="2026-08-03T00:00:00+00:00"):
    return json.dumps({"message": message, "writer": writer, "ts": ts},
                      ensure_ascii=False) + "\n"


def test_chat_offset_path_is_pos_sidecar():
    assert oc.chat_offset_path(os.path.join("x", "demo__1.jsonl")) == \
        os.path.join("x", "demo__1.jsonl.pos")


def test_chat_offset_roundtrip(tmp_path):
    inbox = str(tmp_path / "demo__1.jsonl")
    assert oc.chat_read_offset(inbox) == 0          # no sidecar yet
    oc.chat_write_offset(inbox, 42)
    assert oc.chat_read_offset(inbox) == 42
    oc.chat_write_offset(inbox, 0)
    assert oc.chat_read_offset(inbox) == 0


def test_chat_read_offset_malformed_sidecar(tmp_path):
    inbox = str(tmp_path / "demo__1.jsonl")
    with open(oc.chat_offset_path(inbox), "w", encoding="utf-8") as f:
        f.write("not a number")
    assert oc.chat_read_offset(inbox) == 0


def test_chat_split_messages_delivered_vs_pending():
    a = _inbox_line("first").encode("utf-8")
    b = _inbox_line("second", writer="bob").encode("utf-8")
    msgs = oc.chat_split_messages(a + b, len(a))
    assert [(m["message"], m["delivered"]) for m in msgs] == \
        [("first", True), ("second", False)]
    # Everything delivered.
    msgs = oc.chat_split_messages(a + b, len(a) + len(b))
    assert all(m["delivered"] for m in msgs)
    # Nothing delivered.
    msgs = oc.chat_split_messages(a + b, 0)
    assert not any(m["delivered"] for m in msgs)


def test_chat_split_messages_skips_malformed_and_partial():
    a = b"{not json at all\n"
    b = _inbox_line("real").encode("utf-8")
    partial = b'{"message": "still being writ'
    msgs = oc.chat_split_messages(a + b + partial, 0)
    assert [m["message"] for m in msgs] == ["real"]


def test_chat_read_messages_missing_file(tmp_path):
    assert oc.chat_read_messages(str(tmp_path / "nope.jsonl")) == []


def test_chat_read_messages_uses_sidecar_offset(tmp_path):
    # Text-mode appends (the server's own convention) become CRLF on Windows,
    # so the delivered offset is measured from the real on-disk size — exactly
    # what the pump's byte-offset tailing records.
    inbox = str(tmp_path / "demo__1.jsonl")
    with open(inbox, "w", encoding="utf-8") as f:
        f.write(_inbox_line("first"))
    oc.chat_write_offset(inbox, os.path.getsize(inbox))
    with open(inbox, "a", encoding="utf-8") as f:
        f.write(_inbox_line("second"))
    msgs = oc.chat_read_messages(inbox)
    assert [(m["message"], m["delivered"]) for m in msgs] == \
        [("first", True), ("second", False)]


def test_chat_clear_inbox_removes_both_files(tmp_path):
    inbox = str(tmp_path / "demo__1.jsonl")
    with open(inbox, "w", encoding="utf-8") as f:
        f.write(_inbox_line("x"))
    oc.chat_write_offset(inbox, 3)
    oc.chat_clear_inbox(inbox)
    assert not os.path.exists(inbox)
    assert not os.path.exists(oc.chat_offset_path(inbox))
    oc.chat_clear_inbox(inbox)  # idempotent on missing files


def test_chat_release_inbox_keeps_pending_deletes_drained(tmp_path):
    inbox = str(tmp_path / "demo__1.jsonl")
    with open(inbox, "w", encoding="utf-8") as f:
        f.write(_inbox_line("pending one"))
    # Nothing delivered yet: the inbox must SURVIVE release for reap to surface.
    oc.chat_release_inbox(inbox)
    assert os.path.exists(inbox)
    # Fully delivered: release deletes inbox + sidecar.
    oc.chat_write_offset(inbox, os.path.getsize(inbox))
    oc.chat_release_inbox(inbox)
    assert not os.path.exists(inbox)
    assert not os.path.exists(oc.chat_offset_path(inbox))


def test_chat_undelivered_comment_lists_messages():
    pending = [{"message": "check the tests", "writer": "ryan",
                "ts": "2026-08-03T12:00:00+00:00"},
               {"message": "use py launcher", "writer": "bob", "ts": ""}]
    text = oc.chat_undelivered_comment(pending)
    assert "NOT delivered" in text
    assert "pendingChat" in text
    assert "- ryan (2026-08-03T12:00:00+00:00): check the tests" in text
    assert "- bob: use py launcher" in text


def test_chat_prompt_section_empty_and_filled():
    assert oc.chat_prompt_section([]) == ""
    section = oc.chat_prompt_section(
        [{"message": "also update docs", "writer": "ryan",
          "ts": "2026-08-03T12:00:00+00:00"}])
    assert "User guidance received mid-run" in section
    assert "- From ryan at 2026-08-03T12:00:00+00:00: also update docs" in section
