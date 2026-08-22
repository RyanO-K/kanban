# Performance Tab Usability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Performance tab surfaces problems automatically (sustained CPU, spawn churn), shows the kanban server's own process, reads friendly-first, and gains safe kill/jump actions — while the sampler gets *cheaper* via an adaptive interval.

**Architecture:** All detection lives server-side in `PerfSampler` (perf_monitor.py), which already keeps per-session rolling history keyed by `(pid, create_time)`. The API ships ready-made `alerts` plus per-session `spawnRate`/`cpuAvg2m`/`flags`; kanban.js only renders. The sampler samples fast (3s) only while `/api/performance` is actually being served, idling at 30s otherwise.

**Tech Stack:** Python 3.13 stdlib + psutil (optional dep), pytest; vanilla JS/CSS UI (no framework, no build step).

**Spec:** `docs/superpowers/specs/2026-07-08-performance-tab-usability-design.md` (committed, approved).

## Global Constraints

- No new polling loops, no new subprocess spawns, no extra `cpu_percent()` calls beyond the existing one per session per cycle. (Each process spawn/file op pays a CrowdStrike kernel tax on this machine.)
- All API shape changes are **additive** — never rename or remove existing fields.
- Sampler thread must never die on an exception (wrap like existing `_loop`).
- No `Date.now()`-style time calls inside detector logic without an injectable clock — tests must not sleep.
- Repo: `C:\Users\AE04581\Documents\GitHub\.kanban`, branch `release`, commit directly (no feature branch — kanban-repo convention).
- Run tests with: `C:\Python313\python.exe -m pytest tests/ -q` from the `.kanban` directory.
- Alert thresholds (module constants in perf_monitor.py): `ALERT_CPU_AVG = 50.0` (% of one core, 2-min avg), `ALERT_SPAWN_RATE = 10` (new child PIDs/min), `ALERT_MIN_HISTORY_S = 60.0` (no alerts before a session has this much history).
- Intervals: fast `interval = 3.0` (existing), `idle_interval = 30.0`, `watch_window = 30.0` (seconds since last served snapshot that counts as "being watched").

**WARNING — dirty working tree:** `static/kanban.js` and `static/kanban.css` carry unrelated *staged* changes (VS Code file links in the ticket panel). **Before starting Task 5**, commit those staged changes as their own commit:
`git commit -m "ticket panel: link ticket files to VS Code"` (they are already staged; do NOT `git add` anything first). Perf-tab commits must not mix with them.

---

### Task 1: Adaptive sampling interval

**Files:**
- Modify: `app/perf_monitor.py` (PerfSampler `__init__`, `snapshot`, `_loop`)
- Test: `tests/test_perf_adaptive.py` (create)

**Interfaces:**
- Consumes: existing `PerfSampler` (interval, `_stop` event, `snapshot()` called once per `/api/performance` request by `kanban_server.perf_snapshot`).
- Produces: `PerfSampler(interval=3.0, idle_interval=30.0, watch_window=30.0, ...)`; method `_next_interval(now=None) -> float` (pure given `now`; used by `_loop` and tests); `snapshot(now=None)` records the serve time.

- [ ] **Step 1: Write the failing test**

Create `tests/test_perf_adaptive.py`:

```python
"""Adaptive sampler cadence: fast only while snapshots are being served."""
import perf_monitor as pm


def test_recent_snapshot_selects_fast_interval():
    s = pm.PerfSampler(interval=3.0, idle_interval=30.0, watch_window=30.0)
    s.snapshot(now=1000.0)                      # someone is watching
    assert s._next_interval(now=1010.0) == 3.0  # 10s later: still watched


def test_stale_snapshot_selects_idle_interval():
    s = pm.PerfSampler(interval=3.0, idle_interval=30.0, watch_window=30.0)
    s.snapshot(now=1000.0)
    assert s._next_interval(now=1031.0) == 30.0  # >30s since last serve


def test_never_served_starts_idle():
    s = pm.PerfSampler(interval=3.0, idle_interval=30.0, watch_window=30.0)
    assert s._next_interval(now=1000.0) == 30.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_adaptive.py -v`
Expected: FAIL — `TypeError: snapshot() got an unexpected keyword argument 'now'` (or missing `idle_interval`).

- [ ] **Step 3: Implement**

