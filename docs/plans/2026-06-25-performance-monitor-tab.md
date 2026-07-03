# Performance Monitor Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Performance tab that discovers every `claude.exe` session on the PC (including orphaned/external ones), rolls up each session's subprocess-tree CPU/memory, graphs usage over time per session, and can kill a session's whole tree.

**Architecture:** A new stdlib-isolated module `perf_monitor.py` uses `psutil` to discover sessions and a background sampler thread to keep a rolling per-session history. `kanban_server.py` starts the sampler and exposes `GET /api/performance` + `POST /api/performance/kill/<pid>`. `kanban.html` adds a polling Performance tab with expandable per-session canvas graphs and a kill button.

**Tech Stack:** Python 3 stdlib + `psutil`; vanilla JS + inline `<canvas>` (no charting library); `http.server`-based `kanban_server.py`.

## Global Constraints

- Spec: `docs/specs/2026-06-25-performance-monitor-tab-design.md`.
- New Python dependency allowed: **`psutil`** (the only one). Everything else stays stdlib.
- `kanban.html` stays **vanilla JS, inline `<script>`, zero front-end dependencies** — graph is hand-drawn on `<canvas>`.
- Sampler interval: **~3s**. History cap: **N=100 points** (~5 min). History key: **`(pid, create_time)`**.
- `psutil` absent must degrade gracefully: API returns `{"available": false, "reason": "psutil not installed"}` with HTTP 200; UI shows an install hint, never a 500.
- All process access wrapped against `psutil.NoSuchProcess` / `psutil.AccessDenied` — skip and continue.
- Kill terminates **children first, then parent**; killing a dead pid is a no-op success.
- Reuse the existing Windows kill approach (`taskkill /F /PID`) consistent with `orchestrator.kill_pid`.
- Run tests with: `python -m pytest tests/ -v` from the `.kanban` directory.
- Work happens on branch `feat/performance-monitor-tab`. Do **not** stage or commit the pre-existing unrelated modified files (CLAUDE.md, kanban.html*, kanban_server.py*, orchestrator*.py, tests/test_orchestrator*.py, tests/test_server_*.py) except the specific files each task modifies. Stage files explicitly by path.

\* These files are pre-modified in the working tree; only stage the exact hunks/files a task instructs.

---

### Task 1: `perf_monitor` core — discover & classify sessions

**Files:**
- Create: `perf_monitor.py`
- Test: `tests/test_perf_monitor.py`

**Interfaces:**
- Consumes: nothing (entry module).
- Produces:
  - `discover_sessions(proc_iter=None, owned_pids=None) -> list[dict]` — each dict:
    `{"pid": int, "kind": "interactive"|"headless", "owned": bool, "board": str|None, "ticket": str|None, "cpuPercent": float, "memoryMB": float, "childCount": int, "children": list[dict], "_createTime": float}`.
    `proc_iter` defaults to `psutil.process_iter` (injectable for tests). `owned_pids` defaults to an empty set.
  - `classify_cmdline(cmdline: list[str]) -> tuple[str, str|None, str|None]` returning `(kind, board, ticket)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_perf_monitor.py
import perf_monitor as pm


class FakeProc:
    def __init__(self, pid, name, cmdline, rss=10*1024*1024, cpu=1.0,
                 create_time=100.0, children=None):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self._rss = rss
        self._cpu = cpu
        self._ct = create_time
        self._children = children or []

    def name(self): return self._name
    def cmdline(self): return self._cmdline
    def create_time(self): return self._ct
    def cpu_percent(self, interval=None): return self._cpu
    def memory_info(self):
        class M: pass
        m = M(); m.rss = self._rss; return m
    def children(self, recursive=False): return self._children


def test_classify_headless_extracts_board_and_ticket():
    cmd = ["claude.EXE", "-p", "Ticket #10 ...\nTicket file: C:\\x\\.kanban\\kanban-dev\\10.json"]
    kind, board, ticket = pm.classify_cmdline(cmd)
    assert kind == "headless"
    assert board == "kanban-dev"
    assert ticket == "10"


def test_classify_interactive_has_no_ticket():
    kind, board, ticket = pm.classify_cmdline(["claude.exe"])
    assert kind == "interactive"
    assert board is None and ticket is None


def test_discover_finds_only_claude_and_rolls_up_children():
    child = FakeProc(22, "bash.exe", ["bash"], rss=8*1024*1024, cpu=2.0)
    claude = FakeProc(10, "claude.exe", ["claude.exe"], rss=100*1024*1024,
                      cpu=4.0, children=[child])
    other = FakeProc(99, "explorer.exe", ["explorer.exe"])
    sessions = pm.discover_sessions(proc_iter=lambda: [claude, other], owned_pids={10})
    assert len(sessions) == 1
    s = sessions[0]
    assert s["pid"] == 10 and s["owned"] is True
    assert s["childCount"] == 1
    # rollup: cpu 4.0 + 2.0, mem (100+8)MB
    assert s["cpuPercent"] == 6.0
    assert round(s["memoryMB"]) == 108
    assert s["children"][0]["name"] == "bash.exe"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_perf_monitor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'perf_monitor'`.

