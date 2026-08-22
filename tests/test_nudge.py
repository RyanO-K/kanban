"""Tests for POST /api/orchestrator/nudge — immediate tick trigger."""
import json
import os
import threading
import http.client
import re

import pytest

import kanban_server as ks


@pytest.fixture
def server(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    import orchestrator_core as oc
    monkeypatch.setattr(oc, "KANBAN_DIR", kanban, raising=False)
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = conn.getresponse()
    data = r.read().decode("utf-8")
    conn.close()
    try:
        return r.status, (json.loads(data) if data else None)
    except json.JSONDecodeError:
        return r.status, data


def test_nudge_returns_ok(server, monkeypatch):
    """POST /api/orchestrator/nudge returns 200 {"ok": True, "queued": True}."""
    ticks = []

    def fake_tick():
        ticks.append(1)

    monkeypatch.setattr(ks, "orch_nudge_tick", fake_tick, raising=False)
    status, body = _req(server, "POST", "/api/orchestrator/nudge")
    assert status == 200
    assert body.get("ok") is True


def test_nudge_triggers_tick(server, kanban, monkeypatch):
    """POST /api/orchestrator/nudge actually runs a tick (promotes todo→ready)."""
    import orchestrator_core as oc

    # Ticket 1 is already in 'todo'; no deps, so a tick should promote it to 'ready'.
    p = os.path.join(kanban, "boards", "demo", "1.json")
    with open(p, "r", encoding="utf-8") as f:
        t = json.load(f)
    assert t["status"] == "todo"

    # Monkeypatch spawn so we don't launch real processes, but let the real tick run.
    import orchestrator as orch
    monkeypatch.setattr(orch, "spawn_agent", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("spawn should not be called in this test")))

    # A triage that dispatches nothing (no profiles, so backfill is empty too).
    monkeypatch.setattr(ks, "_nudge_opus_triage",
                        lambda *a, **kw: {"dispatch": []}, raising=False)
    # Stub initial triage so no Sonnet subprocess is spawned during promotion.
    monkeypatch.setattr(ks, "_nudge_initial_triage",
                        lambda *a, **kw: {}, raising=False)

    status, body = _req(server, "POST", "/api/orchestrator/nudge")
    assert status == 200

    # The tick runs in a background thread; give it a moment to complete.
    import time as _time
    _time.sleep(0.5)

    with open(p, "r", encoding="utf-8") as f:
        updated = json.load(f)
    assert updated["status"] == "ready", (
        "nudge tick should promote todo→ready for a dependency-free ticket"
    )


def test_nudge_405_on_get(server):
    """GET /api/orchestrator/nudge must return 404 (not 200) — it's POST-only."""
    status, _ = _req(server, "GET", "/api/orchestrator/nudge")
    assert status == 404


def test_nudge_returns_queued_true_when_no_thread(server, monkeypatch):
    """nudge always returns queued:True (tick runs in a background thread)."""
    import threading as _threading

    threads_started = []
    real_thread = _threading.Thread

    class TrackingThread(real_thread):
        def start(self):
            threads_started.append(self)
            super().start()

    monkeypatch.setattr(ks.threading, "Thread", TrackingThread)
    status, body = _req(server, "POST", "/api/orchestrator/nudge")
    assert status == 200
    assert body.get("queued") is True
    # At least one thread was started for the tick.
    assert len(threads_started) >= 1


def test_nudge_button_on_boards_page():
    """The nudge button should appear in the topbar on the boards page."""
    html_path = ks.HTML_PATH
    js_path = ks.JS_PATH
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    with open(js_path, "r", encoding="utf-8") as f:
        js = f.read()

    # Button element should exist in the HTML with id="nudgeBoardBtn"
    assert re.search(r'id="nudgeBoardBtn"', html), (
        "nudgeBoardBtn not found in HTML — expected on topbar"
    )
    # Should NOT exist in the orchestrator tab (removed from there).
    # The old nudgeBtn should be gone completely.
    assert "nudgeBtn" not in html, (
        "old nudgeBtn reference should be removed from HTML"
    )
    # Event handler lives in the external JS file.
    assert re.search(r'\$\("nudgeBoardBtn"\)\.addEventListener', js), (
        "nudgeBoardBtn event handler not found in kanban.js"
    )