In `app/perf_monitor.py`, add `import time` to the imports if absent. Change `PerfSampler.__init__` signature and body:

```python
    def __init__(self, interval=3.0, cap=100, owned_pids_fn=None,
                 labels_fn=None, env_reader=None, summary_fn=None,
                 idle_interval=30.0, watch_window=30.0):
        self.interval = interval
        self.idle_interval = idle_interval
        self.watch_window = watch_window
        self._last_served = None   # monotonic time snapshot() last ran
```

(keep every existing line of the old body after these). Replace `snapshot` and `_loop`:

```python
    def snapshot(self, now=None):
        # Serving a snapshot = someone is watching: run at the fast cadence.
        self._last_served = time.monotonic() if now is None else now
        with self._lock:
            return self._cache

    def _next_interval(self, now=None):
        """Fast cadence while a snapshot was served in the last watch_window
        seconds; the cheap idle cadence otherwise."""
        now = time.monotonic() if now is None else now
        if self._last_served is not None and (now - self._last_served) < self.watch_window:
            return self.interval
        return self.idle_interval

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:
                pass  # never let the sampler thread die
            self._stop.wait(self._next_interval())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_adaptive.py tests/test_perf_monitor.py tests/test_perf_summaries.py -v`
Expected: all PASS (existing perf tests must stay green).

- [ ] **Step 5: Commit**

```bash
git add app/perf_monitor.py tests/test_perf_adaptive.py
git commit -m "perf tab: adaptive sampler cadence (3s watched, 30s idle)"
```

---

### Task 2: Server self pseudo-session

**Files:**
- Modify: `app/perf_monitor.py` (module-level helper + `PerfSampler.__init__`/`sample_once`)
- Modify: `app/kanban_server.py:1448` (`_PERF_SAMPLER` construction)
- Test: `tests/test_perf_self_session.py` (create)

**Interfaces:**
- Consumes: `PerfSampler.sample_once()` session-list assembly; `discover_sessions` is untouched.
- Produces: `perf_monitor.self_session() -> dict|None` returning a session entry with `kind: "server"`; `PerfSampler(..., self_fn=None)` — when `self_fn` is provided, `sample_once` prepends its entry. `kanban_server` passes `self_fn=perf_monitor.self_session`. Existing default-constructed samplers (tests) are unaffected.

- [ ] **Step 1: Write the failing test**

Create `tests/test_perf_self_session.py`:

```python
"""The kanban server appears in the perf snapshot as a pinned pseudo-session."""
import perf_monitor as pm


def fake_self_session():
    return {"pid": 7777, "kind": "server", "owned": True, "board": None,
            "ticket": None, "model": None, "cpuPercent": 12.5,
            "memoryMB": 80.0, "childCount": 0, "children": [],
            "_createTime": 5.0}


def test_self_session_prepended_when_configured():
    s = pm.PerfSampler(self_fn=fake_self_session)
    s._proc_iter = lambda: []          # no claude sessions at all
    snap = s.sample_once()
    assert snap["totals"]["sessionCount"] == 1
    entry = snap["sessions"][0]
    assert entry["kind"] == "server" and entry["pid"] == 7777
    assert entry["history"]           # gets history like any session


def test_no_self_session_by_default():
    s = pm.PerfSampler()
    s._proc_iter = lambda: []
    snap = s.sample_once()
    assert snap["totals"]["sessionCount"] == 0


def test_real_self_session_reports_this_process():
    import os
    entry = pm.self_session()
    if not pm.PSUTIL_AVAILABLE:
        assert entry is None
    else:
        assert entry["pid"] == os.getpid()
        assert entry["kind"] == "server"
        assert entry["children"] == []   # no rollup: claude children are their own sessions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_self_session.py -v`
Expected: FAIL — `AttributeError: module 'perf_monitor' has no attribute 'self_session'`.

- [ ] **Step 3: Implement**

In `app/perf_monitor.py`, module level (near `_find_proc`):

