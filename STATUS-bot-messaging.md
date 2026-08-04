# STATUS — "message the running bot" feature

Working file for the bot-messaging task (Ryan's request, 2026-08-03). Delete when done/merged.

## Safety check (2026-08-03)

- No `kanban_server.py` / `orchestrator.py` python processes running; port 8745 free.
- Repo on branch `release`, clean tree at start. Ticket `*.json` files are gitignored
  live board state — never committed.

## Found on arrival

Most of the backend already exists on `release` (commits e6727bc..9972160, spec
`docs/specs/2026-07-03-agent-chat-design.md`, plan `docs/plans/2026-07-03-agent-chat.md`):
`POST /api/orchestrator/chat/<board>/<id>` inbox endpoint, per-run stdin pump thread,
streaming-input (`--input-format stream-json`) host + docker dispatch. Messages already
queue in `_orchestrator/chat/<board>__<id>.jsonl` and are delivered at the CLI's next
turn boundary (no mid-turn interruption).

Gaps vs Ryan's request:

1. No GET endpoint / no delivered-vs-queued visibility (pump offset is in-memory only).
2. No UI — kanban.js has no chat panel at all.
3. Undelivered messages are LOST when a run ends (pump/`_release_proc` delete the inbox
   unconditionally; nothing feeds them into a follow-up run).

## Plan / progress

- [x] Baseline test run: **457 passed** (`py -m pytest tests -q`, 126s).
- [ ] Core: delivered-offset sidecar helpers + pending/delivered split + comment/prompt
      formatting (pure, in `orchestrator_core.py`) + tests.
- [ ] Orchestrator: pump persists delivered offset; pending-bearing inboxes survive child
      death/release; reap surfaces undelivered messages (comment + `pendingChat` field);
      completed-with-pending re-queues to `ready`; prompt builders inject `pendingChat`;
      dispatch consumes it + tests.
- [ ] Server: `GET /api/orchestrator/chat/<board>/<id>` (messages + delivered flags +
      nextRun queue + running/enabled) + tests.
- [ ] UI: "Message agent" section on running tickets (input + queued/delivered list,
      queued-for-next-run when not running).
- [ ] Docs: CLAUDE.md API/feature section.
- [ ] Full suite green after changes.

## Known issues / notes

(to be filled in as work lands)
