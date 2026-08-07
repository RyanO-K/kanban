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

## Verification pass (2026-08-07)

Independent re-verification of the 5 feature commits (db07f7e..f30b173), resumed after
the previous verifier was killed mid-review.

- Safety: port 8745 free, no `kanban_server.py`/`orchestrator.py` running; repo on
  `release`, clean tree at start. No ticket `*.json` touched or committed.
- Baseline re-run: **489 passed in 145.93s** (`py -m pytest tests -q`) — matches the
  claimed baseline exactly.
- Code review findings (checked: `.pos` sidecar crash semantics, requeue loop safety,
  stop-all cleanup, GET on bad board/ticket, UI poll lifecycle, pump-vs-reap races):
  - Confirmed sound: batch-granular `.pos` (crash mid-batch re-surfaces at-most-once →
    duplicate-not-lost, as documented); requeue cannot loop on the same guidance
    (`_dispatch_one` pops `pendingChat` after building the prompt and persists the pop;
    re-queue only fires when a fresh inbox holds undelivered bytes); stop-all order is
    kill → release (keeps pending inbox) → surface (consumes it); CRLF/byte-offset
    accounting agrees between `_tail_new_lines` and `chat_split_messages`; GET/POST 404
    on unknown board/ticket via `safe_name` + isfile; UI timers are stopped on both
    `closePanel` and every `renderPanel`.
  - **Bug fixed (03ad4c2):** the #48 stale-marker guard cleared the marker even while
    the self-completed child was still ALIVE with an undelivered inbox. If the child
    then died before the pump delivered, the inbox was orphaned (marker-less tickets
    are never re-reaped) and silently wiped by the next dispatch — the exact loss this
    feature exists to prevent. The guard now defers the marker clear until the inbox
    drains or the process dies (bounded: POST 409s on a done ticket). +1 regression
    test in `tests/test_chat_requeue.py`.
  - **Bug fixed (95261a6):** `pollChat`'s post-await guard only checked that
    `#spChatList` existed, so switching the panel to another ticket mid-flight let the
    OLD ticket's queue render into the NEW ticket's chat list (persistent on the
    no-timer snapshot path). Polls are now keyed by `board|id` and stale responses
    dropped. `node --check` clean.
- Live smoke (scratch-port handler harness on 127.0.0.1:8807 against a temp board tree,
  real `KanbanHandler`, never the real server): **14/14 checks passed** — GET 404s
  (bad board / bad ticket / malformed path), GET running-empty shape
  `{"enabled":true,"running":true,"messages":[],"nextRun":[]}`, POST 200 + inbox file,
  queued→delivered flip when `.pos` = inbox size, delivered/queued split across the
  offset, POST 400 blank / 409 not-running / 404 bad board, and `nextRun` from
  `pendingChat` on an idle ticket.
- Full suite after fixes: **490 passed in 147.68s** (489 + 1 new regression test).