- [ ] **Step 3: Write minimal implementation**

```python
# perf_monitor.py
"""System-wide Claude session discovery + CPU/memory rollup.

Optional dependency on psutil. All public functions accept injectable
process iterators so tests never spawn real processes.
"""
import re

try:
    import psutil  # type: ignore
    PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via available flag
    psutil = None
    PSUTIL_AVAILABLE = False

_TICKET_RE = re.compile(r"[\\/]([^\\/]+)[\\/](\d+)\.json")


def classify_cmdline(cmdline):
    """Return (kind, board, ticket) for a claude process command line."""
    args = list(cmdline or [])
    kind = "headless" if "-p" in args else "interactive"
    board = ticket = None
    blob = "\n".join(args)
    m = _TICKET_RE.search(blob)
    if m:
        board, ticket = m.group(1), m.group(2)
    return kind, board, ticket


def _rollup(proc):
    """Sum cpu_percent and rss for proc + all descendants. Returns
    (cpu, mem_bytes, children_list)."""
    cpu = float(proc.cpu_percent())
    mem = float(proc.memory_info().rss)
    children = []
    try:
        kids = proc.children(recursive=True)
    except Exception:
        kids = []
    for k in kids:
        try:
            kcpu = float(k.cpu_percent())
            kmem = float(k.memory_info().rss)
        except Exception:
            continue
        cpu += kcpu
        mem += kmem
        children.append({
            "pid": k.pid,
            "name": k.name(),
            "cpuPercent": round(kcpu, 1),
            "memoryMB": round(kmem / (1024 * 1024), 1),
        })
    return cpu, mem, children


def discover_sessions(proc_iter=None, owned_pids=None):
    """Find every claude.exe process and roll up its subprocess tree."""
    if proc_iter is None:
        if not PSUTIL_AVAILABLE:
            return []
        proc_iter = psutil.process_iter
    owned = set(owned_pids or ())
    sessions = []
    for p in proc_iter():
        try:
            name = (p.name() or "").lower()
            if name != "claude.exe":
                continue
            kind, board, ticket = classify_cmdline(p.cmdline())
            cpu, mem, children = _rollup(p)
            sessions.append({
                "pid": p.pid,
                "kind": kind,
                "owned": p.pid in owned,
                "board": board,
                "ticket": ticket,
                "cpuPercent": round(cpu, 1),
                "memoryMB": round(mem / (1024 * 1024), 1),
                "childCount": len(children),
                "children": children,
                "_createTime": float(p.create_time()),
            })
        except Exception:
            # NoSuchProcess / AccessDenied / partial-death — skip.
            continue
    return sessions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_perf_monitor.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add perf_monitor.py tests/test_perf_monitor.py
git commit -m "feat: perf_monitor session discovery + tree rollup"
```

---

### Task 2: `PerfSampler` — rolling per-session history + snapshot

**Files:**
- Modify: `perf_monitor.py`
- Test: `tests/test_perf_monitor.py`

