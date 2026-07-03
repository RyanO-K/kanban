# Agent Chat (stdin injection) — Design

**Date:** 2026-07-03
**Status:** Approved design, pre-implementation
**Companion spec:** `discord-kanban-bot/docs/superpowers/specs/2026-07-03-agent-live-channels-design.md`
(the Discord side that consumes this feature)

## Goal

Let a human send messages to a **running** orchestrator agent, delivered into the
agent's context mid-run within seconds. The Discord bot (or any HTTP client) POSTs a
message to the kanban server; the orchestrator relays it to the agent process's stdin.

## Background (current behavior)

- `orchestrator.py` dispatches agents as
  `claude -p <prompt> --session-id <id> --output-format stream-json --verbose`
  with `stdout=log_f, stderr=STDOUT` and **stdin unattached** (`orchestrator.py:1047`).
  There is no channel to a running agent today.
- Run logs are stream-json JSONL at `_orchestrator/runs/<ticket>-<timestamp>.log`;
  the path is stored on the ticket's `orchestrator.logFile` marker.
- `GET /api/board/<slug>/task/<id>/log?n=` already serves parsed turns
  (`task_log` → `orchestrator_core.parse_log_turns`); no changes needed for reading.
- Docker mode (`useDocker` boards) wraps the same claude invocation in
  `docker run` via `_docker_dispatch`.

## Architecture

```
Discord bot ──POST /api/orchestrator/chat/<board>/<id>──▶ kanban_server
                                                              │ append line
                                                              ▼
                                              _orchestrator/chat/<board>__<id>.jsonl
                                                              │ tail (~1s)
                                              orchestrator pump thread (per run)
                                                              │ write user turn
                                                              ▼
                                              agent process stdin (stream-json input)
```

Server and orchestrator communicate through an **inbox file per run**, the same
shared-directory pattern as `_orchestrator/activity.json` — no new IPC mechanism.

## Component 1: chat inbox files

- Path: `_orchestrator/chat/<board>__<ticket-id>.jsonl` (double underscore separates
  board dir name from id; both are filesystem-safe already).
- One JSON object per line: `{"message": str, "writer": str, "ts": <ISO-8601 UTC>}`.
- Writer: kanban server appends (open in `"a"`, single `write()` of one line + flush).
- Reader: the orchestrator pump thread tails by remembered byte offset.
- Lifecycle: **truncated/deleted at dispatch** of a new run for that ticket (stale
  messages from a previous run must never leak into a new run) and **deleted at reap**.

## Component 2: server endpoint

`POST /api/orchestrator/chat/<board>/<id>` — body `{"message": str, "writer": str}`.

- Gated by `_authorized()` like every other state-changing route.
- Validation: ticket must exist (else `404 {"error": "not found"}`); ticket's
  `orchestrator.state` must be `"dispatched"` and `status` must be `"in_progress"`
  (else `409 {"error": "not running"}`); `message` must be a non-empty string
  (else `400`).
- On success: append the line to the inbox file (creating `_orchestrator/chat/` on
  demand) and return `200 {"ok": true}`.
- The endpoint does NOT check the PID is alive — that race belongs to the pump/reap
  side. A message posted just as the run dies is silently dropped with the inbox
  file; the 409 covers the common case.

## Component 3: streaming-input dispatch

When chat is enabled, host dispatch changes to:

```
claude -p --input-format stream-json --output-format stream-json --verbose \
       --session-id <id> [--model <m>] [--allowedTools <list>]
```

- The prompt is **no longer passed as argv**. `Popen(..., stdin=PIPE)`; immediately
  after spawn, the initial prompt is written to stdin as the first user message:
  `{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": <prompt>}]}}\n`
  then flushed. Subsequent chat messages use the identical encoding.
- Docker mode: `_docker_dispatch` adds `-i` to `docker run` (keeps stdin attached
  through the container) and the inner claude command gets the same
  `--input-format stream-json` change. Same stdin-PIPE + first-message write.
- Feature flag: `CHAT_ENABLED = True` module constant in `orchestrator_core.py`
  (following the existing constant-config convention). When `False`, dispatch is
  byte-for-byte the legacy argv-prompt form and no pump thread is created —
  the one-line escape hatch if streaming input misbehaves.

### Consequence: process lifetime changes

