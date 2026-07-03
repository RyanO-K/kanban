"""Tests for performance summary features: server-owned op labels, conversation summaries."""
import json
import os
import tempfile

import perf_monitor as pm


class FakeProc:
    def __init__(self, pid, name, cmdline, rss=10*1024*1024, cpu=1.0,
                 create_time=100.0, children=None, environ=None):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self._rss = rss
        self._cpu = cpu
        self._ct = create_time
        self._children = children or []
        self._environ = environ or {}

    def name(self): return self._name
    def cmdline(self): return self._cmdline
    def create_time(self): return self._ct
    def cpu_percent(self, interval=None): return self._cpu
    def memory_info(self):
        class M: pass
        m = M(); m.rss = self._rss; return m
    def children(self, recursive=False): return self._children
    def environ(self): return self._environ


# --- label propagation ---

def test_discover_sessions_includes_label_from_labels_fn():
    """sessions with a label in labels_fn get that label in the output."""
    claude = FakeProc(10, "claude.exe", ["claude.exe", "-p", "triage prompt"])
    labels = {10: "Triage: assigning model"}
    sessions = pm.discover_sessions(
        proc_iter=lambda: [claude],
        owned_pids={10},
        labels_fn=lambda: labels,
    )
    assert len(sessions) == 1
    assert sessions[0]["label"] == "Triage: assigning model"


def test_discover_sessions_no_label_when_not_in_labels_fn():
    """sessions not in labels_fn have no label (or None)."""
    claude = FakeProc(10, "claude.exe", ["claude.exe"])
    sessions = pm.discover_sessions(
        proc_iter=lambda: [claude],
        owned_pids=set(),
        labels_fn=lambda: {},
    )
    assert sessions[0].get("label") is None


def test_discover_sessions_no_labels_fn_no_label():
    """labels_fn=None (default) produces sessions without label."""
    claude = FakeProc(10, "claude.exe", ["claude.exe"])
    sessions = pm.discover_sessions(proc_iter=lambda: [claude], owned_pids=set())
    assert sessions[0].get("label") is None


# --- session_id_from_env ---

def test_session_id_from_env_reads_env_var():
    proc = FakeProc(10, "claude.exe", ["claude.exe"],
                    environ={"CLAUDE_CODE_SESSION_ID": "abc-123"})
    assert pm.session_id_from_env(proc) == "abc-123"


def test_session_id_from_env_missing_returns_none():
    proc = FakeProc(10, "claude.exe", ["claude.exe"], environ={})
    assert pm.session_id_from_env(proc) is None


def test_session_id_from_env_handles_exception():
    """If environ() raises (AccessDenied), return None."""
    class BadProc:
        def environ(self): raise PermissionError("access denied")
    assert pm.session_id_from_env(BadProc()) is None


# --- read_session_summary ---

def test_read_session_summary_returns_first_user_message(tmp_path):
    session_id = "test-session-001"
    proj_dir = tmp_path / "projects" / "test-project"
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / f"{session_id}.jsonl"
    entries = [
        {"type": "system", "sessionId": session_id},
        {"type": "user", "message": {"role": "user", "content": "Help me with the kanban board"},
         "sessionId": session_id},
        {"type": "assistant", "message": {"role": "assistant", "content": "Sure!"},
         "sessionId": session_id},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    result = pm.read_session_summary(session_id, claude_projects_dir=str(tmp_path / "projects"))
    assert result == "Help me with the kanban board"


def test_read_session_summary_truncates_long_message(tmp_path):
    session_id = "test-session-002"
    proj_dir = tmp_path / "projects" / "test-project"
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / f"{session_id}.jsonl"
    long_msg = "A" * 300
    entries = [
        {"type": "user", "message": {"role": "user", "content": long_msg},
         "sessionId": session_id},
    ]
    jsonl.write_text(json.dumps(entries[0]), encoding="utf-8")
    result = pm.read_session_summary(session_id, claude_projects_dir=str(tmp_path / "projects"))
    assert result is not None
    assert len(result) <= 203  # 200 chars + "..."


def test_read_session_summary_returns_none_for_unknown_session(tmp_path):
    result = pm.read_session_summary("no-such-session", claude_projects_dir=str(tmp_path))
    assert result is None


def test_read_session_summary_handles_list_content(tmp_path):
    """Content can be a list of content blocks; join text parts."""
    session_id = "test-session-003"
    proj_dir = tmp_path / "projects" / "proj"
    proj_dir.mkdir(parents=True)
    jsonl = proj_dir / f"{session_id}.jsonl"
    entry = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "text", "text": "Hello world"},
            {"type": "image", "source": "data:..."},
        ]},
        "sessionId": session_id,
    }
    jsonl.write_text(json.dumps(entry), encoding="utf-8")
    result = pm.read_session_summary(session_id, claude_projects_dir=str(tmp_path / "projects"))
    assert result == "Hello world"