**Interfaces:**
- Consumes: `discover_sessions` (Task 1).
- Produces:
  - `class PerfSampler(interval=3.0, cap=100, owned_pids_fn=None)` with:
    - `.sample_once()` — runs one discovery, appends to history, updates cache. Returns the snapshot dict.
    - `.snapshot() -> dict` — `{"available": bool, "sampledAt": str|None, "totals": {...}, "sessions": [...]}` where each session includes a `"history"` list of `{"t","cpu","mem"}`.
    - `.start()` / `.stop()` — daemon thread lifecycle.
  - History keyed by `(pid, create_time)`; capped at `cap`; series for a `(pid, create_time)` dropped when it is absent from a sample.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_perf_monitor.py
import perf_monitor as pm
from tests.test_perf_monitor import FakeProc  # reuse helper if split; else already in module


def _sampler_with(procs):
    s = pm.PerfSampler(interval=0, cap=3)
    s._proc_iter = lambda: procs
    return s


def test_snapshot_accumulates_history_and_totals():
    claude = FakeProc(10, "claude.exe", ["claude.exe"], rss=50*1024*1024, cpu=2.0)
    s = _sampler_with([claude])
    s.sample_once()
    s.sample_once()
    snap = s.snapshot()
    assert snap["available"] is True
    assert snap["totals"]["sessionCount"] == 1
    sess = snap["sessions"][0]
    assert len(sess["history"]) == 2
    assert sess["history"][-1]["cpu"] == 2.0


def test_history_evicts_by_cap():
    claude = FakeProc(10, "claude.exe", ["claude.exe"])
    s = _sampler_with([claude])  # cap=3
    for _ in range(5):
        s.sample_once()
    sess = s.snapshot()["sessions"][0]
    assert len(sess["history"]) == 3