```python
# Persistent handle for the server's own process: cpu_percent() measures the
# delta since the previous call on the SAME Process object, so it must live
# across samples.
_SELF_PROC = None


def self_session():
    """A pinned pseudo-session for the kanban server's own process.

    CPU + RSS of this process only — NO children rollup: claude.exe processes
    it spawned already appear as their own sessions and would double-count.
    Returns None when psutil is unavailable.
    """
    global _SELF_PROC
    if not PSUTIL_AVAILABLE:
        return None
    try:
        if _SELF_PROC is None:
            _SELF_PROC = psutil.Process(os.getpid())
        return {
            "pid": _SELF_PROC.pid,
            "kind": "server",
            "owned": True,
            "board": None, "ticket": None, "model": None,
            "cpuPercent": round(float(_SELF_PROC.cpu_percent()), 1),
            "memoryMB": round(float(_SELF_PROC.memory_info().rss) / (1024 * 1024), 1),
            "childCount": 0,
            "children": [],
            "_createTime": float(_SELF_PROC.create_time()),
        }
    except Exception:
        return None
```

In `PerfSampler.__init__`, add the parameter `self_fn=None` (after `summary_fn`) and the line `self._self_fn = self_fn`.

In `sample_once`, right after the `sessions = discover_sessions(...)` call, insert:

```python
        if self._self_fn is not None:
            self_entry = self._self_fn()
            if self_entry:
                sessions.insert(0, self_entry)
```

In `app/kanban_server.py:1448` change the sampler construction to:

```python
_PERF_SAMPLER = perf_monitor.PerfSampler(owned_pids_fn=_owned_pids,
                                          labels_fn=_server_op_labels,
                                          self_fn=perf_monitor.self_session)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_self_session.py tests/test_perf_monitor.py tests/test_perf_summaries.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/perf_monitor.py app/kanban_server.py tests/test_perf_self_session.py
git commit -m "perf tab: pin the kanban server as a self pseudo-session"
```

---

### Task 3: Detectors — spawn churn, cpuAvg2m, flags, alerts

**Files:**
- Modify: `app/perf_monitor.py` (constants; `PerfSampler.__init__`/`sample_once`)
- Test: `tests/test_perf_detectors.py` (create)

**Interfaces:**
- Consumes: `sample_once`'s per-session loop (`key = (pid, create_time)`, history deque, `s["children"]` list of `{pid,...}`), Task 2's `self_fn` seam for injecting sessions.
- Produces: module constants `ALERT_CPU_AVG = 50.0`, `ALERT_SPAWN_RATE = 10`, `ALERT_MIN_HISTORY_S = 60.0`; `PerfSampler(..., clock=None)` (epoch-seconds callable, default `time.time`); per-session fields `spawnRate` (float/min), `cpuAvg2m` (float), `flags` (list of `"highCpu"`/`"spawnChurn"`); snapshot field `alerts` (list of `{"severity": "warn", "pid": int, "kind": str, "message": str}`); history points gain `"ts"` (epoch float).

- [ ] **Step 1: Write the failing test**

Create `tests/test_perf_detectors.py`:

```python
"""Server-side detectors: sustained CPU, spawn churn, alerts."""
import perf_monitor as pm


def make_sampler(clock):
    s = pm.PerfSampler(clock=clock)
    s._proc_iter = lambda: []
    return s


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def session(pid=1, cpu=5.0, children=None, kind="headless",
            board="demo", ticket="7"):
    return {"pid": pid, "kind": kind, "owned": True, "board": board,
            "ticket": ticket, "model": None, "cpuPercent": cpu,
            "memoryMB": 10.0, "childCount": len(children or []),
            "children": [{"pid": p, "name": "bash.exe", "cpuPercent": 0.0,
                          "memoryMB": 1.0} for p in (children or [])],
            "_createTime": 5.0}


def drive(sampler, clock, entries, step=3.0):
    """Feed one sample per entries item, advancing the clock each time."""
    snaps = []
    for e in entries:
        sampler._self_fn = lambda e=e: e
        snaps.append(sampler.sample_once())
        clock.t += step
    return snaps


def test_sustained_cpu_flags_after_min_history():
    clock = Clock()
    s = make_sampler(clock)
    # 90s of 80% CPU in 3s steps
    snaps = drive(s, clock, [session(cpu=80.0)] * 30)
    early, late = snaps[5]["sessions"][0], snaps[-1]["sessions"][0]
    assert "highCpu" not in early["flags"]      # <60s history: no alert
    assert "highCpu" in late["flags"]
    assert late["cpuAvg2m"] > 50.0
    assert any(a["pid"] == 1 and "demo #7" in a["message"]
               for a in snaps[-1]["alerts"])


def test_low_cpu_never_flags():
    clock = Clock()
    s = make_sampler(clock)
    snaps = drive(s, clock, [session(cpu=5.0)] * 30)
    assert snaps[-1]["sessions"][0]["flags"] == []
    assert snaps[-1]["alerts"] == []


def test_spawn_churn_from_new_child_pids():
    clock = Clock()
    s = make_sampler(clock)
    # 24 samples (~72s), a fresh child PID set every sample: ~20 new/min
    entries = [session(children=[100 + i]) for i in range(24)]
    snaps = drive(s, clock, entries)
    last = snaps[-1]["sessions"][0]
    assert last["spawnRate"] > pm.ALERT_SPAWN_RATE
    assert "spawnChurn" in last["flags"]


def test_stable_children_no_churn():
    clock = Clock()
    s = make_sampler(clock)
    snaps = drive(s, clock, [session(children=[100, 101])] * 24)
    last = snaps[-1]["sessions"][0]
    assert last["spawnRate"] == 0.0
    assert "spawnChurn" not in last["flags"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_detectors.py -v`
