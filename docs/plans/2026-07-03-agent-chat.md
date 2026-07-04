# Agent Chat (stdin injection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human POST messages to a *running* orchestrator agent and have them delivered into the agent's context mid-run within seconds, via a per-run inbox file relayed to the agent process's stdin.

**Architecture:** The kanban server gains `POST /api/orchestrator/chat/<board>/<id>` which appends JSONL lines to `_orchestrator/chat/<board>__<id>.jsonl`. Dispatch switches to `claude -p --input-format stream-json` with the prompt written to stdin as the first user message; a per-run daemon pump thread tails the inbox, relays messages to stdin, watches the run log for `{"type":"result"}` lines, and closes stdin when the agent is done and the inbox is drained (which makes the CLI exit so the existing reap path proceeds unchanged). All decision/encoding logic is pure functions in `orchestrator_core.py`.

**Tech Stack:** Python 3 stdlib only (json, os, threading, subprocess), pytest. No new dependencies.

**Spec:** `docs/specs/2026-07-03-agent-chat-design.md` (approved). Implement exactly that — nothing more.

## Global Constraints

- Work in `C:\Users\ryan\Documents\Github\.kanban` (git repo, branch `release`). Commit directly with plain `git add`/`git commit` — no branch/merge steps.
- Names are decided by the spec — use them **verbatim**: `CHAT_ENABLED`, `CHAT_DIR`, `chat_inbox_path(board, ticket_id)`, `chat_encode_user_message(text)`, `chat_parse_inbox_line(line)`, `chat_should_close(result_seen_after_last_send, inbox_empty)`, `_chat_pump`, `_PUMPS`, `_start_chat_pump`, route `POST /api/orchestrator/chat/<board>/<id>`.
- Inbox file: `_orchestrator/chat/<board>__<ticket-id>.jsonl`; one JSON object per line `{"message": str, "writer": str, "ts": ISO-8601 UTC}`. Server appends raw fields (open `"a"`, single `write()` of one line + flush) — writer-attribution wrapping is done by the pump, NOT the server.
- Pump wraps each relayed message as `[Message from <writer> via Discord]\n<message>` before encoding it as a stream-json user line.
- Stream-json user line (exact shape, one line + `"\n"`): `{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": <text>}]}}`.
- `CHAT_ENABLED = False` must restore **byte-for-byte** today's legacy dispatch argv (prompt in argv, no `--input-format`, no stdin pipe, no pump thread) — the one-line escape hatch.
- Inbox lifecycle: deleted/truncated at dispatch of a new run for that ticket (before spawn), deleted by the pump on child death, deleted at reap (`_release_proc`).
- Endpoint contract: `200 {"ok": true}` | `400` empty/non-string message | `404 {"error": "not found"}` | `409 {"error": "not running"}` unless `orchestrator.state == "dispatched"` and `status == "in_progress"` | `409 {"error": "chat disabled"}` when `orchestrator_core.CHAT_ENABLED` is false. Gated by `_authorized()` like every other POST. Does NOT check the PID is alive.
- The pump thread must never die from an exception except its exit paths (child death, broken pipe, close decision). Broken pipe/OSError writing stdin = child death.
- `kill_pid`, `reap_decision`, log reading (`GET .../log`), and the UI are unchanged. Out of scope: Discord rendering, chat history beyond the run log, mid-turn interruption.
- Tests live in `tests/` (flat repo-root `tests/` directory; `tests/conftest.py` provides the `kanban` fixture and sys.path setup). Run with `python -m pytest tests -q` from the `.kanban` directory. No automated test may invoke the real `claude` CLI (a real round-trip is a documented manual step at the end).
- `CHAT_DIR` is computed at import time from the real `ORCH_DIR` — every test touching inbox paths must `monkeypatch.setattr(oc, "CHAT_DIR", <tmp path>)` so nothing writes into the real repo.

---

## File structure

| File | Responsibility |
|---|---|
| `orchestrator_core.py` (modify) | Pure chat helpers: flag, dir, path builder, encoder, parser, close decision; `docker_run_argv` gains `interactive` flag (Task 5). |
| `kanban_server.py` (modify) | `orch_chat()` handler + `do_POST` route. |
| `orchestrator.py` (modify) | `_PUMPS` registry, `_tail_new_lines`, `_chat_pump`, `_start_chat_pump`, `_release_proc` cleanup, `spawn_agent` streaming dispatch, `_docker_dispatch` streaming inner cmd. |
| `tests/test_orchestrator_core.py` (modify) | Pure-function unit tests (appended). |
| `tests/test_server_chat.py` (create) | Endpoint tests (server-thread harness style of `test_server_orchestrator.py`). |
| `tests/test_chat_pump.py` (create) | Pump integration tests with a stand-in `python -c` child. |
| `tests/test_chat_dispatch.py` (create) | `spawn_agent` chat-mode tests (fake Popen). |
| `tests/test_orchestrator_loop.py` (modify) | Update 4 existing `spawn_agent` tests to the stdin-prompt contract. |
| `tests/test_docker_workspace.py` (modify) | Update 3 existing spawn tests; add `-i`/inner-cmd/translated-prompt tests. |

---

### Task 1: Pure chat helpers + `CHAT_ENABLED` flag (Milestone A1)

**Files:**
- Modify: `orchestrator_core.py` (insert after `now_iso()`, ~line 181)
- Test: `tests/test_orchestrator_core.py` (append at end)

**Interfaces:**
- Consumes: existing `ORCH_DIR` module constant, `json`, `os` (already imported).
- Produces (used by Tasks 2–5):
  - `CHAT_ENABLED: bool` (module constant, `True`)
  - `CHAT_DIR: str` (= `os.path.join(ORCH_DIR, "chat")`)
  - `chat_inbox_path(board, ticket_id) -> str` — `<CHAT_DIR>/<board>__<id>.jsonl`; reads the module-global `CHAT_DIR` at call time (so tests can monkeypatch it)
  - `chat_encode_user_message(text) -> str` — one JSONL line ending in `"\n"`
  - `chat_parse_inbox_line(line) -> dict | None` — `{"message", "writer", "ts"}` or `None`
  - `chat_should_close(result_seen_after_last_send, inbox_empty) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orchestrator_core.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_orchestrator_core.py -q -k chat`
Expected: FAIL — `AttributeError: module 'orchestrator_core' has no attribute 'CHAT_ENABLED'` (and similar for each test).

- [ ] **Step 3: Write the implementation**

In `orchestrator_core.py`, insert after the `now_iso()` function (~line 181):