# --- conversationSummary in discover_sessions ---

def test_discover_sessions_includes_conversation_summary_for_unowned():
    """Unowned interactive sessions get conversationSummary from session_id lookup."""
    called = []
    def fake_summary_fn(session_id):
        called.append(session_id)
        return "Working on Salesforce LWC components"

    claude = FakeProc(10, "claude.exe", ["claude.exe"],
                      environ={"CLAUDE_CODE_SESSION_ID": "sess-42"})
    sessions = pm.discover_sessions(
        proc_iter=lambda: [claude],
        owned_pids=set(),  # not owned
        env_reader=lambda p: "sess-42",  # returns session id string directly
        summary_fn=fake_summary_fn,
    )
    assert sessions[0]["conversationSummary"] == "Working on Salesforce LWC components"
    assert called == ["sess-42"]


def test_discover_sessions_no_conversation_summary_for_owned():
    """Owned processes (ticket agents) don't get conversationSummary."""
    def fake_summary_fn(session_id):
        return "should not be called"

    claude = FakeProc(10, "claude.exe", ["claude.exe", "-p", "ticket prompt"],
                      environ={"CLAUDE_CODE_SESSION_ID": "sess-10"})
    sessions = pm.discover_sessions(
        proc_iter=lambda: [claude],
        owned_pids={10},
        env_reader=lambda p: {"CLAUDE_CODE_SESSION_ID": "sess-10"},
        summary_fn=fake_summary_fn,
    )
    assert sessions[0].get("conversationSummary") is None


def test_discover_sessions_no_summary_when_no_env(monkeypatch):
    """When env_reader returns None, no conversationSummary."""
    claude = FakeProc(10, "claude.exe", ["claude.exe"])
    sessions = pm.discover_sessions(
        proc_iter=lambda: [claude],
        owned_pids=set(),
        env_reader=lambda p: None,
        summary_fn=lambda sid: "never",
    )
    assert sessions[0].get("conversationSummary") is None


# --- PerfSampler passes labels_fn through ---

def test_sampler_labels_fn_surfaces_in_snapshot():
    claude = FakeProc(10, "claude.exe", ["claude.exe", "-p", "summarize ticket 5"])
    labels = {10: "Summarizing ticket #5"}
    s = pm.PerfSampler(interval=0, cap=3, labels_fn=lambda: labels)
    s._proc_iter = lambda: [claude]
    s.sample_once()
    sess = s.snapshot()["sessions"][0]
    assert sess["label"] == "Summarizing ticket #5"


def test_sampler_summary_fn_surfaces_in_snapshot():
    claude = FakeProc(10, "claude.exe", ["claude.exe"])  # interactive, unowned
    s = pm.PerfSampler(interval=0, cap=3,
                       env_reader=lambda p: {"CLAUDE_CODE_SESSION_ID": "sess-99"},
                       summary_fn=lambda sid: "Writing Apex tests")
    s._proc_iter = lambda: [claude]
    s.sample_once()
    sess = s.snapshot()["sessions"][0]
    assert sess["conversationSummary"] == "Writing Apex tests"
