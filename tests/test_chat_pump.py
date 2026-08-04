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


def test_pump_keeps_pending_inbox_when_child_dies(tmp_path):
    # Bot-messaging follow-up: a queued message must SURVIVE the run's death so
    # the reap path can surface it into the ticket (comment + pendingChat)
    # instead of silently dropping it.
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    _append_inbox(str(inbox), "pending")   # queued, never delivered
    proc = _spawn_child(echo)
    proc.kill()
    proc.wait()
    th = _run_pump(proc, inbox, log)
    assert _wait(lambda: not th.is_alive()), "pump must exit when child died"
    assert os.path.exists(str(inbox)), \
        "an inbox with undelivered messages must survive child death"
    msgs = oc.chat_read_messages(str(inbox))
    assert [(m["message"], m["delivered"]) for m in msgs] == [("pending", False)]


def test_pump_deletes_drained_inbox_when_child_dies(tmp_path):
    # Everything delivered (offset sidecar covers the whole file): the pump's
    # child-death cleanup deletes inbox + sidecar as before.
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    proc = _spawn_child(echo)
    try:
        th = _run_pump(proc, inbox, log)
        _append_inbox(str(inbox), "seen by the agent")

        def _delivered():
            try:
                return len(echo.read_text(encoding="utf-8").splitlines()) >= 1
            except OSError:
                return False
        assert _wait(_delivered)
    finally:
        proc.kill()
        proc.wait()
    assert _wait(lambda: not th.is_alive())
    assert not os.path.exists(str(inbox)), \
        "a fully delivered inbox is deleted on child death"
    assert not os.path.exists(oc.chat_offset_path(str(inbox)))


def test_pump_persists_delivered_offset_sidecar(tmp_path):
    echo = tmp_path / "echo.jsonl"
    inbox = tmp_path / "chat" / "demo__1.jsonl"
    log = tmp_path / "run.log"
    log.write_text("", encoding="utf-8")
    proc = _spawn_child(echo)
    try:
        _run_pump(proc, inbox, log)
        _append_inbox(str(inbox), "first")

        def _offset_written():
            return oc.chat_read_offset(str(inbox)) > 0
        assert _wait(_offset_written), "pump must persist the delivered offset"
        assert oc.chat_read_offset(str(inbox)) == os.path.getsize(str(inbox))
        # A GET-style read now reports the message as delivered.
        msgs = oc.chat_read_messages(str(inbox))
        assert [(m["message"], m["delivered"]) for m in msgs] == [("first", True)]
    finally:
        proc.kill()
        proc.wait()


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
    # "boom" was never delivered (the write raised), so the inbox survives for
    # the reap path to surface — same rule as child death with pending messages.
    assert os.path.exists(str(inbox))
    msgs = oc.chat_read_messages(str(inbox))
    assert [(m["message"], m["delivered"]) for m in msgs] == [("boom", False)]


class _DeadThread:
    def is_alive(self):
        return False


def test_release_proc_drops_pump_and_deletes_drained_inbox(tmp_path):
    inbox = tmp_path / "demo__1.jsonl"
    inbox.write_text("x\n", encoding="utf-8")  # malformed-only == nothing pending

    pid = 987654
    orch._PUMPS[pid] = {"thread": _DeadThread(), "inbox": str(inbox)}
    orch._release_proc(pid)   # pid not in _PROCS: must still clean the pump
    assert pid not in orch._PUMPS
    assert not inbox.exists()


def test_release_proc_keeps_inbox_with_undelivered_messages(tmp_path):
    inbox = tmp_path / "demo__1.jsonl"
    _append_inbox(str(inbox), "queued for the next run")

    pid = 987655
    orch._PUMPS[pid] = {"thread": _DeadThread(), "inbox": str(inbox)}
    orch._release_proc(pid)
    assert pid not in orch._PUMPS
    assert inbox.exists(), \
        "release must not delete an inbox holding undelivered messages"