Expected: FAIL — `TypeError: PerfSampler() got an unexpected keyword argument 'clock'`.

- [ ] **Step 3: Implement**

In `app/perf_monitor.py`, module level (near the top, after `_CLAUDE_PROJECTS_DIR`):

```python
# Detector thresholds (spec 2026-07-08). A session needs ALERT_MIN_HISTORY_S
# of history before it may alert (no cold-start false positives).
ALERT_CPU_AVG = 50.0        # % of one core, averaged over the last 2 minutes
ALERT_SPAWN_RATE = 10       # new child PIDs per minute
ALERT_MIN_HISTORY_S = 60.0
```

In `PerfSampler.__init__`, add parameter `clock=None` (last) and:

```python
        self._clock = clock or time.time
        self._churn = {}   # key -> {"prev": set|None, "events": deque[(ts, n)]}
```

Replace the body of `sample_once` (keep the `discover_sessions` call and Task 2's self-session insert; the per-session loop becomes):

```python
        ts = _now_iso()
        now = self._clock()
        live_keys = set()
        tot_cpu = tot_mem = 0.0
        alerts = []
        for s in sessions:
            key = (s["pid"], s.pop("_createTime"))
            live_keys.add(key)
            buf = self._history.setdefault(key, deque(maxlen=self.cap))
            buf.append({"t": ts, "ts": now, "cpu": s["cpuPercent"],
                        "mem": s["memoryMB"]})
            s["history"] = list(buf)
            tot_cpu += s["cpuPercent"]
            tot_mem += s["memoryMB"]

            # ── detectors (spec 2026-07-08) ───────────────────────────
            churn = self._churn.setdefault(key, {"prev": None,
                                                 "events": deque()})
            cur_pids = {c["pid"] for c in (s.get("children") or [])}
            if churn["prev"] is not None:
                new = cur_pids - churn["prev"]
                if new:
                    churn["events"].append((now, len(new)))
            churn["prev"] = cur_pids
            while churn["events"] and churn["events"][0][0] < now - 60.0:
                churn["events"].popleft()
            s["spawnRate"] = round(float(sum(n for _, n in churn["events"])), 1)

            recent = [p["cpu"] for p in buf if p.get("ts", 0) >= now - 120.0]
            s["cpuAvg2m"] = round(sum(recent) / len(recent), 1) if recent else 0.0

            s["flags"] = []
            aged = buf[0].get("ts", now) <= now - ALERT_MIN_HISTORY_S
            if aged and s["cpuAvg2m"] > ALERT_CPU_AVG:
                s["flags"].append("highCpu")
            if aged and s["spawnRate"] > ALERT_SPAWN_RATE:
                s["flags"].append("spawnChurn")
            if s["flags"]:
                name = ("kanban server" if s["kind"] == "server"
                        else s.get("label")
                        or (f"{s['board']} #{s['ticket']}" if s.get("ticket")
                            else f"PID {s['pid']}"))
                if "highCpu" in s["flags"]:
                    alerts.append({"severity": "warn", "pid": s["pid"],
                                   "kind": "highCpu",
                                   "message": f"{name} has averaged "
                                              f"{s['cpuAvg2m']}% CPU over the "
                                              f"last 2 minutes"})
                if "spawnChurn" in s["flags"]:
                    alerts.append({"severity": "warn", "pid": s["pid"],
                                   "kind": "spawnChurn",
                                   "message": f"{name} spawned "
                                              f"{int(s['spawnRate'])} processes "
                                              f"in the last minute"})
        # prune history and churn state for sessions no longer present
        for dead in [k for k in self._history if k not in live_keys]:
            del self._history[dead]
        for dead in [k for k in self._churn if k not in live_keys]:
            del self._churn[dead]
        snap = {
            "available": PSUTIL_AVAILABLE,
            "sampledAt": ts,
            "totals": {"cpuPercent": round(tot_cpu, 1),
                       "memoryMB": round(tot_mem, 1),
                       "sessionCount": len(sessions)},
            "alerts": alerts,
            "sessions": sessions,
        }
        with self._lock:
            self._cache = snap
        return snap
```

- [ ] **Step 4: Run the full perf tests**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_detectors.py tests/test_perf_adaptive.py tests/test_perf_self_session.py tests/test_perf_monitor.py tests/test_perf_summaries.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the whole suite (sampler is widely consumed)**

Run: `C:\Python313\python.exe -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/perf_monitor.py tests/test_perf_detectors.py
git commit -m "perf tab: sustained-CPU and spawn-churn detectors with alerts"
```

---

### Task 4: Kill guard — never kill the server via the claude-kill path

**Files:**
- Modify: `app/kanban_server.py:1463` (`perf_kill`)
- Test: `tests/test_perf_kill_guard.py` (create)

**Interfaces:**
- Consumes: `perf_kill(pid_str)` returning `(payload, status)`.
- Produces: same signature; `pid == os.getpid()` returns
  `({"error": "refusing to kill the kanban server; use POST /api/server/restart"}, 400)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_perf_kill_guard.py`:

```python
"""The perf kill endpoint must refuse the server's own PID."""
import os

import kanban_server as ks


def test_perf_kill_refuses_own_pid():
    payload, status = ks.perf_kill(str(os.getpid()))
    assert status == 400
    assert "restart" in payload["error"]


def test_perf_kill_still_rejects_garbage():
    payload, status = ks.perf_kill("not-a-pid")
    assert status == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_kill_guard.py -v`
Expected: `test_perf_kill_refuses_own_pid` FAILS (kill_session on our own test runner would be attempted — the test asserts 400 but gets 200. Note: it will NOT actually kill the pytest process on Windows because taskkill is only reached for real; to be safe the guard must be implemented before running with `-v` on the real pid — this is exactly why the test exists. If nervous, implement Step 3 first and accept a test-first violation note in the commit.)

- [ ] **Step 3: Implement**

In `app/kanban_server.py`, replace `perf_kill`:

```python
def perf_kill(pid_str):
    try:
        pid = int(pid_str)
    except (TypeError, ValueError):
        return {"error": "bad pid"}, 400
    if pid == os.getpid():
        return {"error": "refusing to kill the kanban server; "
                         "use POST /api/server/restart"}, 400
    return perf_monitor.kill_session(pid), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\Python313\python.exe -m pytest tests/test_perf_kill_guard.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/kanban_server.py tests/test_perf_kill_guard.py
git commit -m "perf tab: refuse killing the server's own pid via the kill endpoint"
```

---

### Task 5: UI — alert banner, friendly rows, flags, server row

> **FIRST:** commit the pre-existing staged `static/kanban.js`/`static/kanban.css` changes (see Global Constraints warning) with
> `git commit -m "ticket panel: link ticket files to VS Code"`.

**Files:**
- Modify: `static/kanban.js:1629-1704` (`renderPerformance`)
- Modify: `static/kanban.css` (append perf styles)

**Interfaces:**
- Consumes: snapshot fields from Tasks 2-3 (`alerts`, per-session `kind:"server"`, `flags`, `spawnRate`, `cpuAvg2m`, plus existing `label`, `conversationSummary`, `board`, `ticket`); UI globals `currentFile`, `currentTasks`, `esc()`, `getModelName()`, `showToast()`.
- Produces: helper `perfSessionName(s) -> string` and row ids `perf-row-<pid>` consumed by Task 6's modal/jump wiring.

- [ ] **Step 1: Append styles to `static/kanban.css`**

```css
/* ── Performance tab (spec 2026-07-08) ─────────────────────────── */
.perf-alert { display:flex;align-items:center;gap:8px;background:#7c2d1220;border:1px solid #ea580c;border-radius:6px;padding:6px 10px;margin-bottom:8px;font-size:13px;cursor:pointer; }
.perf-alert:hover { background:#7c2d1240; }
.perf-alert .sev { color:#fb923c;font-weight:600; }
.perf-row.flagged td { background:#7c2d1218; }
.perf-chip { background:#ea580c;color:#fff;border-radius:3px;padding:1px 6px;margin-left:6px;font-size:11px; }
.perf-name { font-weight:500; }
.perf-pid { color:var(--text-muted);font-size:11px;margin-left:6px; }
```

- [ ] **Step 2: Add the name helper and rewrite the render loop in `static/kanban.js`**

Insert before `renderPerformance` (around line 1629):

```javascript
function perfSessionName(s){
  if(s.kind==="server") return "kanban server";
  if(s.label) return s.label;                          // triage / summarize ops
  if(s.ticket){
    // Title lookup only when that board's tasks are already loaded in the UI.
    let title="";
    if(currentFile===s.board||currentFile==="__all__"){
      const t=(currentTasks||[]).find(x=>String(x.id)===String(s.ticket)&&(!x._board||x._board===s.board));
      if(t) title=" — "+t.title;
    }
    return s.board+" #"+s.ticket+title;
  }
  if(s.conversationSummary) return s.conversationSummary.slice(0,80);
  return "Interactive session";
}
```

Replace the totals header + table-row construction inside `renderPerformance` (keep the fetch, `available` check, canvas drawing, and handler wiring) with:

```javascript
  const t = snap.totals || {};
  let html = "<div style='margin-bottom:8px;font-size:13px;color:var(--text-muted)'>"
    + "Sessions: <b>"+(t.sessionCount||0)+"</b> &nbsp; CPU: <b>"+(t.cpuPercent||0)
    + "%</b> &nbsp; Mem: <b>"+(t.memoryMB||0)+" MB</b> &nbsp; @ "+(snap.sampledAt||"")
    + "</div>";
  for(const a of (snap.alerts||[])){
    html += "<div class='perf-alert' data-pid='"+a.pid+"'><span class='sev'>⚠ "
      + (a.kind==="spawnChurn"?"Spawn churn":"High CPU")+"</span> "
      + esc(a.message)+"</div>";
  }
  html += "<table style='width:100%;border-collapse:collapse;font-size:13px'>";
  for(const s of (snap.sessions||[])){
    const open = perfExpanded.has(s.pid);
    const badge = (txt,bg)=>"<span style='background:"+bg+";color:#fff;border-radius:3px;"
      +"padding:1px 6px;margin-left:6px;font-size:11px'>"+txt+"</span>";
    const flagged = (s.flags||[]).length ? " flagged" : "";
    const chips = (s.flags||[]).map(f=>"<span class='perf-chip'>"
      +(f==="spawnChurn"?"spawn churn":"high CPU")+"</span>").join("");
    const kindBadge = s.kind==="server" ? badge("server","#16a34a")
      : badge(s.kind, s.kind==="headless"?"#a855f7":"#0ea5e9");
    const action = s.kind==="server"
      ? "<button class='perf-restart'>Restart</button>"
      : "<button class='perf-kill' data-pid='"+s.pid+"'>Kill</button>";
    html += "<tr id='perf-row-"+s.pid+"' data-pid='"+s.pid+"' class='perf-row"+flagged
      + "' style='border-top:1px solid var(--surface-alt);cursor:pointer'>"
      + "<td style='padding:6px 4px'>"+(open?"▾":"▸")+" <span class='perf-name'>"
      +   esc(perfSessionName(s))+"</span><span class='perf-pid'>PID "+s.pid+"</span>"
      +   kindBadge
      +   badge(s.owned?"owned":"external", s.owned?"#64748b":"#ef4444")
      +   (s.model?badge(getModelName(s.model),"#1f2937"):"")
      +   chips
      + "</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.cpuPercent+"%</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.memoryMB+" MB</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.childCount+" sub</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+action+"</td>"
      + "</tr>";
    if(open){
      let kids = (s.children||[]).map(c=>"PID "+c.pid+" "+c.name+" ("+c.cpuPercent
        +"%, "+c.memoryMB+"MB)").join("<br>") || "<i>no subprocesses</i>";
      let modelInfo = s.model?("<div style='font-size:12px;color:var(--text-muted);margin-bottom:6px'>Model: <b>"+esc(s.model)+"</b></div>"):"";
      html += "<tr><td colspan='5' style='padding:8px 16px;background:var(--bg)'>"
        + modelInfo
        + "<div style='font-size:12px;color:var(--text-muted);margin-bottom:6px'>2-min avg CPU: <b>"
        +   (s.cpuAvg2m!=null?s.cpuAvg2m:"–")+"%</b> &nbsp; spawn rate: <b>"
        +   (s.spawnRate!=null?s.spawnRate:"–")+"/min</b></div>"
        + "<canvas width='520' height='90' data-pid='"+s.pid
        +   "' style='display:block;margin-bottom:8px;border:1px solid var(--surface-alt)'></canvas>"
        + "<div style='font-size:11px;color:#3b82f6'>■ CPU%</div>"
        + "<div style='font-size:11px;color:#10b981'>■ Memory</div>"
        + "<div style='margin-top:6px;font-size:12px;color:var(--text-muted)'>"+kids+"</div>"
        + "</td></tr>";
    }
  }
  html += "</table>";
  wrap.innerHTML = html;
```

After the existing kill-button wiring, add banner + restart wiring:

```javascript
  // alert banner → scroll to the offending row
  wrap.querySelectorAll(".perf-alert").forEach(a=>{
    a.addEventListener("click",()=>{
      const row=$("perf-row-"+a.dataset.pid);
      if(row){row.scrollIntoView({behavior:"smooth",block:"center"});}
    });
  });
  // server row restart
  wrap.querySelectorAll(".perf-restart").forEach(b=>{
    b.addEventListener("click", async e=>{
      e.stopPropagation();
      if(!confirm("Restart the kanban server? In-flight agents keep running and are re-adopted."))return;
      try{ await apiFetch("/api/server/restart",{method:"POST"}); showToast("Server restarting…"); }
      catch(err){ showToast("Restart failed",true); }
    });
  });
```

- [ ] **Step 3: Verify manually**

Start the server (`C:\Python313\python.exe app\kanban_server.py` from the `.kanban` dir), open `http://127.0.0.1:8745`, Performance tab. Check: server row pinned with green `server` badge and Restart button; sessions named friendly-first with muted PID; expanding a row shows "2-min avg CPU / spawn rate" line; no console errors. (Alerts need a genuinely hot session — skip unless one exists.)

- [ ] **Step 4: Run the suite (server module changed indirectly — sanity)**

Run: `C:\Python313\python.exe -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add static/kanban.js static/kanban.css
git commit -m "perf tab UI: alert banner, friendly session names, server row"
```

---

### Task 6: Kill confirmation modal + jump to ticket/log

**Files:**
- Modify: `static/kanban.js` (kill wiring inside `renderPerformance`; new helpers after `perfSessionName`)
- Modify: `static/kanban.css` (modal styles)

**Interfaces:**
- Consumes: Task 5's `perfSessionName(s)`; existing `openPanel(task)` (kanban.js:375), `switchView(view)` (kanban.js:1469), `poll()` (async, kanban.js:231), globals `currentFile`, `currentTasks`, `$("boardSelect")`.
- Produces: `confirmKill(session)` modal; `jumpToTicket(board, ticketId)`.

- [ ] **Step 1: Append modal styles to `static/kanban.css`**

```css
.perf-modal-overlay { position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center;z-index:60; }
.perf-modal { background:var(--surface);border:1px solid var(--surface-alt);border-radius:8px;padding:16px 20px;max-width:460px;width:90%;font-size:13px; }
.perf-modal h3 { margin:0 0 8px;font-size:15px; }
.perf-modal ul { margin:8px 0;padding-left:18px;color:var(--text-muted);max-height:180px;overflow-y:auto; }
.perf-modal .row { display:flex;gap:8px;justify-content:flex-end;margin-top:12px; }
.perf-modal button { padding:6px 14px;border-radius:5px;border:1px solid var(--surface-alt);background:var(--bg);color:var(--text);cursor:pointer; }
.perf-modal button.danger { background:#dc2626;border-color:#dc2626;color:#fff; }
```

- [ ] **Step 2: Add helpers to `static/kanban.js`** (after `perfSessionName`)

```javascript
function confirmKill(s){
  const overlay=document.createElement("div");
  overlay.className="perf-modal-overlay";
  const kids=(s.children||[]).map(c=>"<li>PID "+c.pid+" "+esc(c.name)+"</li>").join("")
    || "<li><i>no subprocesses</i></li>";
  const ticketNote=s.ticket
    ? "<p>Ticket <b>"+esc(s.board)+" #"+esc(s.ticket)+"</b> goes back to the board for re-triage.</p>" : "";
  overlay.innerHTML="<div class='perf-modal'>"
    + "<h3>Kill "+esc(perfSessionName(s))+"?</h3>"
    + "<p>This terminates the whole subprocess tree:</p><ul>"+kids+"</ul>"
    + ticketNote
    + "<div class='row'><button class='cancel'>Cancel</button>"
    + "<button class='danger'>Kill session</button></div></div>";
  document.body.appendChild(overlay);
  overlay.addEventListener("click",e=>{ if(e.target===overlay) overlay.remove(); });
  overlay.querySelector(".cancel").addEventListener("click",()=>overlay.remove());
  overlay.querySelector(".danger").addEventListener("click", async ()=>{
    overlay.remove();
    try{
      await apiFetch("/api/performance/kill/"+encodeURIComponent(s.pid),{method:"POST"});
      showToast("Kill sent to PID "+s.pid);
      renderPerformance();
    }catch(err){ showToast("Failed to kill PID "+s.pid,true); }
  });
}

async function jumpToTicket(board,ticketId){
  const sel=$("boardSelect");
  if([...sel.options].some(o=>o.value===board)){ sel.value=board; currentFile=board; }
  switchView("boards");
  await poll();
  const t=(currentTasks||[]).find(x=>String(x.id)===String(ticketId));
  if(t) openPanel(t); else showToast("Ticket #"+ticketId+" not found on "+board,true);
}
```

- [ ] **Step 3: Rewire the kill button and add the jump link**

In `renderPerformance`, replace the `.perf-kill` wiring block with:

```javascript
  wrap.querySelectorAll(".perf-kill").forEach(b=>{
    b.addEventListener("click", e=>{
      e.stopPropagation();
      const s=(snap.sessions||[]).find(x=>String(x.pid)===b.dataset.pid);
      if(s) confirmKill(s);
    });
  });
  wrap.querySelectorAll(".perf-jump").forEach(a=>{
    a.addEventListener("click", e=>{
      e.stopPropagation();
      jumpToTicket(a.dataset.board, a.dataset.ticket);
    });
  });
```

And in the row construction (Task 5's template), after the `chips` variable add a jump link for ticket rows — change the first `<td>` to include, right after `chips`:

```javascript
      + (s.ticket?(" <a class='perf-jump' data-board='"+esc(s.board)+"' data-ticket='"
        +esc(s.ticket)+"' style='font-size:11px;color:#38bdf8;cursor:pointer'>open ticket ›</a>"):"")
```

- [ ] **Step 4: Verify manually**

With the server running and (if possible) one dispatched agent: Kill now opens the modal listing children and the ticket note — Cancel leaves it alive, Kill terminates the tree; "open ticket ›" switches to the board view with the ticket panel open (its log section live); the server row offers Restart, never Kill.

- [ ] **Step 5: Commit**

```bash
git add static/kanban.js static/kanban.css
git commit -m "perf tab UI: kill confirmation modal and jump-to-ticket link"
```

---

## Self-review notes

- Spec coverage: adaptive sampling → Task 1; self session → Task 2; detectors/alerts/API fields → Task 3; kill guard → Task 4; banner/readability/server row → Task 5; kill modal + jump → Task 6. Out-of-scope items from the spec have no tasks (correct).
- Task 4 Step 2's caveat is deliberate: running the failing test executes the unguarded path with the test-runner's own PID as input; `perf_kill` would call `kill_session(os.getpid())` for real. Implementing Step 3 before running the test is the safe order — the step text says so explicitly.
- `snapshot()` gained an optional `now=` param (Task 1) — its only production caller is `perf_snapshot()` with no args; additive.
- History points gain `"ts"` (Task 3); `drawGraph` reads only `cpu`/`mem` — unaffected.
