# STATUS — "message the running bot" feature

Working file for the bot-messaging task (Ryan's request, 2026-08-03). Delete when done/merged.

## Safety check (2026-08-03)

- No `kanban_server.py` / `orchestrator.py` python processes running; port 8745 free.
- Repo on branch `release`, clean tree at start. Ticket `*.json` files are gitignored
  live board state — never committed.

## Found on arrival

Most of the backend already existed on `release` (commits e6727bc..9972160, spec
`docs/specs/2026-07-03-agent-chat-design.md`, plan `docs/plans/2026-07-03-agent-chat.md`):
`POST /api/orchestrator/chat/<board>/<id>` inbox endpoint, per-run stdin pump thread,
streaming-input (`--input-format stream-json`) host + docker dispatch. Messages already
queued in `_orchestrator/chat/<board>__<id>.jsonl` and were delivered at the CLI's next
turn boundary (no mid-turn interruption).

Gaps vs Ryan's request, now closed:

1. No GET endpoint / no delivered-vs-queued visibility (pump offset was in-memory only).
2. No UI — kanban.js had no chat panel at all.
3. Undelivered messages were LOST when a run ended (pump/`_release_proc` deleted the
   inbox unconditionally; nothing fed them into a follow-up run).

## Done (all committed on `release`)

- [x] Baseline test run: **457 passed** (`py -m pytest tests -q`, 126s).
- [x] ad3a0f0 core: delivered-offset sidecar (`<inbox>.pos`) + pending/delivered split +
      release-keeps-pending + comment/prompt formatting helpers (11 new tests).
- [x] db236e9 orchestrator: pump persists delivered offset; pending-bearing inboxes
      survive child death/broken pipe/release; reap surfaces undelivered messages
      (comment + `pendingChat`) on every terminal outcome incl. stop-all and the
      self-completed #48 guard; completed-with-pending re-queues to `ready`
      (`chat_requeue` activity); prompt builders inject `pendingChat`; `_dispatch_one`
      consumes it (9 new tests in `tests/test_chat_requeue.py`, pump tests updated).
- [x] cc008de server: `GET /api/orchestrator/chat/<board>/<id>` queue-status endpoint
      (`messages` with `delivered` flags, `nextRun` = pendingChat, `running`,
      `enabled`) (8 new tests).
- [x] a6fce5e UI: **Message agent** side-panel section — send form (Ctrl/Cmd+Enter) on
      live tickets, 2s-polled queued/delivered list, "queued for next run" badges when
      idle with pendingChat; drafts survive re-renders. `node --check` clean.
- [x] CLAUDE.md: API table rows + "Agent chat — message a running bot" section.
- [x] Full suite after all changes: **489 passed** (`py -m pytest tests -q`, 144s).
- [x] Live smoke on scratch port 8799: `/api/files`, board load, chat GET (200 shape
      `{"enabled":true,"running":false,"messages":[],"nextRun":[]}`), 404 route,
      `/kanban.js` 200. Server stopped after.

## Known issues / notes

- Delivered state is batch-granular: the pump writes the offset sidecar after each
  delivered batch, so a crash mid-batch can re-surface an already-delivered message as
  pending (deliberate: err on the side of not losing messages).
- A `completed` reap with pending guidance re-queues to `ready` and skips publish for
  that round; publish/auto-commit happens when the follow-up run completes.
- The real-CLI round-trip (costs tokens) remains a manual step — see
  `docs/plans/2026-07-03-agent-chat.md` "Manual verification"; the new UI path adds:
  open a running ticket, send a message, watch it flip queued → delivered, and check a
  killed run's messages appear as a comment + "queued for next run".