```python
# --- Agent chat (stdin injection) --------------------------------------------
#
# A human can message a RUNNING agent (spec docs/specs/2026-07-03-agent-chat-
# design.md): the kanban server appends lines to a per-run inbox file under
# CHAT_DIR and the orchestrator's per-run pump thread relays them to the agent
# process's stdin as stream-json user messages. The helpers here are pure so
# the server, the pump, and the tests share one encoding/decision
# implementation.

# One-line escape hatch: set False to restore the legacy argv-prompt dispatch
# (no streaming input, no pump threads; the chat endpoint then returns
# 409 {"error": "chat disabled"}).
CHAT_ENABLED = True

CHAT_DIR = os.path.join(ORCH_DIR, "chat")


def chat_inbox_path(board, ticket_id):
    """The chat inbox file for one ticket's run: <CHAT_DIR>/<board>__<id>.jsonl.

    Double underscore separates the board dir name from the id; both are
    filesystem-safe already (same convention as the idle sidecar files).
    Reads the module-global CHAT_DIR at call time so tests can repoint it.
    """
    return os.path.join(CHAT_DIR, f"{board}__{ticket_id}.jsonl")


def chat_encode_user_message(text):
    """One stream-json input line carrying *text* as a user message.

    This exact shape is what `claude -p --input-format stream-json` consumes;
    the initial prompt and every relayed chat message use it. Returns a single
    JSON line terminated by \\n.
    """
    return json.dumps(
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": text}]}},
        ensure_ascii=False) + "\n"


def chat_parse_inbox_line(line):
    """Parse one inbox JSONL line into {"message", "writer", "ts"}, or None.

    Malformed JSON, a non-dict payload, or a missing/empty/non-string
    `message` all return None — the pump skips such lines. A missing writer
    defaults to "unknown", a missing ts to "".
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    msg = obj.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return None
    return {"message": msg,
            "writer": str(obj.get("writer") or "unknown"),
            "ts": str(obj.get("ts") or "")}


def chat_should_close(result_seen_after_last_send, inbox_empty):
    """True when the pump should close the child's stdin, ending the run.

    Close only when (a) the CLI has emitted a top-level result SINCE the last
    user message we injected AND (b) no unsent inbox message is queued. If a
    chat message arrives before close, it is sent instead and the agent runs
    another turn; the next result re-arms the decision.
    """
    return bool(result_seen_after_last_send) and bool(inbox_empty)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestrator_core.py -q`
