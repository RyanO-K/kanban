# Performance Monitor Tab — design

**Date:** 2026-06-25
**Status:** Approved (user pre-approved spec + plan)

## Goal

Add a **Performance** tab to the kanban UI (`kanban.html`) that discovers **every**
`claude.exe` process running on the PC — including sessions the orchestrator did **not**
spawn (orphaned agents, other interactive sessions) — rolls up each session's full
subprocess tree (e.g. `bash.exe`, `git`, `node`), and shows CPU + memory per session.

The tab lets the user:
- See all live Claude sessions, tagged interactive vs headless and owned vs external.
- Expand each session to view a **CPU% + memory graph over time** and its subprocess tree.
- **Kill** any session and its entire child tree.

Motivation: the orchestrator currently only knows about agents it spawned (its `_PROCS`
registry). Orphaned/externally-launched sessions are invisible to it, which is exactly the
failure mode that left stray `claude.exe`/`bash.exe` processes running on the PC.

## Components

### 1. `perf_monitor.py` (new module, single purpose)

Depends on `psutil`.

- `discover_sessions()` — walk `psutil.process_iter()`, find all `claude.exe`. Classify each
  as `interactive` or `headless` (cmdline contains `-p`). For headless sessions, extract the
  board/ticket from the cmdline when present. For each session, walk
  `proc.children(recursive=True)` to build the subprocess tree and roll up `cpu_percent`
  and `memory_info().rss` across the whole tree.
- **Owned tagging** — cross-reference the orchestrator's `_PROCS` registry so each session is
  tagged `owned: true|false` (orchestrator-spawned vs external/orphan).
- `PerfSampler` daemon thread — every ~3s calls `discover_sessions()` and:
  - Stores the latest snapshot in a module-level cache (guarded by a lock).
  - Appends each session's `(timestamp, cpuPercent, memoryMB)` rollup to a **rolling ring
    buffer keyed by `(pid, create_time)`**, capped at **N=100 points (~5 min at 3s)**.
  - Maintains a persistent `psutil.Process` map so `cpu_percent` reflects real interval
    deltas (not first-call zeros).
  - Prunes a pid's history after it disappears (short grace period) so dead sessions don't
    leak memory. Keying on `(pid, create_time)` prevents OS pid reuse from inheriting a dead
    session's series.
  - Never lets an exception kill the loop (catch-all per tick, log, continue).
- `snapshot()` — returns the cached snapshot (with per-session `history`) instantly.
- `kill_session(pid)` — terminate the process tree (children first, then parent), reusing the
  same `taskkill /F /PID` approach already in `orchestrator.kill_pid`. Killing an
  already-dead pid is a no-op success.

### 2. `kanban_server.py`

- Start the `PerfSampler` thread on boot (next to `ensure_orchestrator_running`).
- `GET /api/performance` → `perf_monitor.snapshot()`.
- `POST /api/performance/kill/<pid>` → `perf_monitor.kill_session(pid)`.
- **Graceful fallback** — if `psutil` is not importable, the endpoint returns
  `{"available": false, "reason": "psutil not installed"}` (HTTP 200) so the UI shows a
  friendly "run `pip install psutil`" message instead of a 500.

### 3. `kanban.html`

- Add a `Performance` view-tab button + `<div id="view-performance">` panel, following the
  exact pattern of the existing `orchestrator`/`profiles` tabs.
- While the tab is active and visible, poll `/api/performance` every **3s** and render a
  table: session (pid + interactive/headless badge + owned/external badge + board#ticket),
  CPU%, memory, # subprocesses, and a **Kill** button. A totals summary row sits at the top.
- Each session row has a **disclosure toggle** (▸/▾). Expanding reveals a drop-down area with:
  - A small **CPU% + memory line graph over time** (dual series, shared time axis), drawn into
    an inline `<canvas>` by a ~40-line vanilla `drawGraph(canvas, points)` function — **no
    charting library** (matches the file's vanilla-JS, no-dependency house style).
  - The subprocess tree list.
- The graph redraws each 3s poll from the session's `history` array, animating as new points
  arrive while expanded. Collapsed rows skip drawing (cheap).

### Why server-side history

The client only polls while the tab is open. Keeping history in the server sampler means the
graph shows the **last ~5 minutes immediately on expand** (and survives a browser refresh),
rather than starting empty and filling in only going forward.

## Data shape (`GET /api/performance`)

```json
{
  "available": true,
  "sampledAt": "2026-06-25T15:50:00+00:00",
  "totals": {"cpuPercent": 12.4, "memoryMB": 880, "sessionCount": 3},
  "sessions": [
    {
      "pid": 13988,
      "kind": "interactive",
      "owned": false,
      "board": null,
      "ticket": null,
      "cpuPercent": 4.1,
      "memoryMB": 410,
      "childCount": 2,
      "history": [
        {"t": "2026-06-25T15:49:00+00:00", "cpu": 3.2, "mem": 402},
        {"t": "2026-06-25T15:49:03+00:00", "cpu": 4.1, "mem": 410}
      ],
      "children": [
        {"pid": 22, "name": "bash.exe", "cpuPercent": 0.0, "memoryMB": 8}
      ]
    }
  ]
}
```

`cpuPercent`/`memoryMB` and the `history` `cpu`/`mem` values are the **tree rollup**
(session process + all descendants), matching the headline numbers.

## Error handling

- Processes can die mid-walk → wrap per-process access in try/except for
  `psutil.NoSuchProcess` / `psutil.AccessDenied`; skip and continue.
- Sampler thread catches all exceptions per tick, logs, and continues.
- `psutil` absent → API reports `available: false`; UI degrades to an install hint.
- Kill of an already-dead pid → no-op success.

## Testing (`tests/test_perf_monitor.py`)

Monkeypatch `psutil.process_iter` with fake process objects (mirroring how the orchestrator
tests monkeypatch dispatch) — **no real processes spawned**. Cases:

- Classification: interactive vs headless (`-p` in cmdline); board/ticket extraction.
- Tree rollup math: parent + recursive children CPU/mem summed correctly.
- Owned tagging against a fake `_PROCS` registry.
- Ring buffer: accumulates points, evicts by the N=100 cap, and drops a pid's series on death.
- `(pid, create_time)` keying: a reused pid with a new create_time starts a fresh series.
- `psutil`-absent fallback returns `{"available": false, ...}`.

## Scope boundaries (YAGNI)

- Current snapshot + fixed ~5-min in-memory history window only. No disk persistence.
- No zoom/pan/export on the graph; one canvas line graph per expanded session.
- No per-subprocess kill — kill is whole-session-tree only.
- No historical charts beyond the rolling window.
