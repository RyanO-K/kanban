# Performance tab usability — design

**Date:** 2026-07-08
**Status:** approved (design review with user)
**Motivation:** During a severe-CPU incident (kernel-amplified spawn churn from
crash-looping dispatches, plus the server's own hot scan loop), the Performance
tab had the underlying data but surfaced none of it. The user diagnosed the
problem with external tools. This design makes the tab surface problems
automatically, reads at a glance, and makes its actions safe — without adding
meaningful sampler cost.

## Scope

In scope:

- Server-computed problem detectors: **sustained high CPU** and **spawn churn**.
- The kanban server's **own python process** shown as a pinned pseudo-session.
- Readability: friendly names first (ticket/board, labels, conversation
  summary), PIDs demoted; flagged-row highlighting; alert banner strip.
- Actions: **kill confirmation with subprocess-tree preview**; **jump to
  ticket/log** for ticket-bound sessions; **Restart server** on the server row.

Out of scope (explicitly declined in review):

- Whole-machine / kernel CPU monitoring.
- Crash-loop dispatch detection (activity-feed analysis).
- Runaway-session-count detection.
- Orchestrator ON/OFF / Stop All controls on this tab.

Constraint: **no new polling, no new subprocess spawns, no meaningful sampler
CPU**. Everything rides the existing 3-second sample cycle and the data it
already collects.

## Architecture (Approach A — server-computed signals)

All detection logic lives server-side in `PerfSampler` (perf_monitor.py), which
already keeps per-session rolling history keyed by `(pid, create_time)`. The
API ships ready-made alerts and per-session signal fields; the UI only renders.
Rationale: detectors are unit-testable in pytest next to the existing sampler
tests, thresholds live in one place, and spawn churn *requires* server-side
state (consecutive child-PID sets never cross the wire).

## Components

### 1. perf_monitor.py — sampler additions

- **Self pseudo-session.** Each `sample_once` appends an entry for
  `os.getpid()` with `kind: "server"`, `owned: true` — CPU + RSS of the server
  process **only, no children rollup**: any claude.exe it spawned (triage,
  summarizer, agents) already appears as its own session, and rolling them up
  here would double-count. It gets history and detector treatment like the
  rest.
- **Spawn churn.** Per session key, keep the previous sample's set of child
  PIDs. `new_pids = current - previous`; push `len(new_pids)` with a timestamp
  into a small deque; `spawnRate` = new PIDs over the last 60s (per minute).
  Cost: one set-diff per session per cycle.
- **Sustained CPU.** `cpuAvg2m` = mean of the existing history deque's `cpu`
  values over the last ~120s (≈40 samples). No new data collected.
- **Alerts.** Module constants `ALERT_CPU_AVG = 50.0` (percent of one core,
  2-minute average) and `ALERT_SPAWN_RATE = 10` (new child PIDs per minute).
  Snapshot gains `alerts: [{severity: "warn", pid, kind, message}]` with
  plain-language messages, e.g.
  `"kanban-dev #103 spawned 14 processes in the last minute"`.
  A session must have ≥60s of history before it can alert (no cold-start
  false positives).

### 2. /api/performance — additive shape changes

Per session: `spawnRate` (float, per minute), `cpuAvg2m` (float),
`flags: ["highCpu"|"spawnChurn", ...]`. Snapshot: `alerts` list as above.
Nothing existing is renamed or removed; old clients keep working.

### 3. kanban.js / kanban.html — UI

- **Alert banner strip** above the table: one line per alert, warning-styled,
  clicking it scrolls to/expands the offending row.
- **Row identity, friendly-first.** Headless ticket agents render
  `<board> #<id> — <ticket title>` (title looked up from board data already
  loaded in the UI, falling back to `<board> #<id>` alone when that board
  isn't loaded; no new API); orchestrator utility ops render their existing
  label ("Triage: …", "Summarizing …"); interactive sessions render their
  `conversationSummary` snippet; PID becomes small muted text.
- **Flagged rows** get a warning background tint and a small reason chip
  ("high CPU", "spawn churn").
- **Server row** pinned first, labelled "kanban server", with **Restart**
  (POSTs the existing server-restart endpoint) instead of Kill.
- **Kill confirmation modal**: lists the subprocess tree that will die (from
  `children`, already in the payload) and, for ticket agents, states that the
  ticket returns to the board for re-triage. Confirm/cancel; no instant kill.
- **Jump to ticket/log**: ticket-bound rows get links that open the ticket
  card and its live-log panel (both already exist in the UI; this is wiring,
  not new views).

## Error handling

- Detector code is wrapped like the rest of `sample_once` (the sampler thread
  never dies on an exception; a failed detector cycle just skips flags).
- A vanished process mid-cycle (NoSuchProcess/AccessDenied) is skipped exactly
  as today; its churn state is pruned with the existing history pruning.
- The self pseudo-session must never be killable through the claude-kill path:
  the kill endpoint already targets PIDs, so the UI simply never offers Kill
  for `kind: "server"`, and the server-side kill handler refuses
  `pid == os.getpid()` as defense in depth.

## Testing

- Unit tests in `.kanban/tests/` following `test_perf_monitor.py`'s injectable
  fake-process pattern:
  - spawn-rate: new child PIDs across fake samples produce the right per-minute
    rate; stable children produce 0.
  - sustained CPU: history below/above threshold toggles the flag only after
    ≥60s of history.
  - alerts: correct message text, no alerts during cold start, self session
    present with `kind: "server"`.
  - kill guard: server-side kill handler refuses the server's own PID.
- UI verified manually (no JS test harness exists in this repo).

## Non-goals / future ideas (not planned)

Crash-loop detection from the activity feed and a whole-machine kernel gauge
were considered and declined for now; they can be added as new detectors on the
same alert plumbing later.