Expected: PASS (all, including pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add orchestrator_core.py tests/test_orchestrator_core.py
git commit -m "chat: pure inbox/encode/close helpers + CHAT_ENABLED flag"
```

---

### Task 2: Server endpoint `POST /api/orchestrator/chat/<board>/<id>` (Milestone A1)

**Files:**
- Modify: `kanban_server.py` — add `orch_chat()` after `orch_answer()` (~line 1236), add route in `do_POST` (~line 1563, after the answer route)
- Test: `tests/test_server_chat.py` (create)

**Interfaces:**
- Consumes (Task 1): `_oc.CHAT_ENABLED` (read at call time so monkeypatching works), `_oc.chat_inbox_path(board, task_id)`, `_oc.now_iso()`; existing `_ticket_file(board, task_id)`.
- Produces: `orch_chat(board, task_id, payload) -> (dict, int)` — same handler convention as `orch_kill`/`orch_answer`. Stores raw `{"message", "writer", "ts"}` — NO writer wrapping here (the pump wraps).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server_chat.py`:

```python
"""Tests for POST /api/orchestrator/chat/<board>/<id> (agent chat inbox).

Spec: docs/specs/2026-07-03-agent-chat-design.md, Component 2. The endpoint
appends one raw {"message","writer","ts"} JSONL line to the ticket's inbox
file; writer-attribution wrapping is the pump's job, not the server's.
"""
import json
import os
import threading
import http.client

import pytest

import kanban_server as ks
import orchestrator_core as oc


@pytest.fixture
def server(kanban, monkeypatch):
    # Point the server AND the chat dir at the temp kanban tree.
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    monkeypatch.setattr(oc, "KANBAN_DIR", kanban, raising=False)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, json.dumps(body) if body is not None else None, hdrs)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    return r.status, (json.loads(data) if data else None)


def _make_running(kanban, tid="1"):
    """Mark a ticket as a live run: dispatched marker + in_progress status."""
    p = os.path.join(kanban, "demo", f"{tid}.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "in_progress"
    t["orchestrator"] = {"state": "dispatched", "pid": 4242,
                         "killRequested": False}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    return p


def test_chat_appends_inbox_line(server, kanban):
    _make_running(kanban)
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                        {"message": "hello agent", "writer": "ryan"})
    assert status == 200
    assert body == {"ok": True}
    inbox = oc.chat_inbox_path("demo", "1")
    assert os.path.isfile(inbox)
    lines = open(inbox, encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    # Raw fields stored — no writer-attribution wrapping at the server.
    assert obj["message"] == "hello agent"
    assert obj["writer"] == "ryan"
    assert obj["ts"], "ts must be stamped"


def test_chat_appends_in_order(server, kanban):
    _make_running(kanban)
    _req(server, "POST", "/api/orchestrator/chat/demo/1",
         {"message": "first", "writer": "ryan"})
    _req(server, "POST", "/api/orchestrator/chat/demo/1",
         {"message": "second", "writer": "ryan"})
    lines = open(oc.chat_inbox_path("demo", "1"), encoding="utf-8").read().splitlines()
    assert [json.loads(l)["message"] for l in lines] == ["first", "second"]


def test_chat_404_unknown_ticket(server):
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/999",
                        {"message": "hi", "writer": "ryan"})
    assert status == 404
    assert body == {"error": "not found"}


def test_chat_400_empty_or_non_string_message(server, kanban):
    _make_running(kanban)
    for bad in ({"message": "", "writer": "r"},
                {"message": "   ", "writer": "r"},
                {"message": 42, "writer": "r"},
                {"writer": "r"}):
        status, _ = _req(server, "POST", "/api/orchestrator/chat/demo/1", bad)
        assert status == 400, f"payload {bad!r} must be rejected with 400"
    assert not os.path.exists(oc.chat_inbox_path("demo", "1")), \
        "rejected messages must not touch the inbox"


def test_chat_409_when_not_running(server, kanban):
    # Default fixture ticket: status todo, no orchestrator marker.
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                        {"message": "hi", "writer": "ryan"})
    assert status == 409
    assert body == {"error": "not running"}

    # Dispatched marker but wrong status (e.g. blocked) is also "not running".
    p = os.path.join(kanban, "demo", "2.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    t["status"] = "blocked"
    t["orchestrator"] = {"state": "dispatched", "pid": 1}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(t, f)
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/2",
                        {"message": "hi", "writer": "ryan"})
    assert status == 409
    assert body == {"error": "not running"}


def test_chat_409_when_chat_disabled(server, kanban, monkeypatch):
    _make_running(kanban)
    monkeypatch.setattr(oc, "CHAT_ENABLED", False)
    status, body = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                        {"message": "hi", "writer": "ryan"})
    assert status == 409
    assert body == {"error": "chat disabled"}
    assert not os.path.exists(oc.chat_inbox_path("demo", "1"))


def test_chat_cross_origin_requires_token(server, kanban):
    # Same _authorized() gate as every other state-changing route.
    _make_running(kanban)
    status, _ = _req(server, "POST", "/api/orchestrator/chat/demo/1",
                     {"message": "hi", "writer": "ryan"},
                     headers={"Origin": "http://evil.example.com"})
    assert status == 403
    assert not os.path.exists(oc.chat_inbox_path("demo", "1"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_server_chat.py -q`
Expected: FAIL — every test gets a 404 from the unmatched route (`send_error(404)` returns an HTML body, so `_req` raises `json.JSONDecodeError` or the status assertion fails). Either failure mode is the correct RED signal.

- [ ] **Step 3: Write the implementation**

In `kanban_server.py`, add after `orch_answer()` (~line 1236):

```python
def orch_chat(board, task_id, payload):
    """Append a chat message to a RUNNING ticket's inbox (agent chat).

    Spec: docs/specs/2026-07-03-agent-chat-design.md, Component 2. Stores the
    raw {"message","writer","ts"} fields — the orchestrator pump does the
    writer-attribution wrapping when it relays to the agent's stdin. Does NOT
    check the PID is alive: that race belongs to the pump/reap side (a message
    posted just as the run dies is silently dropped with the inbox file).
    """
    path = _ticket_file(board, task_id)
    if path is None or not os.path.isfile(path):
        return {"error": "not found"}, 404
    msg = (payload or {}).get("message")
    if not isinstance(msg, str) or not msg.strip():
        return {"error": "message must be a non-empty string"}, 400
    try:
        with open(path, "r", encoding="utf-8") as f:
            task = json.load(f)
    except (OSError, ValueError):
        return {"error": "could not read ticket"}, 500
    marker = task.get("orchestrator") or {}
    if not (marker.get("state") == "dispatched"
            and task.get("status") == "in_progress"):
        return {"error": "not running"}, 409
    if not _oc.CHAT_ENABLED:
        return {"error": "chat disabled"}, 409
    writer = str((payload or {}).get("writer") or "unknown")
    line = json.dumps({"message": msg, "writer": writer, "ts": _oc.now_iso()},
                      ensure_ascii=False) + "\n"
    inbox = _oc.chat_inbox_path(board, task_id)
    try:
        os.makedirs(os.path.dirname(inbox), exist_ok=True)
        # Single write of one whole line + flush: the pump tails by byte
        # offset and only consumes complete lines, so this append is atomic
        # enough — nothing partial is ever relayed.
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError:
        return {"error": "could not write inbox"}, 500
    return {"ok": True}, 200
```

In `do_POST` (~line 1563), insert between the `answer` route and the `nudge` route:

```python
        # POST /api/orchestrator/chat/<board>/<id> — message a running agent
        elif len(parts) == 6 and parts[1] == "api" and parts[2] == "orchestrator" and parts[3] == "chat":
            payload = self._read_json()
            if payload is None:
                return
            self._json(*orch_chat(unquote(parts[4]), unquote(parts[5]), payload))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_server_chat.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Run the full suite (A1 milestone gate)**

Run: `python -m pytest tests -q`
Expected: PASS — no pre-existing test touches the new route or constants.

- [ ] **Step 6: Commit**

```bash
git add kanban_server.py tests/test_server_chat.py
git commit -m "chat: POST /api/orchestrator/chat/<board>/<id> inbox endpoint"
```

---

### Task 3: Pump thread machinery + reap cleanup (Milestone A2)

**Files:**
- Modify: `orchestrator.py` — add `import threading` (stdlib import block, after `import sys`), add `_PUMPS` next to `_PROCS` (~line 25), add `_tail_new_lines` / `_chat_pump` / `_start_chat_pump` after `_kill_container` (~line 213), extend `_release_proc` (~line 215)
- Test: `tests/test_chat_pump.py` (create)

**Interfaces:**
- Consumes (Task 1): `oc.chat_parse_inbox_line`, `oc.chat_encode_user_message`, `oc.chat_should_close`.
- Produces (used by Task 4):
  - `_PUMPS: dict[int, dict]` — pid → `{"thread": Thread, "inbox": str}`
  - `_tail_new_lines(path, offset) -> (list[str], int)` — new complete lines past byte `offset` + new offset (partial trailing line not consumed)
  - `_chat_pump(proc, inbox_path, log_path, poll_seconds=1.0) -> None` — the thread body
  - `_start_chat_pump(proc, inbox_path, log_path) -> Thread` — creates + registers + starts the daemon thread (the mockable seam `spawn_agent` calls)
  - `_release_proc(pid)` — now ALSO pops the `_PUMPS` entry and deletes its inbox file

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chat_pump.py`:

```python
"""Integration tests for the orchestrator chat pump thread.

Spec: docs/specs/2026-07-03-agent-chat-design.md, Component 4. A stand-in
child process (`python -c`) plays the claude CLI: it echoes every stdin line
to a file and exits 0 on stdin EOF. No real claude, no tokens.
"""
import json
import os
import subprocess
import sys
import threading
import time

import orchestrator as orch
import orchestrator_core as oc

# Echo child: writes each stdin line to the file at argv[1], exits 0 on EOF.
_CHILD = (
    "import sys\n"
    "out = open(sys.argv[1], 'w', encoding='utf-8')\n"
    "for line in sys.stdin:\n"
    "    out.write(line)\n"
    "    out.flush()\n"
    "out.close()\n"
)


def _spawn_child(echo_path):
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _CHILD, str(echo_path)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


def _append_inbox(inbox, message, writer="alice"):
    """Append one line exactly the way the server endpoint does."""
    os.makedirs(os.path.dirname(inbox), exist_ok=True)
    with open(inbox, "a", encoding="utf-8") as f:
        f.write(json.dumps({"message": message, "writer": writer,
                            "ts": oc.now_iso()}, ensure_ascii=False) + "\n")
        f.flush()


def _run_pump(proc, inbox, log):
    th = threading.Thread(target=orch._chat_pump,
                          args=(proc, str(inbox), str(log)),
                          kwargs={"poll_seconds": 0.05}, daemon=True)
    th.start()
    return th


def _wait(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_tail_new_lines_only_consumes_complete_lines(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a": 1}\n{"partial', encoding="utf-8")
    lines, off = orch._tail_new_lines(str(p), 0)
    assert lines == ['{"a": 1}']
    with open(p, "a", encoding="utf-8") as f:
        f.write('!}\n')
    lines2, off2 = orch._tail_new_lines(str(p), off)
    assert lines2 == ['{"partial!}']
    assert off2 == os.path.getsize(p)
    # No new content: nothing returned, offset unchanged.
    lines3, off3 = orch._tail_new_lines(str(p), off2)
    assert lines3 == [] and off3 == off2


def test_tail_new_lines_missing_file(tmp_path):
    lines, off = orch._tail_new_lines(str(tmp_path / "nope.jsonl"), 0)
    assert lines == [] and off == 0


def test_pump_delivers_messages_in_order_then_closes(tmp_path):
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    proc = _spawn_child(echo)
    try:
        th = _run_pump(proc, inbox, log)
        _append_inbox(str(inbox), "first question", writer="ryan")
        _append_inbox(str(inbox), "second question", writer="ryan")

        def _delivered():
            try:
                return len(echo.read_text(encoding="utf-8").splitlines()) >= 2
            except OSError:
                return False
        assert _wait(_delivered), "messages never reached the child's stdin"
        lines = echo.read_text(encoding="utf-8").splitlines()
        texts = [json.loads(l)["message"]["content"][0]["text"] for l in lines]
        # Delivery order preserved; each wrapped with writer attribution.
        assert texts == ["[Message from ryan via Discord]\nfirst question",
                         "[Message from ryan via Discord]\nsecond question"]
        # Every relayed line is a well-formed stream-json user message.
        for l in lines:
            obj = json.loads(l)
            assert obj["type"] == "user"
            assert obj["message"]["role"] == "user"

        # A result line NEWER than the last send arms the close decision;
        # the pump closes stdin, the child sees EOF and exits cleanly.
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "result", "subtype": "success"}) + "\n")
        assert _wait(lambda: proc.poll() is not None), \
            "child did not exit after stdin close"
        assert proc.returncode == 0
        th.join(timeout=5)
        assert not th.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_pump_closes_on_result_with_empty_inbox(tmp_path):
    # No inbox file at all == nothing queued: result alone closes the run.
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"   # never created
    log = tmp_path / "run.log"
    log.write_text(json.dumps({"type": "result"}) + "\n", encoding="utf-8")
    proc = _spawn_child(echo)
    try:
        th = _run_pump(proc, inbox, log)
        assert _wait(lambda: proc.poll() is not None)
        assert proc.returncode == 0
        th.join(timeout=5)
        assert not th.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_pump_does_not_close_before_result(tmp_path):
    # No result line in the log: stdin must stay open (the agent is mid-run).
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text(json.dumps({"type": "assistant",
                               "message": {"content": []}}) + "\n",
                   encoding="utf-8")
    proc = _spawn_child(echo)
    try:
        _run_pump(proc, inbox, log)
        time.sleep(0.5)
        assert proc.poll() is None, "pump must not close stdin before a result"
    finally:
        proc.kill()
        proc.wait()


def test_pump_skips_malformed_inbox_lines(tmp_path):
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    os.makedirs(os.path.dirname(str(inbox)), exist_ok=True)
    with open(inbox, "w", encoding="utf-8") as f:
        f.write("{not json at all\n")                      # malformed: skipped
        f.write(json.dumps({"message": ""}) + "\n")        # empty: skipped
    proc = _spawn_child(echo)
    try:
        _run_pump(proc, inbox, log)
        _append_inbox(str(inbox), "real one", writer="bob")

        def _delivered():
            try:
                return len(echo.read_text(encoding="utf-8").splitlines()) >= 1
            except OSError:
                return False
        assert _wait(_delivered), "pump died on a malformed line"
        lines = echo.read_text(encoding="utf-8").splitlines()
        texts = [json.loads(l)["message"]["content"][0]["text"] for l in lines]
        assert texts == ["[Message from bob via Discord]\nreal one"]
    finally:
        proc.kill()
        proc.wait()


def test_pump_deletes_inbox_and_exits_when_child_dies(tmp_path):
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    _append_inbox(str(inbox), "pending")   # a queued message dies with the run
    proc = _spawn_child(echo)
    proc.kill()
    proc.wait()
    th = _run_pump(proc, inbox, log)
    assert _wait(lambda: not th.is_alive()), "pump must exit when child died"
    assert not os.path.exists(str(inbox)), "pump must delete the inbox on child death"


def test_pump_treats_broken_pipe_as_child_death(tmp_path):
    # Deterministic broken pipe via a fake proc whose stdin write raises.
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    _append_inbox(str(inbox), "boom")

    class _BrokenStdin:
        def write(self, data):
            raise OSError("broken pipe")

        def flush(self):
            pass

        def close(self):
            pass

    class _FakeProc:
        stdin = _BrokenStdin()

        def poll(self):
            return None  # "alive" — only the write reveals the death

    th = threading.Thread(target=orch._chat_pump,
                          args=(_FakeProc(), str(inbox), str(log)),
                          kwargs={"poll_seconds": 0.05}, daemon=True)
    th.start()
    assert _wait(lambda: not th.is_alive()), \
        "broken pipe must terminate the pump like a child death"
    assert not os.path.exists(str(inbox))


def test_release_proc_drops_pump_and_deletes_inbox(tmp_path):
    inbox = tmp_path / "demo__1.jsonl"
    inbox.write_text("x\n", encoding="utf-8")

    class _DeadThread:
        def is_alive(self):
            return False

    pid = 987654
    orch._PUMPS[pid] = {"thread": _DeadThread(), "inbox": str(inbox)}
    orch._release_proc(pid)   # pid not in _PROCS: must still clean the pump
    assert pid not in orch._PUMPS
    assert not inbox.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_chat_pump.py -q`
Expected: FAIL — `AttributeError: module 'orchestrator' has no attribute '_tail_new_lines'` (and `_chat_pump`, `_PUMPS`).

- [ ] **Step 3: Write the implementation**

In `orchestrator.py`:

(a) Add `import threading` to the stdlib import block (between `import sys` and `import time`):

```python
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
```

(b) Below the `_PROCS = {}` definition (~line 25), add:

```python
# Registry of chat pump threads for processes we spawned (agent chat, spec
# docs/specs/2026-07-03-agent-chat-design.md). Maps pid (int) ->
# {"thread": Thread, "inbox": str} so reap/shutdown can join the thread and
# delete the run's inbox file. Pumps are daemons: a crashed loop never blocks
# orchestrator exit.
_PUMPS = {}
```

(c) After `_kill_container` (~line 213), add:

```python
# --- Agent chat pump (spec docs/specs/2026-07-03-agent-chat-design.md) ------


def _tail_new_lines(path, offset):
    """New complete lines appended to *path* past byte *offset*.

    Returns (lines, new_offset). Only lines terminated by \\n are consumed —
    a partial trailing line stays unconsumed (offset not advanced past it) so
    the next poll picks it up once its writer finishes. A missing/unreadable
    file returns ([], offset) — retry next loop, never crash.
    """
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    text = data.decode("utf-8", errors="replace")
    parts = text.split("\n")
    remainder = "" if text.endswith("\n") else parts[-1]
    complete = parts[:-1]
    consumed = len(data) - len(remainder.encode("utf-8"))
    return [ln for ln in complete if ln.strip()], offset + consumed


def _chat_pump(proc, inbox_path, log_path, poll_seconds=1.0):
    """Per-run daemon thread body: relay inbox messages to the child's stdin.

    Loop (~1s):
      1. Child died?            -> delete the inbox file, exit.
      2. New inbox lines?       -> write each to child stdin as a stream-json
         user message wrapped `[Message from <writer> via Discord]\\n<msg>`;
         a broken pipe on write is treated as child death (step 1).
      3. Result in the run log? -> arms the close decision.
      4. chat_should_close      -> close child stdin (the CLI exits after its
         queued input; the normal reap path then proceeds), exit the thread.

    Never lets an exception kill the loop except through the exit paths.
    """
    inbox_off = 0
    log_off = 0
    result_seen_after_last_send = False
    while True:
        if proc.poll() is not None:
            # Child died (finished, killed, or crashed): drop the inbox —
            # messages posted just as the run died are silently dropped.
            try:
                os.remove(inbox_path)
            except OSError:
                pass
            return
        try:
            # 2. Deliver any new inbox lines to the child's stdin.
            lines, inbox_off = _tail_new_lines(inbox_path, inbox_off)
            for raw in lines:
                parsed = oc.chat_parse_inbox_line(raw)
                if parsed is None:
                    continue  # malformed line: skip it
                wrapped = (f"[Message from {parsed['writer']} via Discord]\n"
                           f"{parsed['message']}")
                try:
                    proc.stdin.write(
                        oc.chat_encode_user_message(wrapped).encode("utf-8"))
                    proc.stdin.flush()
                except (OSError, ValueError):
                    # Broken pipe: the child is dead/dying. Same cleanup as
                    # step 1; the reap path handles the ticket.
                    try:
                        os.remove(inbox_path)
                    except OSError:
                        pass
                    return
                result_seen_after_last_send = False
            # 3. Watch the run log for the CLI's top-level result lines.
            log_lines, log_off = _tail_new_lines(log_path, log_off)
            for raw in log_lines:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "result":
                    result_seen_after_last_send = True
            # 4. Close stdin once the agent is done and nothing is queued.
            try:
                inbox_empty = os.path.getsize(inbox_path) <= inbox_off
            except OSError:
                inbox_empty = True  # no inbox file = nothing queued
            if oc.chat_should_close(result_seen_after_last_send, inbox_empty):
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                return
        except Exception:
            # Inbox/log transiently unreadable, etc: retry next iteration.
            pass
        time.sleep(poll_seconds)


def _start_chat_pump(proc, inbox_path, log_path):
    """Create, register (next to _PROCS), and start a run's chat pump thread.

    A module-level seam so spawn_agent tests can monkeypatch it away without
    threading real pumps around fake Popen objects.
    """
    th = threading.Thread(target=_chat_pump, args=(proc, inbox_path, log_path),
                          name=f"chat-pump-{proc.pid}", daemon=True)
    _PUMPS[proc.pid] = {"thread": th, "inbox": inbox_path}
    th.start()
    return th
```

(d) Replace `_release_proc` (~line 215) with:

```python
def _release_proc(pid):
    """Close the log handle, drop the pid from the registries, delete the
    run's chat inbox.

    Call this whenever we are done with a dispatched process (killed, reaped,
    crashed, completed, needs_human, stop-all).  Safe to call even if the pid
    is not in the registries (no-op).  Closing stdin here also unblocks a
    still-waiting CLI (belt-and-braces alongside the pump's own close).
    """
    p = _PROCS.pop(pid, None)
    if p is not None:
        log_f = getattr(p, "_log_f", None)
        if log_f is not None:
            try:
                log_f.close()
            except OSError:
                pass
        stdin = getattr(p, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
    pump = _PUMPS.pop(pid, None)
    if pump is not None:
        try:
            os.remove(pump["inbox"])
        except OSError:
            pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_chat_pump.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Run the neighbouring suites for regressions**

Run: `python -m pytest tests/test_orchestrator_loop.py tests/test_kill_switch.py tests/test_docker_workspace.py -q`
Expected: PASS — `_release_proc` changes are additive (`getattr(..., "stdin", None)` tolerates fake procs without stdin).

- [ ] **Step 6: Commit**

```bash
git add orchestrator.py tests/test_chat_pump.py
git commit -m "chat: per-run stdin pump thread, _PUMPS registry, inbox cleanup on release"
```

---

### Task 4: Host streaming-input dispatch (Milestone A2)

**Files:**
- Modify: `orchestrator.py` — `spawn_agent` (~lines 1009–1095)
- Modify: `tests/test_orchestrator_loop.py` — 4 spawn tests (lines ~296, ~566, ~629, ~675)
- Modify: `tests/test_docker_workspace.py` — `test_spawn_agent_non_docker_has_no_container` (~line 235)
- Test: `tests/test_chat_dispatch.py` (create)

**Interfaces:**
- Consumes: Task 1 (`oc.CHAT_ENABLED`, `oc.chat_inbox_path`, `oc.chat_encode_user_message`), Task 3 (`_start_chat_pump`).
- Produces: `spawn_agent` in chat mode spawns
  `[claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", *session_flag, (--model m), (--allowedTools ...)]`
  with `stdin=subprocess.PIPE`, writes the prompt as the first stream-json user message, deletes any stale inbox BEFORE spawn, and starts the pump. In this task chat wiring is **host-only** (`container_name is None`); Task 5 extends it to Docker. Legacy form when `CHAT_ENABLED` is False is byte-for-byte unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chat_dispatch.py`:

```python
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
            "_path": os.path.join(kanban, "demo", "1.json")}


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_chat_dispatch.py -q`
Expected: FAIL — `assert cmd[2:4] == ["--input-format", "stream-json"]` fails (cmd[2] is the prompt today); the legacy test may already pass (that is fine — it pins the escape hatch).

- [ ] **Step 3: Write the implementation**

In `orchestrator.py`, replace `spawn_agent` (currently ~lines 1009–1095) with:

```python
def spawn_agent(kanban_dir, board, task, profile, model):
    # Resolve to an absolute path so the run dir and cwd are always valid.
    # (os.path.dirname(".") is "" — an invalid cwd that raises WinError 123.)
    kanban_dir = os.path.abspath(kanban_dir)
    runs_dir = os.path.join(kanban_dir, "_orchestrator", "runs")
    os.makedirs(runs_dir, exist_ok=True)
    ts = oc.now_iso().replace(":", "").replace("-", "")
    log_name = f"{task['id']}-{ts}.log"
    log_path = os.path.join(runs_dir, log_name)

    # The sub-agent runs from the repo root (parent of .kanban) so it can see
    # the .kanban/<board>/<id>.json paths in its prompt. Fall back to the
    # kanban dir itself if there is no parent.
    cwd = os.path.dirname(kanban_dir) or kanban_dir

    # Session handling has two modes (ticket #13):
    #   - Unblock/resume: a ticket that already ran once and is now being
    #     re-dispatched after a human answered its block should CONTINUE its
    #     existing session (`claude --resume <id>`) so it keeps the context it had
    #     before it blocked, rather than restarting fresh. The prompt then only
    #     carries the reason it was unblocked (the human answer).
    #   - Fresh dispatch: mint the sub-agent's session id up front and pass it to
    #     the CLI with --session-id, rather than scraping it from stdout. The
    #     orchestrator then knows the id deterministically and records it on the
    #     ticket, so a human can `claude --resume <id>` to take over manually.
    resume_id = oc.resume_session_id(task)
    resuming = bool(resume_id)
    session_id = resume_id if resuming else str(uuid.uuid4())

    # Fable 5 may not always be available; probe and fall back to opus if needed.
    if model == oc.FABLE_MODEL:
        model = oc.resolve_model(model, fable_available=_probe_fable_available())

    board_meta = oc.read_board_meta(kanban_dir, board)
    prompt = (_build_resume_prompt(task, profile, board_meta) if resuming
              else _build_agent_prompt(task, profile, board_meta))
    allowed = profile.get("allowedTools")

    log_f = open(log_path, "w", encoding="utf-8")

    # Two dispatch modes (ticket #16):
    #   - Docker (per-board `useDocker`): build the board's workspace image and run
    #     the agent INSIDE a container mounted on the workspace root, with the
    #     board's editable env vars supplied via `--env-file`.
    #   - Default: run `claude -p` as a plain host subprocess in the workspace root.
    container_name = None
    stdin_prompt = prompt  # what the first stream-json user message will carry
    if oc.use_docker(board_meta):
        cmd, container_name = _docker_dispatch(
            kanban_dir, board, task, board_meta, prompt, session_id, model,
            allowed, log_f, resuming=resuming)
    else:
        # --resume reattaches the prior session (context preserved); --session-id
        # mints a fresh one. The two are mutually exclusive.
        session_flag = (["--resume", session_id] if resuming
                        else ["--session-id", session_id])
        if oc.CHAT_ENABLED:
            # Agent chat (spec 2026-07-03): the prompt is NOT argv — it is
            # written to stdin as the first stream-json user message, and
            # later chat messages follow on the same pipe.
            cmd = [_claude_cmd(), "-p", "--input-format", "stream-json",
                   "--output-format", "stream-json", "--verbose", *session_flag]
        else:
            # Legacy escape hatch: byte-for-byte the pre-chat dispatch form.
            cmd = [_claude_cmd(), "-p", prompt, *session_flag,
                   "--output-format", "stream-json", "--verbose"]
        if model:
            cmd += ["--model", model]
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]

    # Chat wiring is host-only for now; the Docker task extends it (the inner
    # container claude does not read streaming input yet).
    chat = oc.CHAT_ENABLED and container_name is None

    inbox_path = oc.chat_inbox_path(board, task["id"])
    if chat:
        # Stale messages from a previous run must never leak into this run.
        try:
            os.remove(inbox_path)
        except OSError:
            pass

    # On POSIX, put the agent in its own session/process group so kill_pid can
    # take down the whole child tree via os.killpg (Windows uses taskkill /T).
    popen_kw = {} if sys.platform == "win32" else {"start_new_session": True}
    if chat:
        popen_kw["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=cwd,
                            **popen_kw)
    # Stash log handle on proc so _release_proc can close it on reap.
    proc._log_f = log_f
    # Register the Popen so _exit_code / _process_alive can use it.
    _PROCS[proc.pid] = proc
    if chat:
        try:
            proc.stdin.write(
                oc.chat_encode_user_message(stdin_prompt).encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            pass  # child died instantly; the normal reap path handles it
        _start_chat_pump(proc, inbox_path, log_path)
    marker = {
        "state": "dispatched",
        "profile": profile.get("name"),
        "model": model,
        "pid": proc.pid,
        "sessionId": session_id,
        "cwd": cwd,
        "dispatchedAt": oc.now_iso(),
        "killRequested": False,
        "logFile": f".kanban/_orchestrator/runs/{log_name}",
    }
    # Record the container name so reap/kill can `docker kill` it by name even
    # after a loop restart (when we no longer hold the client Popen).
    if container_name:
        marker["containerName"] = container_name
    return marker
```

- [ ] **Step 4: Run the new tests**

Run: `python -m pytest tests/test_chat_dispatch.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Update the 4 existing spawn tests in `tests/test_orchestrator_loop.py`**

They now break because chat mode passes an extra `stdin=` kwarg to Popen, writes to `proc.stdin`, and starts a pump. Add `import io` to the imports at the top of `tests/test_orchestrator_loop.py`, then apply these changes:

Replace `test_spawn_agent_uses_valid_cwd` (~line 296) with:

```python
def test_spawn_agent_uses_valid_cwd(kanban, monkeypatch):
    """spawn_agent must launch with a valid working directory even when
    kanban_dir is given relatively. Regression for WinError 123 caused by
    cwd=os.path.dirname('.') == '' (an invalid directory)."""
    captured = {}

    class FakeProc:
        pid = 4242
        stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

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
```

Replace `test_spawn_agent_sets_resumable_session_id` (~line 566) with:

```python
def test_spawn_agent_sets_resumable_session_id(kanban, monkeypatch):
    """spawn_agent must mint a session id, pass it to the CLI via --session-id,
    and return it on the marker so the ticket can record a resumable id."""
    captured = {}

    class FakeProc:
        pid = 7777
        stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

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
```

Replace `test_spawn_agent_resumes_prior_session_on_unblock` (~line 629) with (the prompt now travels via stdin, so the prompt assertion moves there):

```python
def test_spawn_agent_resumes_prior_session_on_unblock(kanban, monkeypatch):
    """Re-dispatching an unblocked ticket that already ran once must RESUME its
    existing session via `--resume <sid>` — not mint a fresh `--session-id` — so
    the agent keeps the context it had before it blocked. The resume prompt
    (now delivered via stdin in chat mode) must carry the human's answer."""
    captured = {}

    class FakeProc:
        pid = 9191

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cmd"] = cmd
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    task = {"id": "1", "title": "x", "detail": "the detail", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json"),
            "status": "blocked", "claudeSessionId": "prior-sess",
            "orchestrator": {"state": "blocked",
                             "question": {"id": "q1", "prompt": "which option?",
                                          "answer": {"value": "option A",
                                                     "notes": "go ahead"}}}}
    profile = {"name": "general", "systemPrompt": "p", "model": "m"}

    marker = orch.spawn_agent(kanban, "demo", task, profile, "m")

    cmd = captured["cmd"]
    assert "--resume" in cmd, f"unblock must resume the prior session, got {cmd}"
    assert cmd[cmd.index("--resume") + 1] == "prior-sess"
    assert "--session-id" not in cmd, "resume must not also mint a new session id"
    # The marker keeps the resumed id so the ticket keeps pointing at one session.
    assert marker.get("sessionId") == "prior-sess"
    # The resume prompt is the first stream-json user message on stdin and
    # must convey the unblock reason (the human answer).
    raw = captured["proc"].stdin.getvalue().decode("utf-8")
    prompt = json.loads(raw.splitlines()[0])["message"]["content"][0]["text"]
    assert "option A" in prompt and "go ahead" in prompt, (
        f"resume prompt must carry the human answer, got: {prompt}"
    )
```

Replace `test_spawn_agent_fresh_dispatch_still_mints_session` (~line 675) with:

```python
def test_spawn_agent_fresh_dispatch_still_mints_session(kanban, monkeypatch):
    """A ticket with no prior session (first dispatch) must still start fresh with
    a minted --session-id — the resume path must not swallow the normal path."""
    captured = {}

    class FakeProc:
        pid = 9292
        stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    profile = {"name": "general", "systemPrompt": "p", "model": "m"}

    marker = orch.spawn_agent(kanban, "demo", task, profile, "m")
    cmd = captured["cmd"]
    assert "--session-id" in cmd and "--resume" not in cmd
    assert cmd[cmd.index("--session-id") + 1] == marker["sessionId"]
```

- [ ] **Step 6: Update `test_spawn_agent_non_docker_has_no_container` in `tests/test_docker_workspace.py` (~line 235)**

This test uses the default (non-docker) board, so chat mode now runs. Add `import io` to the file's imports if not present, then replace the test with:

```python
def test_spawn_agent_non_docker_has_no_container(kanban, monkeypatch):
    # Regression: default board (no useDocker) still runs a plain claude subprocess.
    class FakeProc:
        pid = 8888
        stdin = io.BytesIO()

        def poll(self):
            return None

    captured = {}

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    marker = orch.spawn_agent(kanban, "demo", task,
                              {"name": "g", "systemPrompt": "p"}, "m")
    assert "containerName" not in marker
    assert captured["cmd"][0] != "docker"
    assert captured["cmd"][1] == "-p"
```

- [ ] **Step 7: Run the affected suites**

Run: `python -m pytest tests/test_chat_dispatch.py tests/test_orchestrator_loop.py tests/test_docker_workspace.py tests/test_chat_pump.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add orchestrator.py tests/test_chat_dispatch.py tests/test_orchestrator_loop.py tests/test_docker_workspace.py
git commit -m "chat: streaming-input host dispatch (prompt via stdin, stale-inbox truncation, pump start)"
```

---

### Task 5: Docker streaming-input dispatch + full-suite gate (Milestone A2)

**Files:**
- Modify: `orchestrator_core.py` — `docker_run_argv` (~line 465)
- Modify: `orchestrator.py` — `_docker_dispatch` (~line 950) and the `spawn_agent` docker call site / `chat` gate from Task 4
- Modify: `tests/test_docker_workspace.py` — argv-shape test additions, 2 spawn tests updated, 2 new tests

**Interfaces:**
- Consumes: Tasks 1, 3, 4.
- Produces:
  - `oc.docker_run_argv(..., interactive=False)` — `interactive=True` inserts `-i` right after `["docker", "run", "--rm"]`
  - `_docker_dispatch(...) -> (cmd, container_name, inner_prompt)` — now a 3-tuple; `inner_prompt` is the host-path-translated prompt for the first stdin message
  - `spawn_agent` chat gate becomes plain `oc.CHAT_ENABLED` (docker included)

- [ ] **Step 1: Write the failing tests**

In `tests/test_docker_workspace.py`, after `test_docker_run_argv_without_envfile` (~line 136), add:

```python
def test_docker_run_argv_interactive_adds_stdin_flag():
    # Agent chat needs the container's stdin attached to the docker run client.
    argv = oc.docker_run_argv("img", "c", "/r", None, ["claude"], interactive=True)
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    # Default stays non-interactive (legacy shape untouched).
    argv2 = oc.docker_run_argv("img", "c", "/r", None, ["claude"])
    assert "-i" not in argv2
```

At the end of the file, add:

```python
# --- Agent chat in docker mode (spec 2026-07-03) -----------------------------


def test_spawn_agent_docker_chat_streams_translated_prompt(kanban, monkeypatch):
    """Docker chat dispatch: `docker run -i`, inner claude gets
    --input-format stream-json with NO prompt argv, and the first stdin
    message carries the host-path-TRANSLATED prompt."""
    _docker_board(kanban)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    captured = {}

    class FakeProc:
        pid = 7779

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        captured["stdin"] = k.get("stdin")
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")

    cmd = captured["cmd"]
    assert cmd[:4] == ["docker", "run", "--rm", "-i"], \
        "docker chat mode must keep stdin attached with -i"
    # Inner claude reads streaming input; the prompt is NOT in argv.
    tag = oc.docker_image_tag("demo")
    inner = cmd[cmd.index(tag) + 1:]
    assert inner[0] == "claude" and inner[1] == "-p"
    assert inner[2:4] == ["--input-format", "stream-json"]
    assert not any(kanban in str(a) for a in inner), \
        "no host path (i.e. no prompt) may remain in the inner argv"
    # The first stdin message is the TRANSLATED prompt (host paths -> /workspace).
    assert captured["stdin"] is orch.subprocess.PIPE
    raw = captured["proc"].stdin.getvalue().decode("utf-8")
    text = json.loads(raw.splitlines()[0])["message"]["content"][0]["text"]
    assert "/workspace" in text
    assert kanban not in text


def test_spawn_agent_docker_chat_disabled_keeps_legacy_inner_cmd(kanban, monkeypatch):
    """CHAT_ENABLED=False: docker dispatch is byte-for-byte the legacy form —
    prompt in the inner argv, no -i, no stdin pipe, no pump."""
    _docker_board(kanban)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(oc, "CHAT_ENABLED", False)
    pumps = []
    monkeypatch.setattr(orch, "_start_chat_pump",
                        lambda *a, **k: pumps.append(a))
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    captured = {}

    class FakeProc:
        pid = 7780

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        captured["stdin"] = k.get("stdin")
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")

    cmd = captured["cmd"]
    assert "-i" not in cmd
    tag = oc.docker_image_tag("demo")
    inner = cmd[cmd.index(tag) + 1:]
    assert inner[0] == "claude" and inner[1] == "-p"
    assert "/workspace" in inner[2], "legacy form keeps the translated prompt in argv"
    assert "--input-format" not in inner
    assert captured["stdin"] is None
    assert captured["proc"].stdin.getvalue() == b""
    assert pumps == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_docker_workspace.py -q -k "interactive or chat"`
Expected: FAIL — `TypeError: docker_run_argv() got an unexpected keyword argument 'interactive'`; the chat test fails on `cmd[:4]` (no `-i`) / `ValueError: too many values to unpack` is NOT expected yet (implementation not changed).

- [ ] **Step 3: Write the implementation**

(a) In `orchestrator_core.py`, replace `docker_run_argv` (~line 465) with:

```python
def docker_run_argv(image_tag, container_name, mount_src, env_file, inner_argv,
                    container_workdir=CONTAINER_WORKSPACE, passthrough_env=None,
                    interactive=False):
    """The `docker run` argv that runs `inner_argv` inside the workspace image.

    - `--rm` so the container is discarded on exit (its work is on the mounted
      volume, persisted to the host).
    - `-i` (when `interactive`) keeps the container's stdin attached to the
      host `docker run` client so agent chat can stream stream-json input
      through it (spec 2026-07-03).
    - `--name` fixes the container name so reap can `docker kill` it by name.
    - `-v mount_src:/workspace` mounts the whole workspace root, giving the agent
      both its board repo and the `.AI-kanban` tree (its ticket JSON lives there).
    - `--env-file` supplies the board's editable env vars.
    - `passthrough_env` names host env vars to forward with bare `-e NAME`
      (value inherited from the orchestrator's own environment) — used for
      secrets like `ANTHROPIC_API_KEY` that shouldn't be written into _meta.json.
    """
    argv = ["docker", "run", "--rm"]
    if interactive:
        argv.append("-i")
    argv += ["--name", container_name,
             "-v", f"{mount_src}:{CONTAINER_WORKSPACE}",
             "-w", container_workdir]
    if env_file:
        argv += ["--env-file", env_file]
    for name in (passthrough_env or []):
        argv += ["-e", name]
    argv.append(image_tag)
    argv += list(inner_argv)
    return argv
```

(b) In `orchestrator.py`, replace `_docker_dispatch` (~line 950) with (docstring unchanged except the added final paragraph):

```python
def _docker_dispatch(kanban_dir, board, task, board_meta, prompt, session_id,
                     model, allowed, log_f, resuming=False):
    """Build the `docker run` argv (and container name) for an in-container agent.

    Mounts the workspace root at `/workspace`, so the agent sees both its board
    repo and the `.AI-kanban` tree; the prompt's host paths are translated onto
    that mount. The board's editable env vars ride in via `--env-file`, and secret
    env vars are forwarded by NAME from the orchestrator's own environment: the
    hardcoded Anthropic credentials plus the board's `passthroughEnv` names
    (ticket #7). No secret value is ever written to `_meta.json`, the env-file, or
    the image — only the name travels, `docker run -e NAME` inherits the value.

    Returns (cmd, container_name, inner_prompt). With CHAT_ENABLED the inner
    claude reads `--input-format stream-json` from an attached stdin
    (`docker run -i`) and `inner_prompt` — the host-path-translated prompt —
    is what spawn_agent writes as the first stdin user message. With
    CHAT_ENABLED off the legacy argv-prompt form is emitted and inner_prompt
    is unused.
    """
    _build_docker_image(kanban_dir, board, log_f)
    env_file = _write_board_env_file(kanban_dir, board, board_meta)
    mount_src = _repo_root(kanban_dir)  # workspace root -> /workspace
    container_name = oc.docker_container_name(board, task)
    inner_prompt = oc.translate_host_paths(prompt, mount_src)
    # An unblock resumes the prior in-container session (ticket #13); a fresh
    # dispatch mints one. The two flags are mutually exclusive.
    session_flag = (["--resume", session_id] if resuming
                    else ["--session-id", session_id])
    if oc.CHAT_ENABLED:
        inner = ["claude", "-p", "--input-format", "stream-json",
                 "--output-format", "stream-json", "--verbose", *session_flag]
    else:
        inner = ["claude", "-p", inner_prompt, *session_flag,
                 "--output-format", "stream-json", "--verbose"]
    if model:
        inner += ["--model", model]
    if allowed:
        inner += ["--allowedTools", ",".join(allowed)]
    passthrough = _resolve_passthrough_env(board_meta, log_f)
    cmd = oc.docker_run_argv(oc.docker_image_tag(board), container_name, mount_src,
                             env_file, inner, passthrough_env=passthrough,
                             interactive=oc.CHAT_ENABLED)
    return cmd, container_name, inner_prompt
```

(c) In `spawn_agent` (as written in Task 4), replace the docker call site and the `chat` gate:

```python
    container_name = None
    stdin_prompt = prompt  # what the first stream-json user message will carry
    if oc.use_docker(board_meta):
        cmd, container_name = _docker_dispatch(
            kanban_dir, board, task, board_meta, prompt, session_id, model,
            allowed, log_f, resuming=resuming)
```

becomes

```python
    container_name = None
    stdin_prompt = prompt  # what the first stream-json user message will carry
    if oc.use_docker(board_meta):
        # Docker mode: the first stdin message must carry the host-path-
        # TRANSLATED prompt (the agent lives on the /workspace mount).
        cmd, container_name, stdin_prompt = _docker_dispatch(
            kanban_dir, board, task, board_meta, prompt, session_id, model,
            allowed, log_f, resuming=resuming)
```

and

```python
    # Chat wiring is host-only for now; the Docker task extends it (the inner
    # container claude does not read streaming input yet).
    chat = oc.CHAT_ENABLED and container_name is None
```

becomes

```python
    # Agent chat applies to both dispatch modes: host subprocess and docker
    # (`docker run -i` keeps the pipe attached through the container).
    chat = oc.CHAT_ENABLED
```

- [ ] **Step 4: Update the 2 remaining docker spawn tests**

In `tests/test_docker_workspace.py`, chat mode now applies to docker spawns: fake procs need stdin and the pump seam must be stubbed; the prompt moved from `inner[2]` to stdin.

Replace `test_spawn_agent_docker_runs_in_container` (~line 157) with:

```python
def test_spawn_agent_docker_runs_in_container(kanban, monkeypatch):
    _docker_board(kanban, env={"FOO": "bar"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    builds = []

    class FakeBuild:
        returncode = 0

    monkeypatch.setattr(orch.subprocess, "run",
                        lambda argv, **k: builds.append(argv) or FakeBuild())

    captured = {}

    class FakeProc:
        pid = 7777

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cmd"] = cmd
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)

    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    marker = orch.spawn_agent(kanban, "demo", task,
                              {"name": "g", "systemPrompt": "p"}, "m")

    cmd = captured["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    # The image was built first.
    assert any(a[:2] == ["docker", "build"] for a in builds)
    # Workspace root is mounted at /workspace and an env-file is supplied.
    assert any(str(a).endswith(":/workspace") for a in cmd)
    assert "--env-file" in cmd
    # The container name is recorded on the marker AND used in the run command.
    cname = oc.docker_container_name("demo", task)
    assert marker["containerName"] == cname
    assert cname in cmd
    # The inner claude invocation runs after the image tag; in chat mode the
    # prompt travels via stdin (host paths translated onto the /workspace
    # mount) rather than argv.
    tag = oc.docker_image_tag("demo")
    inner = cmd[cmd.index(tag) + 1:]
    assert inner[0] == "claude" and inner[1] == "-p"
    text = json.loads(captured["proc"].stdin.getvalue().decode("utf-8")
                      .splitlines()[0])["message"]["content"][0]["text"]
    assert "/workspace" in text
    assert kanban not in text
```

Replace `test_spawn_agent_docker_forwards_api_key` (~line 208) with:

```python
def test_spawn_agent_docker_forwards_api_key(kanban, monkeypatch):
    _docker_board(kanban)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))
    captured = {}

    class FakeProc:
        pid = 7778
        stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")
    cmd = captured["cmd"]
    # The host's credential is forwarded by name (value inherited), never echoed.
    assert "ANTHROPIC_API_KEY" in cmd
    assert "sk-test" not in cmd
```

Also ensure `import io` is present in this file's imports (added in Task 4 Step 6 if not already).

- [ ] **Step 5: Run the docker suite**

Run: `python -m pytest tests/test_docker_workspace.py -q`
Expected: PASS.

- [ ] **Step 6: Full-suite regression run (A2 milestone gate)**

Run: `python -m pytest tests -q`
Expected: PASS — everything green. If any unrelated test regressed, fix it before committing (do not skip).

- [ ] **Step 7: Commit**

```bash
git add orchestrator_core.py orchestrator.py tests/test_docker_workspace.py
git commit -m "chat: docker -i streaming-input dispatch, translated prompt via stdin"
```

---

## Manual verification (NOT automated — costs tokens)

One live smoke path is deliberately deferred to a human/manual step, per the spec's Testing section:

1. Start the server and orchestrator (`python .kanban/kanban_server.py`, `python .kanban/orchestrator.py`) with `CHAT_ENABLED = True` (the default).
2. Create a trivial ticket on a non-docker board (e.g. "reply to my chat message, then finish") and let the orchestrator dispatch it.
3. Confirm the run log at `_orchestrator/runs/<id>-<ts>.log` starts filling (the CLI accepted the stdin-delivered prompt via `--input-format stream-json`).
4. While the ticket is `in_progress`, send a message:
   `curl -X POST http://127.0.0.1:8745/api/orchestrator/chat/<board>/<id> -H "Content-Type: application/json" -d "{\"message\": \"please also say hello\", \"writer\": \"ryan\"}"` → expect `{"ok": true}`.
5. Within a few seconds the run log should show a new user turn containing `[Message from ryan via Discord]` and a subsequent assistant turn responding to it.
6. Confirm the run then completes normally (pump closes stdin after the final result; the ticket is reaped to `completed`, the inbox file under `_orchestrator/chat/` is gone).
7. Optional docker check: repeat on a `useDocker: true` board with its per-board Dockerfile; confirm `docker run` includes `-i` (visible via the Performance tab or `docker inspect`) and the same round-trip works.
8. Escape-hatch check: flip `CHAT_ENABLED = False` in `orchestrator_core.py`, restart, dispatch — verify the argv-prompt legacy form (no stdin pipe) and that the chat POST returns `409 {"error": "chat disabled"}`. Flip it back to `True`.

## Ticket boundaries

The tasks split into TWO ticket-sized milestones:

- **Milestone A1 — Tasks 1–2** (mergeable alone, all tests green): pure chat helpers + `CHAT_ENABLED`/`CHAT_DIR` constants in `orchestrator_core.py`, plus the `POST /api/orchestrator/chat/<board>/<id>` server endpoint. After A1 the API accepts and stores chat messages; nothing consumes them yet (a running agent simply never reads its inbox — harmless, and the endpoint's 409s gate the common misuse). Full suite green at the Task 2 commit.
- **Milestone A2 — Tasks 3–5** (depends on A1): pump machinery + reap cleanup, streaming-input host dispatch, docker `-i` dispatch. This is the behavior change: dispatch switches to stdin-delivered prompts and run completion becomes pump-driven stdin close. Full suite green at the Task 5 commit; the manual smoke test above applies after A2.