With streaming input the CLI does **not** exit after its final result — it waits for
more stdin. Run completion is therefore driven by the pump thread **closing stdin**
(see Component 4), after which the CLI exits normally and the existing reap path
(`_exit_code`, marker cleanup, auto-commit, `completedLog`) proceeds unchanged.
`kill_pid` (taskkill `/T` on Windows, killpg on POSIX) is unchanged and still works —
a killed process moots the pump, which notices the child died and exits.

## Component 4: pump thread (one per run, daemon)

Created at dispatch alongside the Popen. Inputs: the `Popen`, the inbox path, the
run-log path. Loop (~1s interval):

1. **Child died?** → clean up (delete inbox file) and exit the thread.
2. **New inbox lines?** → for each parsed line, write it to child stdin as a user
   message (encoding above) and flush. A message wrapped as
   `[Message from <writer> via Discord]\n<message>` so the agent knows the source.
   Record that a user message has been sent since the last result.
3. **Close decision:** tail the run log for a top-level `{"type": "result"}` line.
   When (a) a result has been seen, (b) it is *newer* than any user message we've
   injected, and (c) the inbox is drained — close child stdin. The CLI exits; reap
   proceeds. (If a chat message arrives before close, it is sent instead and the
   agent runs another turn; the next result re-arms the close decision.)
4. Broken pipe / OSError writing stdin → treat as child death (step 1).

The close decision logic is a **pure function** in `orchestrator_core.py` so it is
unit-testable without processes:
`chat_should_close(result_seen_after_last_send, inbox_empty) -> bool`
(true only when both are true)
plus a pure `chat_encode_user_message(text) -> str` (the JSONL line) and pure
inbox-line parsing `chat_parse_inbox_line(line) -> dict | None`.

Pump threads are registered next to `_PROCS` so tests and shutdown can join them;
they are daemons, so a crashed loop never blocks orchestrator exit.

## Error handling

- Malformed inbox line → skipped (parse returns `None`), logged at debug.
- Inbox file unreadable → retry next loop iteration; never crash the thread.
- Server-side write failure → `500` to the caller; nothing partial (single-line append).
- `CHAT_ENABLED=False` + POST chat → the ticket is running but no pump exists;
  messages would rot in the inbox. The endpoint returns
  `409 {"error": "chat disabled"}` when `CHAT_ENABLED` is false (server imports the
  constant from `orchestrator_core`, which it already imports).

## Testing

- Pure: `chat_should_close`, `chat_encode_user_message`, `chat_parse_inbox_line`,
  inbox truncate-at-dispatch decision — plain unit tests in `test_orchestrator_core.py` style.
- Endpoint: existing kanban-server test harness — 200 append, 404, 409 not-running,
  409 chat-disabled, 400 empty message, auth gate.
- Pump integration: spawn a stand-in child (a tiny `python -c` script that echoes
  stdin lines to stdout and exits on EOF) to prove: message delivery order, close on
  result+drained, cleanup on child death, stale-inbox truncation at dispatch.
- One live smoke path is acceptable to defer to manual verification: a real
  `claude -p --input-format stream-json` round-trip (documented as a manual step in
  the plan, since it costs tokens).

## Out of scope

- Reading logs (already served by `GET .../log`).
- Discord rendering, channel lifecycle, watermarks — see the companion bot spec.
- Preserving chat history beyond the run log (injected user turns do appear in the
  stream-json log, but `parse_log_turns` only surfaces assistant turns; that is fine
  because the sender's own message is already visible wherever they typed it).
- Interrupting/steering a turn in progress — messages are queued by the CLI and
  processed as the next user turn; no mid-turn interruption.

## Contract summary for consumers

- `POST /api/orchestrator/chat/<board>/<id>` `{"message", "writer"}` →
  `200 {"ok": true}` | `400` | `404` | `409 {"error": "not running" | "chat disabled"}`.
- `GET /api/board/<slug>/task/<id>/log?n=0` → `{"turns": [...], "running": bool,
  "hasLog": bool, "status": str}`; turn = `{"seq", "role": "assistant", "text",
  "tools": [{"name", "summary", "result"}], "timestamp"?}`. `seq` is renumbered per
  response and the log is tailed to its last 256 KB — consumers must not treat `seq`
  as a stable global index. `text` includes thinking blocks (merged by
  `parse_log_turns`); `result` is pre-truncated to 6000 chars.
