# Kanban Orchestrator — Design

**Ticket:** `.kanban/kanban-dev/6.json`
**Date:** 2026-06-24
**Status:** Approved design, ready for implementation plan

## Summary

An orchestrator that watches the entire `.kanban/` board and autonomously works
tickets by dispatching headless `claude -p` sub-agents. Opus acts as the "brain":
it reads each eligible ticket, selects the best-fit named **profile** and model,
and dispatches a sub-agent to do the work. Blocked tickets that need a human
surface as **structured, typed questions** in a dedicated Orchestrator tab on the
kanban HTML. The orchestrator can be turned on/off and its concurrency capped from
the UI, and individual sub-agents (or all of them) can be killed from the UI or
reaped autonomously by the loop.

The user can also open a normal `claude` CLI in the `.kanban` workspace to talk to
the orchestrator — it reads the same JSON files and shares the same triage "brain"
prompt, so the conversation always reflects exactly what the loop is doing.

## Goals

- Autonomously take eligible tickets (deps met) from any board to `completed` or
  `blocked` (needs-human) without human typing.
- Opus selects a named profile + model per ticket from natural-language `whenToUse`
  descriptions — no ticket tagging required.
- Runs as a headless loop ticking ~60s, controllable (on/off, concurrency, kill,
  stop-all) from the kanban UI.
- Structured human-attention inbox: sub-agents ask typed questions; the UI renders
  a form per question type; answering auto re-dispatches the ticket.
- Conversational: a `claude` CLI session can inspect and steer the orchestrator via
  the shared files and shared triage prompt.

## Non-goals

- Full file-locking / multi-machine coordination (single-machine, low-frequency
  board; concurrent-write mitigation is by field ownership, documented as a known
  limitation).
- Cloud scheduling (`/schedule` / CronCreate). The loop runs locally; OS Task
  Scheduler may later launch it, but that is out of scope here.
- Tag-based or rule-based profile matching (explicitly rejected in favor of
  `whenToUse` + Opus judgment).

## Architecture

Four components, all sharing the `.kanban/` directory as the single source of truth.

```
 orchestrator.py  ──reads/writes──►  .kanban/ JSON files  ◄──reads/writes── kanban_server.py
 (headless loop,                      - <board>/<id>.json                    (+ profiles API,
  ticks ~60s)                         - config/*.json   (profiles)            + kill endpoint,
       │ spawns                       - _orchestrator/  (state/log/runs)       + state toggle)
       ▼                                                                            │ serves
  claude -p (sub-agent per ticket)                                            kanban.html
                                                                             (+ Orchestrator tab,
  You ◄── normal `claude` CLI in .kanban ──► same JSON files                  + Profiles tab)
```

### 1. `orchestrator.py` — headless engine
Started once (like the server). Each tick (~60s) it reads control state, reaps
finished/killed/unproductive agents, finds eligible tickets, asks Opus to triage,
and dispatches sub-agents up to the concurrency cap. Appends to the activity log.