def test_dead_session_history_dropped():
    claude = FakeProc(10, "claude.exe", ["claude.exe"])
    s = _sampler_with([claude])
    s.sample_once()
    s._proc_iter = lambda: []          # process gone
    s.sample_once()
    assert s.snapshot()["sessions"] == []
    assert s._history == {}            # series pruned
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_perf_monitor.py -v`
Expected: FAIL with `AttributeError: module 'perf_monitor' has no attribute 'PerfSampler'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to perf_monitor.py
import threading
import time
from collections import deque
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PerfSampler:
    def __init__(self, interval=3.0, cap=100, owned_pids_fn=None):
        self.interval = interval
        self.cap = cap
        self._owned_pids_fn = owned_pids_fn or (lambda: set())
        self._proc_iter = None  # None => use psutil via discover_sessions
        self._history = {}       # (pid, create_time) -> deque[{"t","cpu","mem"}]
        self._cache = {"available": PSUTIL_AVAILABLE, "sampledAt": None,
                       "totals": {"cpuPercent": 0.0, "memoryMB": 0.0, "sessionCount": 0},
                       "sessions": []}
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def sample_once(self):
        sessions = discover_sessions(proc_iter=self._proc_iter,
                                     owned_pids=self._owned_pids_fn())
        ts = _now_iso()
        live_keys = set()
        tot_cpu = tot_mem = 0.0
        for s in sessions:
            key = (s["pid"], s.pop("_createTime"))
            live_keys.add(key)
            buf = self._history.setdefault(key, deque(maxlen=self.cap))
            buf.append({"t": ts, "cpu": s["cpuPercent"], "mem": s["memoryMB"]})
            s["history"] = list(buf)
            tot_cpu += s["cpuPercent"]
            tot_mem += s["memoryMB"]
        # prune history for sessions no longer present
        for dead in [k for k in self._history if k not in live_keys]:
            del self._history[dead]
        snap = {
            "available": PSUTIL_AVAILABLE,
            "sampledAt": ts,
            "totals": {"cpuPercent": round(tot_cpu, 1),
                       "memoryMB": round(tot_mem, 1),
                       "sessionCount": len(sessions)},
            "sessions": sessions,
        }
        with self._lock:
            self._cache = snap
        return snap

    def snapshot(self):
        with self._lock:
            return self._cache

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:
                pass  # never let the sampler thread die
            self._stop.wait(self.interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="perf-sampler",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
```

> Note: when `proc_iter` is injected (tests), `cpu_percent` returns the fake's value directly. With real psutil the first `cpu_percent()` per process returns 0.0 and subsequent calls return the interval delta — acceptable since the sampler calls repeatedly.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_perf_monitor.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add perf_monitor.py tests/test_perf_monitor.py
git commit -m "feat: PerfSampler rolling per-session history + snapshot"
```

---

### Task 3: `kill_session` — terminate a session tree

**Files:**
- Modify: `perf_monitor.py`
- Test: `tests/test_perf_monitor.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `kill_session(pid, killer=None) -> dict` → `{"killed": [pids], "ok": bool}`. `killer` defaults to a `taskkill /F /PID` (win) / `os.kill` (posix) callable; injectable for tests. Kills children before parent; a missing pid is a no-op success.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_perf_monitor.py
def test_kill_session_kills_children_then_parent(monkeypatch):
    child = FakeProc(22, "bash.exe", ["bash"])
    parent = FakeProc(10, "claude.exe", ["claude.exe"], children=[child])
    monkeypatch.setattr(pm, "_find_proc", lambda pid: parent if pid == 10 else None)
    order = []
    res = pm.kill_session(10, killer=lambda p: order.append(p) or True)
    assert order == [22, 10]          # children first, then parent
    assert res["ok"] is True
    assert res["killed"] == [22, 10]


def test_kill_missing_pid_is_noop_success(monkeypatch):
    monkeypatch.setattr(pm, "_find_proc", lambda pid: None)
    res = pm.kill_session(999, killer=lambda p: True)
    assert res["ok"] is True
    assert res["killed"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_perf_monitor.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'kill_session'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to perf_monitor.py
import os
import subprocess
import sys


def _find_proc(pid):
    if not PSUTIL_AVAILABLE:
        return None
    try:
        return psutil.Process(pid)
    except Exception:
        return None


def _default_killer(pid):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def kill_session(pid, killer=None):
    killer = killer or _default_killer
    proc = _find_proc(pid)
    killed = []
    if proc is None:
        return {"killed": killed, "ok": True}
    try:
        kids = proc.children(recursive=True)
    except Exception:
        kids = []
    for k in kids:
        if killer(k.pid):
            killed.append(k.pid)
    if killer(pid):
        killed.append(pid)
    return {"killed": killed, "ok": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_perf_monitor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add perf_monitor.py tests/test_perf_monitor.py
git commit -m "feat: kill_session terminates a session tree children-first"
```

---

### Task 4: Server wiring — sampler boot + API routes

**Files:**
- Modify: `kanban_server.py` (add module-level sampler + two handler functions near the other `orch_*` helpers ~line 672–745; start sampler in `run()` ~line 941; add routes in `do_GET` ~line 763 and `do_POST` ~line 834)
- Test: `tests/test_server_performance.py`

**Interfaces:**
- Consumes: `perf_monitor.PerfSampler`, `perf_monitor.kill_session` (Tasks 2–3); `orchestrator._PROCS` for owned pids.
- Produces:
  - `GET /api/performance` → `perf_snapshot()` → `(snapshot_dict, 200)`.
  - `POST /api/performance/kill/<pid>` → `perf_kill(pid)` → `({"killed":[...],"ok":true}, 200)`; non-int pid → `({"error":"bad pid"}, 400)`.
  - Module-level `_PERF_SAMPLER` started once in `run()` and on the orchestrator-enable path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server_performance.py
import kanban_server as ks


def test_perf_snapshot_returns_sampler_cache(monkeypatch):
    fake = {"available": True, "sampledAt": "t", "totals": {}, "sessions": []}
    class FakeSampler:
        def snapshot(self): return fake
    monkeypatch.setattr(ks, "_PERF_SAMPLER", FakeSampler())
    data, status = ks.perf_snapshot()
    assert status == 200
    assert data is fake


def test_perf_kill_validates_pid(monkeypatch):
    monkeypatch.setattr(ks.perf_monitor, "kill_session",
                        lambda pid: {"killed": [pid], "ok": True})
    data, status = ks.perf_kill("10")
    assert status == 200 and data["killed"] == [10]
    data, status = ks.perf_kill("notanint")
    assert status == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server_performance.py -v`
Expected: FAIL with `AttributeError: module 'kanban_server' has no attribute 'perf_snapshot'`.

- [ ] **Step 3: Write minimal implementation**

Add near the top imports of `kanban_server.py` (after `import orchestrator_core as _oc`):

```python
import perf_monitor
```

Add module-level sampler + helpers (place beside the other `orch_*` functions, e.g. after `orch_answer`, ~line 745):

```python
# --- Performance monitor ----------------------------------------------------

def _owned_pids():
    """PIDs the orchestrator spawned, for owned/external tagging."""
    try:
        import orchestrator as _orch
        return set(_orch._PROCS.keys())
    except Exception:
        return set()


_PERF_SAMPLER = perf_monitor.PerfSampler(owned_pids_fn=_owned_pids)


def ensure_perf_sampler_running():
    try:
        _PERF_SAMPLER.start()
    except Exception:
        pass


def perf_snapshot():
    return _PERF_SAMPLER.snapshot(), 200


def perf_kill(pid_str):
    try:
        pid = int(pid_str)
    except (TypeError, ValueError):
        return {"error": "bad pid"}, 400
    return perf_monitor.kill_session(pid), 200
```

Add the GET route in `do_GET` (after the `/api/orchestrator/activity` branch, ~line 764):

```python
        elif path == "/api/performance":
            self._json(*perf_snapshot())
```

Add the POST route in `do_POST` (after the orchestrator `kill` branch, ~line 835):

```python
        # POST /api/performance/kill/<pid>
        elif len(parts) == 5 and parts[1] == "api" and parts[2] == "performance" and parts[3] == "kill":
            self._json(*perf_kill(unquote(parts[4])))
```

Start the sampler in `run()` (next to `ensure_orchestrator_running()`, ~line 941):

```python
    ensure_perf_sampler_running()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server_performance.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite to check no regressions**

Run: `python -m pytest tests/ -v`
Expected: PASS (existing tests + new ones).

- [ ] **Step 6: Commit**

```bash
git add kanban_server.py tests/test_server_performance.py
git commit -m "feat: /api/performance snapshot + kill routes and sampler boot"
```

---

### Task 5: UI — Performance tab with expandable per-session graph

**Files:**
- Modify: `kanban.html` (tab button ~line 92; view-panel `<div>` ~line 274; `switchView` ~line 790–799; add a `renderPerformance` block + poll loop near `renderOrchestrator` ~line 898)

**Interfaces:**
- Consumes: `GET /api/performance`, `POST /api/performance/kill/<pid>`; existing `apiFetch`, `showToast`, `$` helpers.
- Produces: a `Performance` tab that polls every 3s while active, renders a session table with kill buttons and expandable canvas graphs.

- [ ] **Step 1: Add the tab button**

In the `.view-tabs` div (~line 92), after the Orchestrator button:

```html
    <button class="view-tab" data-view="performance">Performance</button>
```

- [ ] **Step 2: Add the view panel**

After the `view-orchestrator` div (~line 274):

```html
<div class="view-panel" id="view-performance" style="display:none;padding:18px;overflow:auto;height:calc(100vh - 52px);"></div>
```

- [ ] **Step 3: Wire `switchView` to show/poll the tab**

In `switchView(view)` (~line 795), alongside the orchestrator line, add:

```javascript
  $("view-performance").style.display = view==="performance" ? "block" : "none";
```

and after the orchestrator render line (~line 797):

```javascript
  if(view==="performance"){ perfActive=true; renderPerformance(); } else { perfActive=false; }
```

- [ ] **Step 4: Add the render + poll + graph code**

Place near `renderOrchestrator` (~line 898). This is the full block — copy verbatim:

```javascript
// ── Performance tab ──────────────────────────────────────────────
let perfActive = false;
const perfExpanded = new Set();   // pids whose graph is open

function drawGraph(canvas, history){
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0,0,W,H);
  if(!history || history.length < 2){
    ctx.fillStyle = "#888"; ctx.font = "11px sans-serif";
    ctx.fillText("collecting…", 8, H/2); return;
  }
  const cpu = history.map(p=>p.cpu), mem = history.map(p=>p.mem);
  const maxCpu = Math.max(10, ...cpu), maxMem = Math.max(1, ...mem);
  const plot = (vals, max, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
    vals.forEach((v,i)=>{
      const x = (i/(vals.length-1))*(W-8)+4;
      const y = H-4 - (v/max)*(H-12);
      i?ctx.lineTo(x,y):ctx.moveTo(x,y);
    });
    ctx.stroke();
  };
  plot(cpu, maxCpu, "#3b82f6");   // CPU% — blue
  plot(mem, maxMem, "#10b981");   // mem  — green
}

async function renderPerformance(){
  const wrap = $("view-performance");
  let snap;
  try { snap = await apiFetch("/api/performance"); }
  catch(e){ wrap.innerHTML = "<p>Failed to load performance data.</p>"; return; }
  if(!snap.available){
    wrap.innerHTML = "<p>Process monitoring needs <code>psutil</code>. Run "
      + "<code>pip install psutil</code> and restart the server.</p>";
    return;
  }
  const t = snap.totals || {};
  let html = "<div style='margin-bottom:12px;font-size:13px;color:#666'>"
    + "Sessions: <b>"+(t.sessionCount||0)+"</b> &nbsp; CPU: <b>"+(t.cpuPercent||0)
    + "%</b> &nbsp; Mem: <b>"+(t.memoryMB||0)+" MB</b> &nbsp; @ "+(snap.sampledAt||"")
    + "</div>";
  html += "<table style='width:100%;border-collapse:collapse;font-size:13px'>";
  for(const s of (snap.sessions||[])){
    const open = perfExpanded.has(s.pid);
    const badge = (txt,bg)=>"<span style='background:"+bg+";color:#fff;border-radius:3px;"
      +"padding:1px 6px;margin-left:6px;font-size:11px'>"+txt+"</span>";
    html += "<tr data-pid='"+s.pid+"' class='perf-row' style='border-top:1px solid #eee;cursor:pointer'>"
      + "<td style='padding:6px 4px'>"+(open?"▾":"▸")+" PID "+s.pid
      +   badge(s.kind, s.kind==="headless"?"#a855f7":"#0ea5e9")
      +   badge(s.owned?"owned":"external", s.owned?"#64748b":"#ef4444")
      +   (s.ticket?(" "+s.board+"#"+s.ticket):"")
      + "</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.cpuPercent+"%</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.memoryMB+" MB</td>"
      + "<td style='padding:6px 4px;text-align:right'>"+s.childCount+" sub</td>"
      + "<td style='padding:6px 4px;text-align:right'>"
      +   "<button class='perf-kill' data-pid='"+s.pid+"'>Kill</button></td>"
      + "</tr>";
    if(open){
      let kids = (s.children||[]).map(c=>"PID "+c.pid+" "+c.name+" ("+c.cpuPercent
        +"%, "+c.memoryMB+"MB)").join("<br>") || "<i>no subprocesses</i>";
      html += "<tr><td colspan='5' style='padding:8px 16px;background:#fafafa'>"
        + "<canvas width='520' height='90' data-pid='"+s.pid
        +   "' style='display:block;margin-bottom:8px;border:1px solid #eee'></canvas>"
        + "<div style='font-size:11px;color:#3b82f6'>■ CPU%</div>"
        + "<div style='font-size:11px;color:#10b981'>■ Memory</div>"
        + "<div style='margin-top:6px;font-size:12px;color:#555'>"+kids+"</div>"
        + "</td></tr>";
    }
  }
  html += "</table>";
  wrap.innerHTML = html;

  // draw graphs for expanded rows
  wrap.querySelectorAll("canvas[data-pid]").forEach(c=>{
    const s = (snap.sessions||[]).find(x=>String(x.pid)===c.dataset.pid);
    if(s) drawGraph(c, s.history);
  });
  // expand/collapse
  wrap.querySelectorAll(".perf-row").forEach(r=>{
    r.addEventListener("click", e=>{
      if(e.target.classList.contains("perf-kill")) return;
      const pid = +r.dataset.pid;
      perfExpanded.has(pid) ? perfExpanded.delete(pid) : perfExpanded.add(pid);
      renderPerformance();
    });
  });
  // kill
  wrap.querySelectorAll(".perf-kill").forEach(b=>{
    b.addEventListener("click", async e=>{
      e.stopPropagation();
      await apiFetch("/api/performance/kill/"+encodeURIComponent(b.dataset.pid), {method:"POST"});
      showToast("Kill sent to PID "+b.dataset.pid);
      renderPerformance();
    });
  });
}

setInterval(()=>{ if(perfActive) renderPerformance(); }, 3000);
```

- [ ] **Step 5: Manual smoke test**

Run the server: `python kanban_server.py` (from `.kanban`). Open `http://localhost:8745`, click **Performance**.
Expected: the tab lists this interactive `claude.exe` session (badged `interactive` / `external`), updating every ~3s. Click a row → graph canvas appears and starts plotting after 2+ samples. Confirm the **Kill** button is present (do not click it on your own live session).

- [ ] **Step 6: Commit**

```bash
git add kanban.html
git commit -m "feat: Performance tab UI with per-session usage graph and kill"
```

---

### Task 6: Dependency note + full verification

**Files:**
- Modify: `docs/specs/2026-06-25-performance-monitor-tab-design.md` is reference-only; create/append `requirements.txt` if the repo has one, else note in `CLAUDE.md` install section. Check first.

- [ ] **Step 1: Check for an existing requirements file**

Run: `ls requirements*.txt 2>/dev/null; grep -rn "psutil" . --include=*.txt --include=*.md | head`
If a `requirements.txt` exists, add `psutil`. If not, skip the file and add a one-line install note to `.kanban/CLAUDE.md` under server setup (only that one line — leave other pre-existing CLAUDE.md edits alone).

- [ ] **Step 2: Add the dependency note**

If `requirements.txt` exists, add line `psutil`. Otherwise append to the server section of `CLAUDE.md`:

```markdown
> The Performance tab needs `psutil` (`pip install psutil`). Without it the tab shows an install hint and the rest of the server works normally.
```

- [ ] **Step 3: Full suite + import sanity**

Run: `python -m pytest tests/ -v && python -c "import perf_monitor, kanban_server; print('imports ok')"`
Expected: all tests PASS and `imports ok` printed.

- [ ] **Step 4: Commit**

```bash
git add -A -- requirements.txt CLAUDE.md 2>/dev/null
git commit -m "docs: note psutil dependency for Performance tab"
```

---

## Self-Review

**Spec coverage:**
- Discover all `claude.exe` + classify interactive/headless + board/ticket → Task 1. ✓
- Subprocess-tree CPU/mem rollup → Task 1 `_rollup`. ✓
- Owned vs external tagging via `_PROCS` → Task 1 (`owned_pids`) + Task 4 (`_owned_pids`). ✓
- Background sampler, ~3s, snapshot cache → Task 2. ✓
- Rolling per-session history, N=100, `(pid, create_time)` key, prune on death → Task 2. ✓
- `kill_session` children-first, dead pid no-op → Task 3. ✓
- `GET /api/performance` + `POST /api/performance/kill/<pid>` + sampler boot → Task 4. ✓
- psutil-absent graceful fallback → Task 1 (`PSUTIL_AVAILABLE`/`available`) + Task 5 UI hint. ✓
- Performance tab, 3s poll, table, expandable canvas graph (CPU+mem), kill button → Task 5. ✓
- Per-process error handling (NoSuchProcess/AccessDenied) → Task 1/3 try/except. ✓
- Dependency documentation → Task 6. ✓

**Placeholder scan:** No TBD/TODO; all code shown in full; commands have expected output. ✓

**Type consistency:** `discover_sessions(proc_iter, owned_pids)`, `PerfSampler(interval,cap,owned_pids_fn)`, `kill_session(pid, killer)`, `perf_snapshot()`, `perf_kill(pid_str)`, session dict keys (`cpuPercent`, `memoryMB`, `childCount`, `children`, `history`, `_createTime`) are used identically across tasks and the JS consumer. ✓