def test_nudge_button_calls_api():
    """The nudge button handler should POST to /api/orchestrator/nudge."""
    with open(ks.JS_PATH, "r", encoding="utf-8") as f:
        js = f.read()

    # Extract the nudgeBoardBtn click handler to verify it calls the right endpoint.
    handler_match = re.search(
        r'\$\("nudgeBoardBtn"\)\.addEventListener\("click",async\s*\(\)=>\{([^}]+)\}\)',
        js,
        re.DOTALL
    )
    assert handler_match, "Could not find nudgeBoardBtn event handler in kanban.js"
    handler_code = handler_match.group(1)
    assert "/api/orchestrator/nudge" in handler_code, (
        "Handler should POST to /api/orchestrator/nudge"
    )
    assert "method:" in handler_code and "POST" in handler_code, (
        "Handler should use POST method"
    )


# --- Multiple nudge / serialization tests ------------------------------------

def _reset_nudge_state(monkeypatch):
    """Reset module-level nudge state between tests."""
    monkeypatch.setattr(ks, "_nudge_running", False, raising=False)
    monkeypatch.setattr(ks, "_nudge_queued", False, raising=False)


def test_nudge_second_call_while_running_queues_one(monkeypatch):
    """While a nudge tick is running, a second POST queues exactly one follow-up."""
    import time as _time

    tick_started = threading.Event()
    tick_unblock = threading.Event()
    tick_calls = []

    def slow_tick():
        tick_calls.append("start")
        tick_started.set()
        tick_unblock.wait(timeout=5)
        tick_calls.append("end")

    _reset_nudge_state(monkeypatch)
    monkeypatch.setattr(ks, "orch_nudge_tick", slow_tick, raising=False)

    # First nudge: starts the slow tick.
    ks.orch_nudge()
    tick_started.wait(timeout=2)
    assert ks._nudge_running is True

    # Second nudge while running: should queue.
    result, _ = ks.orch_nudge()
    assert result.get("ok") is True
    assert ks._nudge_queued is True

    # Third nudge while running: queue is already full, still just one queued.
    ks.orch_nudge()
    assert ks._nudge_queued is True

    # Unblock the first tick and wait for it to drain.
    tick_unblock.set()
    _time.sleep(0.3)

    # After the first tick ends, the queued nudge should have fired and cleared.
    _time.sleep(0.3)
    assert tick_calls.count("start") == 2, (
        f"Expected exactly 2 tick runs (one immediate + one queued), got {tick_calls}"
    )
    assert ks._nudge_queued is False
    assert ks._nudge_running is False


def test_nudge_no_parallel_ticks(monkeypatch):
    """Two parallel nudge calls must not run two ticks simultaneously."""
    import time as _time

    concurrent_peak = [0]
    running_now = [0]
    tick_lock = threading.Lock()

    def counting_tick():
        with tick_lock:
            running_now[0] += 1
            concurrent_peak[0] = max(concurrent_peak[0], running_now[0])
        _time.sleep(0.05)
        with tick_lock:
            running_now[0] -= 1

    _reset_nudge_state(monkeypatch)
    monkeypatch.setattr(ks, "orch_nudge_tick", counting_tick, raising=False)

    # Fire many nudges rapidly.
    for _ in range(5):
        ks.orch_nudge()
        _time.sleep(0.01)

    _time.sleep(0.5)
    assert concurrent_peak[0] <= 1, (
        f"Ticks ran in parallel (peak concurrency={concurrent_peak[0]})"
    )


def test_nudge_backlog_capped_at_one(monkeypatch):
    """Spamming nudge while running should result in at most 2 total tick runs."""
    import time as _time

    tick_started = threading.Event()
    tick_unblock = threading.Event()
    tick_count = [0]

    def slow_tick():
        tick_count[0] += 1
        if tick_count[0] == 1:
            tick_started.set()
            tick_unblock.wait(timeout=5)

    _reset_nudge_state(monkeypatch)
    monkeypatch.setattr(ks, "orch_nudge_tick", slow_tick, raising=False)

    ks.orch_nudge()
    tick_started.wait(timeout=2)

    # Spam 10 nudges while tick 1 is running.
    for _ in range(10):
        ks.orch_nudge()

    tick_unblock.set()
    _time.sleep(0.5)

    assert tick_count[0] <= 2, (
        f"Backlog should be capped at 1 (max 2 total ticks), got {tick_count[0]}"
    )


def test_nudge_completes_cleanly_when_no_backlog(monkeypatch):
    """After a tick with no queued follow-up, _nudge_running is False."""
    import time as _time

    done = threading.Event()

    def fast_tick():
        done.set()

    _reset_nudge_state(monkeypatch)
    monkeypatch.setattr(ks, "orch_nudge_tick", fast_tick, raising=False)

    ks.orch_nudge()
    done.wait(timeout=2)
    _time.sleep(0.1)

    assert ks._nudge_running is False
    assert ks._nudge_queued is False
