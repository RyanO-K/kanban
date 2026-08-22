"""spawn_agent chat-mode dispatch tests (streaming stdin prompt).

Spec: docs/specs/2026-07-03-agent-chat-design.md, Component 3. Uses a fake
Popen; the pump seam (_start_chat_pump) is stubbed so no thread spins against
a fake proc.
"""
import io
import json
import os

import pytest

import orchestrator as orch
import orchestrator_core as oc


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.stdin = io.BytesIO()

    def poll(self):
        return None


@pytest.fixture
def chat_env(kanban, monkeypatch):
    """Repoint CHAT_DIR at the temp tree; stub the pump seam; fix the prompt."""
    monkeypatch.setattr(oc, "CHAT_DIR",
                        os.path.join(kanban, "_orchestrator", "chat"))
    monkeypatch.setattr(orch, "_build_agent_prompt",
                        lambda *a, **k: "PROMPT SENTINEL")
    pumps = []
    monkeypatch.setattr(orch, "_start_chat_pump",
                        lambda proc, inbox, log: pumps.append((proc, inbox, log)))
    return pumps


def _capture_popen(monkeypatch):
    captured = {}

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, stdin=None, **kw):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    return captured


def _task(kanban):
    return {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "boards", "demo", "1.json")}


def _first_stdin_text(proc):
    raw = proc.stdin.getvalue().decode("utf-8")
    obj = json.loads(raw.splitlines()[0])
    return obj["message"]["content"][0]["text"]


def test_spawn_agent_streams_prompt_via_stdin(kanban, chat_env, monkeypatch):
    captured = _capture_popen(monkeypatch)
    marker = orch.spawn_agent(kanban, "demo", _task(kanban),
                              {"name": "g", "systemPrompt": "p"}, "m")
    cmd = captured["cmd"]
    # New argv shape: no prompt in argv, streaming input flags present.
    assert cmd[1] == "-p"
    assert cmd[2:4] == ["--input-format", "stream-json"]
    assert "--output-format" in cmd and "--verbose" in cmd
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == marker["sessionId"]
    assert "PROMPT SENTINEL" not in cmd, "prompt must NOT be argv in chat mode"
    # stdin is a pipe and the prompt arrives as the first stream-json message.
    assert captured["stdin"] is orch.subprocess.PIPE
    raw = captured["proc"].stdin.getvalue().decode("utf-8")
    assert raw == oc.chat_encode_user_message("PROMPT SENTINEL")
    assert _first_stdin_text(captured["proc"]) == "PROMPT SENTINEL"
    assert marker["state"] == "dispatched"


def test_spawn_agent_truncates_stale_inbox_before_spawn(kanban, chat_env,
                                                        monkeypatch):
    captured = _capture_popen(monkeypatch)
    inbox = oc.chat_inbox_path("demo", "1")
    os.makedirs(os.path.dirname(inbox), exist_ok=True)
    with open(inbox, "w", encoding="utf-8") as f:
        f.write(json.dumps({"message": "stale from a previous run",
                            "writer": "old"}) + "\n")
    orch.spawn_agent(kanban, "demo", _task(kanban),
                     {"name": "g", "systemPrompt": "p"}, "m")
    assert not os.path.exists(inbox), \
        "stale inbox must be deleted before the new run spawns"
    assert "cmd" in captured  # sanity: we did spawn


def test_spawn_agent_starts_pump_with_inbox_and_log(kanban, chat_env,
                                                    monkeypatch):
    captured = _capture_popen(monkeypatch)
    marker = orch.spawn_agent(kanban, "demo", _task(kanban),
                              {"name": "g", "systemPrompt": "p"}, "m")
    assert len(chat_env) == 1, "exactly one pump per dispatch"
    proc, inbox, log = chat_env[0]
    assert proc is captured["proc"]
    assert inbox == oc.chat_inbox_path("demo", "1")
    # The pump watches the same run log the marker records.
    assert os.path.basename(log) == os.path.basename(marker["logFile"])
    assert os.path.isabs(log)


def test_spawn_agent_legacy_form_when_chat_disabled(kanban, chat_env,
                                                    monkeypatch):
    monkeypatch.setattr(oc, "CHAT_ENABLED", False)
    captured = _capture_popen(monkeypatch)
    marker = orch.spawn_agent(kanban, "demo", _task(kanban),
                              {"name": "g", "systemPrompt": "p"}, "m")
    cmd = captured["cmd"]
    # Byte-for-byte today's legacy argv: prompt at cmd[2], then session flag,
    # then output flags. No streaming-input flag anywhere.
    assert cmd[1] == "-p"
    assert cmd[2] == "PROMPT SENTINEL"
    assert cmd[3] == "--session-id"
    assert cmd[4] == marker["sessionId"]
    assert cmd[5:7] == ["--output-format", "stream-json"]
    assert cmd[7] == "--verbose"
    assert cmd[8:] == ["--model", "m"]
    assert "--input-format" not in cmd
    # No stdin pipe, nothing written, no pump.
    assert captured["stdin"] is None
    assert captured["proc"].stdin.getvalue() == b""
    assert chat_env == []


def test_spawn_agent_resume_streams_resume_prompt(kanban, chat_env,
                                                  monkeypatch):
    monkeypatch.setattr(orch, "_build_resume_prompt",
                        lambda *a, **k: "RESUME SENTINEL")
    captured = _capture_popen(monkeypatch)
    task = _task(kanban)
    task["status"] = "blocked"
    task["claudeSessionId"] = "prior-sess"
    task["orchestrator"] = {"state": "blocked",
                            "question": {"id": "q1", "prompt": "which?",
                                         "answer": {"value": "A", "notes": ""}}}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")
    cmd = captured["cmd"]
    assert "--resume" in cmd and "--session-id" not in cmd
    assert cmd[cmd.index("--resume") + 1] == "prior-sess"
    assert "--input-format" in cmd
    assert _first_stdin_text(captured["proc"]) == "RESUME SENTINEL"