### 2. `config/<name>.json` — named profiles
One file per profile. Never a board (the directory has no `_meta.json`, so the
server's existing board detection ignores it). Managed from the Profiles tab.

### 3. `kanban_server.py` (extended)
Adds: Profiles CRUD API, an instant-kill endpoint, and orchestrator state read/write
(on/off, concurrency cap, stop-all). Existing board scan already ignores non-board
dirs, so `config/` and `_orchestrator/` won't appear as boards.

### 4. `kanban.html` (extended)
Two new tabs (follows existing vanilla-JS patterns):
- **Profiles** — manage profile JSONs; set the concurrency cap.
- **Orchestrator** — on/off toggle, "Stop all" button, activity feed, and the
  **human-attention inbox** of typed question forms with per-ticket kill buttons.

## Data shapes

### Profile — `.kanban/config/<name>.json`
```json
{
  "name": "frontend",
  "displayName": "Frontend Specialist",
  "whenToUse": "UI work, HTML/CSS/JS, kanban.html changes, visual design tickets.",
  "model": "claude-opus-4-8",
  "allowedTools": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
  "systemPrompt": "You are a frontend specialist working a kanban ticket...",
  "enabled": true
}
```
`model` is a default; Opus may override per-ticket based on its estimation.

### Orchestrator control state — `.kanban/_orchestrator/state.json`
UI writes via server; loop reads each tick.
```json
{ "enabled": true, "concurrencyCap": 3, "stopAllRequested": false }
```
- `enabled: false` → loop still ticks but does reaping only (no new dispatch).
- `stopAllRequested: true` → loop kills all in-flight agents, then resets the flag.

### Activity log — `.kanban/_orchestrator/activity.json`
Append-only; the Orchestrator tab renders it.
```json
{ "entries": [
  { "ts": "...", "kind": "dispatch", "board": "kanban-dev", "ticket": "7",
    "profile": "frontend", "model": "claude-opus-4-8", "reason": "UI ready-column work" },
  { "ts": "...", "kind": "needs_human", "ticket": "4", "message": "Ambiguous auth scheme" },
  { "ts": "...", "kind": "reap", "ticket": "9", "reason": "no progress in 15m, killed" }
] }
```
`kind` is one of: `dispatch`, `complete`, `needs_human`, `reap`, `error`.

### In-flight marker on the ticket — `.kanban/<board>/<id>.json`
```json
{
  "orchestrator": {
    "state": "dispatched",
    "profile": "frontend",
    "model": "claude-opus-4-8",
    "pid": 24817,
    "dispatchedAt": "...",
    "killRequested": false,
    "logFile": ".kanban/_orchestrator/runs/7-<ts>.log"
  },
  "claudeSessionId": "..."
}
```
`orchestrator.state` is one of: `dispatched`, `done`, `reaped`, `blocked`.

### Structured human-attention question — on the ticket's `orchestrator` block
```json
"question": {
  "id": "q-<ts>",
  "type": "input",
  "format": "text",
  "prompt": "Which auth scheme for the API?",
  "options": ["OAuth2", "API key", "Session cookie"],
  "multi": false,
  "askedAt": "...",
  "answer": null,
  "answeredAt": null
}
```
Two question types:
- `input` — single field; `format` (`text` | `number`) is a validation hint only.
- `choice` — radio buttons, or checkboxes when `multi: true`. A yes/no confirm is a
  2-option choice.

**Every question also renders a free-text "Notes" box.** The answer is always:
```json
"answer": { "value": "OAuth2", "notes": "Use the existing session middleware in auth.py" }
```
`value` may be null if only notes were used. The sub-agent always receives both, and
is instructed to treat `notes` as authoritative human intent (overriding a
badly-formed question). Answering writes `answer` + `answeredAt`; the ticket
auto re-dispatches on the next eligible tick.

## Control flow — the loop (each ~60s tick)

1. **Read state.** Load `state.json`.
   - `stopAllRequested` → kill every in-flight PID, mark those tickets `blocked` +
     comment, clear markers, reset flag.
   - `enabled == false` → do step 2 only (reaping), skip dispatch.
2. **Reap.** For each ticket with `orchestrator.state == "dispatched"`:
   - `killRequested` → terminate PID, mark ticket `blocked`, comment, log `reap`.
   - Process exited → read result; move ticket to `completed`, or `blocked` if it
     wrote a `question` / `NEEDS HUMAN`; record `claudeSessionId`; log `complete` or
     `needs_human`. If it exited non-zero with no result → `blocked` +
     `NEEDS HUMAN: agent exited unexpectedly` + tail of log; log `error`.
   - Running too long with no progress → Opus judges "productive?"; if not,
     terminate + log `reap`.
3. **Find eligible tickets.** Across all boards: any ticket whose `dependsOn` are all
   `completed` (or none) and not already in-flight. (Subsumes ticket #7's "Ready".)
   Includes tickets whose question was just answered (auto re-dispatch).
4. **Triage & dispatch (up to cap).** If in-flight < `concurrencyCap`: send Opus the
   eligible tickets + all profile `whenToUse` descriptions. Opus returns, per ticket
   it chooses to act on, `{ticket, profile, model, reason}`. Validate the profile
   exists and response parses (else log + skip). For each until cap hit: write the
   in-flight marker, move ticket to `in_progress`, spawn `claude -p` with the
   profile prompt + ticket detail + relevant context (incl. a prior answered question
   if re-dispatching), capture PID + log file.
5. **Log & sleep.** Append activity entries; sleep ~60s.

### Sub-agent contract
Each `claude -p` agent is told: do the ticket; on completion write a summary
`comment` to the ticket JSON; if it needs a human, set its ticket `status` to
`blocked` and write a structured `orchestrator.question`. The loop keys off that on
reap.

### Shared brain
The triage prompt (profile selection + model estimation) is a single reusable prompt
file invoked by both the loop and the interactive CLI session, so both answer "what
would the orchestrator do with ticket X?" identically.

## Kill / control semantics
- **Instant kill (UI):** `POST /api/orchestrator/kill/<board>/<ticket>` → server
  terminates the PID directly (verifying it is the recorded `claude` run via log
  file / start time before signaling). If the PID is unreachable, it falls back to
  setting `killRequested` and reports "kill queued"; the loop reaps next tick.
- **Autonomous reap:** the loop kills agents it judges unproductive (step 2).
- **On/off:** `enabled:false` pauses new dispatch only; in-flight agents run to
  completion.
- **Stop all:** a distinct button sets `stopAllRequested`; loop kills everything
  in flight on its next tick.
- **Concurrency lowered below in-flight count:** never kills to comply; just stops
  new dispatch until natural completion brings it under.

## Error handling & edge cases
- **Malformed profile/config JSON** — skipped with a logged warning; never crashes a
  tick (server already swallows per-file `JSONDecodeError`).
- **Sub-agent crash / non-zero exit** — ticket → `blocked` with
  `NEEDS HUMAN: agent exited unexpectedly` + log tail. No silent loss.
- **Stale in-flight marker (loop restart)** — on startup, any `dispatched` ticket
  whose PID is gone is treated as a crashed agent.
- **PID reuse** — verify the process matches the recorded run before killing.
- **Concurrent writes to one ticket** — mitigated by field ownership: the loop writes
  only the `orchestrator` block + `status`; the sub-agent writes only `comments` and
  `question`/result fields. Read-modify-write of the whole file, kept brief.
  Documented known limitation (no full locking).
- **Opus triage returns nonsense** — validate profile name + JSON parse; on failure
  log and skip dispatch that tick.

## Testing
- **Unit (Python):** eligibility, reap-decision, triage-response validation/parsing,
  question/answer round-trip shape, state read/write.
- **Dispatch seam:** loop calls `spawn_agent(ticket, profile)`; tests inject a fake
  that simulates exit/crash/needs-human without launching real `claude`.
- **Server endpoints:** profiles CRUD, kill, state toggle tested against a temp
  `.kanban/` dir (existing server test style).
- **HTML:** manual verification of the two new tabs (no existing test harness);
  follows current vanilla-JS patterns.
- **E2E smoke:** one manual run with a real cheap-model `claude -p` against a
  throwaway ticket to confirm loop → dispatch → reap → comment.

## Relationship to other tickets
- **#7 (Ready column):** eligibility logic (deps met, not started) is exactly the
  "ready" notion; the orchestrator computes it. The Ready column UI can consume the
  same computation.
- **#5 (GitHub integration), #1 (file path on cards, done):** independent; no
  conflict.
