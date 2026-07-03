# Live Logs — design (ticket #45)

## Goal

A button on the ticket detail side-panel that opens a live-updating render of the
dispatched agent's most recent **turns** — what it is thinking and what it is doing —
sourced from the sub-agent's `stream-json` run-log.

## Background

When the orchestrator dispatches a sub-agent for a ticket it writes an `orchestrator`
marker onto the ticket JSON with a `logFile` field, e.g.
`.kanban/_orchestrator/runs/45-20260630T141023+0000.log`. That log is the agent's
stdout in `--output-format stream-json --verbose` form: one JSON object per line.

Relevant line shapes:

- `{"type":"assistant","message":{"content":[ ...blocks... ]}}` — the agent's turn.
  Blocks: `{"type":"text","text":...}`, `{"type":"thinking","thinking":...}`,
  `{"type":"tool_use","name":...,"input":{...}}`.
- `{"type":"user","message":{"content":[ {"type":"tool_result","content":...} ]}}` —
  the tool results fed back to the agent.
- `{"type":"system",...}`, `{"type":"result",...}`, `{"type":"rate_limit_event"}` —
  housekeeping; not turns.

The log paths are relative to the workspace root (the parent of `.kanban`), matching
how `orchestrator.py` resolves them (`os.path.join(kanban_dir, "..", logFile)`).

## Backend

New endpoint: `GET /api/board/<slug>/task/<id>/log?n=<turns>`

- Resolve the ticket via the existing board/task lookup. Read its
  `orchestrator.logFile`. If the ticket has no marker / no log file, return
  `{"turns": [], "running": false, "hasLog": false}` (200) so the UI shows an
  informative empty state rather than erroring.
- Resolve the log path against the workspace root and **confine it to the
  `_orchestrator/runs/` tree** (reject traversal), mirroring `read_doc`'s safety
  pattern. A path outside runs/ → 403.
- Tail-read the file (bounded — only the last slice of the file is read so a 500 KB
  log doesn't get fully parsed each poll), parse stream-json lines into compact turn
  objects, keep the last `n` (default 20, clamped 1..100).
- A turn object: `{seq, role, text, tools}` where
  - `role` ∈ `"assistant" | "user"`,
  - `text` is the concatenated `text`/`thinking` for assistant turns, or a truncated
    `tool_result` preview for user turns,
  - `tools` is a list of `{name, summary}` derived from `tool_use` blocks
    (`Read foo.py`, `Bash <cmd>`, `Edit bar.js`, etc.).
- Empty turns (no text and no tools) are dropped so housekeeping/echo lines don't
  clutter the stream.
- Response: `{"turns":[...], "running": <bool>, "hasLog": true, "status": <ticket status>}`.
  `running` is true when the ticket is currently in-flight (`orchestrator.state ==
  "dispatched"` and status `in_progress`); the UI uses it to stop polling once the
  agent finishes.

The stream-json → turns parsing is a **pure module-level function**
(`parse_log_turns(text, n)`) with no I/O, so it is directly unit-testable.

## Frontend (vanilla JS, matching `renderPanel`)

- In `renderPanel`, when `task.orchestrator && task.orchestrator.logFile`, render a
  **Live logs** section with a toggle button (`📡 Live logs`).
- Clicking the button expands an inline `.sp-log` container and starts a ~2 s poll of
  the log endpoint; clicking again (or closing/switching the panel) stops the poll.
- Each turn is a row: a role badge (🤖 agent / ⬅ result), the condensed reasoning
  text, and tool chips showing what the agent is doing. Newest at the bottom;
  auto-scroll to the latest turn when the view is already near the bottom (so a user
  scrolled up to read history isn't yanked down).
- A live indicator: a pulsing dot + "live" while `running`, switching to "stream
  ended" when the agent finishes (poll stops automatically).
- Polling is owned by a single module-level handle that is always cleared on
  panel close / re-render / toggle-off, so only one log poll is ever active.

## Testing

`tests/test_log_endpoint.py`:

- `parse_log_turns`: assistant text turn; thinking block; `tool_use` → tool chip with
  a sensible summary; `user`/`tool_result` → truncated preview; malformed / non-JSON
  lines skipped; system/result lines ignored; empty turns dropped; `n` tail limit
  honored.
- Path confinement: a `logFile` escaping `_orchestrator/runs/` is rejected.

The board's `commitRequirements` require `python -m pytest tests` to pass; the new
tests plus the existing suite must be green before commit.

## Out of scope

- No websockets / SSE — simple poll-while-open is sufficient and matches the existing
  `poll()`-based UI.
- No historical run picker (only the current/last `logFile` on the ticket). Earlier
  runs remain on disk but are not surfaced here.
