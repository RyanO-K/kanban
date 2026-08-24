# Layrr live edit — fast in-page patching with session-end ticket chunking

**Date:** 2026-08-24
**Status:** Approved design (option A: server-owned persistent agent, sonnet default,
widget-only finalize, conditional reload suppression)

## Problem

Today every layrr overlay edit becomes a kanban ticket worked by a full orchestrator
agent in a worktree: minutes to tens of minutes per tweak. For the dominant use case —
small HTML/CSS adjustments made while looking at the running app — that loop is far too
slow. The user wants: describe a tweak, see it on the page in seconds, iterate, and only
when satisfied convert the accumulated session of edits into tickets that make the real
source changes.

## Goals

- A live edit (click element → type instruction → visible change) lands in **~3–6
  seconds**, applied to the already-served page without touching source.
- Edits survive page reloads and SPA navigation for the life of the layrr instance.
- Per-edit revert/reactivate from the in-page widget.
- A **finalize** action (in-page widget only) chunks the session's active edits into
  coherent kanban tickets. Each ticket embeds the approved patch itself, so the worker
  reproduces an already-signed-off visual state instead of interpreting prose.
- One persistent model conversation per live session: the agent that made the edits is
  the agent that chunks them.

## Non-goals (v1)

- Warm-worktree claiming for finalize-filed tickets (pool logic lives in JS; v1 tickets
  take the existing "no prepared workspace" fallback; porting the claim to Python is a
  follow-up ticket).
- Enriching the agent's context with live `outerHTML`/computed styles from the browser.
  Layrr's selector + class list + source-context snippet is the v1 input; revisit if
  patch quality disappoints.
- Any change to ticket mode. `liveEdit` is opt-in per board; boards without it behave
  exactly as today.
- A JS test rig for the widget (manual verification, consistent with the repo).

## Architecture

```
overlay (browser) ──WS──▶ layrr proxy ──editQueue──▶ kanban-agent.mjs (sink)
                                                          │ liveEdit? ──no──▶ POST create-task (unchanged)
                                                          │ yes
                                                          ▼
                                    POST /api/layrr/live/<instance>/edit
                                                          │ enqueue, return 202
                                                          ▼
                     app/layrr_live.py ── stdin ──▶ persistent `claude -p` (sonnet,
                       ledger file                  stream-json in/out, tool-less)
                          ▲   │ reader thread parses patch JSON, appends to ledger
                          │   ▼
      widget polls GET /api/layrr/live/<instance>?since=… ──▶ applies/reverts ops in DOM
```

The kanban server stays single-threaded `HTTPServer`; every live handler enqueues and
returns immediately. All waiting happens on a per-agent **reader thread** (the same
pattern the orchestrator and perf sampler already use inside this process). The
persistent-agent mechanics (`claude -p --input-format stream-json --output-format
stream-json`, follow-up messages written to stdin) reuse the orchestrator's proven
child-process pattern (`orchestrator.py` dispatch + inbox).

Why persistent rather than per-edit spawns: claude.exe cold start under CrowdStrike on
this machine costs 10–30s; one spawn per session amortizes it, and the conversation
itself is the session memory the finalize step needs.

## Components

### 1. `app/layrr_live.py` — live-edit engine

Owns, per layrr instance:

- **Agent lifecycle.** Lazily spawned on the first live edit:
  `claude -p --input-format stream-json --output-format stream-json --verbose
  --model <liveModel>`. Spawned with the launcher's `_spawn` conventions (own process
  group, no console window, breaks away from the server's CPU-cap Job Object). The
  stream-json `system:init` event's `session_id` is stored in the ledger
  (`agent.claudeSessionId`) for `claude --resume` debugging, per kanban convention.
  The first stdin message is a **preamble**: role, the patch JSON contract, rules
  (CSS-first, high specificity + `!important` because the page uses Tailwind, minimal
  ops, echo the `editId`, respond with JSON only — no tools, no file access).
- **Ledger** at `_orchestrator/layrr-live/<instance-id>.json`, written atomically
  (reuse `_atomic_write_json`). Durable session record; survives agent and server
  restarts. The agent process is disposable — if it is dead at enqueue time, respawn
  and prime with preamble + a summary of the ledger's active edits before sending the
  new one.
- **Reader thread.** Collects each assistant turn's text, extracts the first JSON
  object, validates it, appends the patch to the matching pending edit (matched by
  echoed `editId`, FIFO fallback). Malformed/unparseable JSON → one retry message
  carrying the parse error; a second failure marks the edit `failed` with the raw text
  as `error`.

**Ledger shape:**

```json
{
  "instance": "b2react-local-dev-4567",
  "board": "b2react-local-dev",
  "projectRoot": "…",
  "agent": { "pid": 0, "model": "claude-sonnet-4-6",
             "claudeSessionId": "…", "startedAt": "…" },
  "edits": [
    {
      "editId": 1,
      "instruction": "make the CTA red",
      "element": { "tagName": "button", "className": "…",
                   "textContent": "…", "selector": "…" },
      "elements": null,
      "sources": ["src/components/Hero.tsx:42"],
      "state": "pending",
      "patch": null,
      "error": null,
      "ticketId": null,
      "requestedAt": "…",
      "resolvedAt": null
    }
  ],
  "finalizing": false,
  "updated": "…"
}
```

`element` is the primary selection; for layrr multi-select requests `elements` carries
the per-element info array (same fields each) and the agent's prompt lists all of them,
mirroring layrr's own `buildPrompt` branching.

**Edit states:** `pending → applied | failed`; `applied ⇄ reverted` (widget toggle);
`applied → filed` (finalize, `ticketId` stamped); `filed → done` (its ticket reached
`done` — joined lazily server-side during the live GET, so the widget stays dumb and
the ledger is the single source of truth). The widget applies ops for `applied` and
`filed` edits only. `done` deactivation is what prevents a live patch and its landed
source change from double-applying once HMR delivers the real edit.

**Endpoints** (all on the existing server; each returns `(payload, status)` like
`layrr_launcher`):

| Endpoint | Behaviour |
|---|---|
| `POST /api/layrr/live/<instance>/edit` | Body: instruction + element info + bundle-relative sources (from the sink). Appends a `pending` edit, writes the stdin message, returns `202 {editId}`. Never waits for the model. |
| `GET /api/layrr/live/<instance>?since=<mtime>` | Ledger for the widget; `{"unchanged": true}` short-circuit on matching mtime (same contract as the board API). Lazily joins ticket status for `filed` edits. |
| `POST /api/layrr/live/<instance>/revert/<editId>` | Body `{"active": false}` → `reverted`; `{"active": true}` → back to `applied`. Only `applied`/`reverted` are togglable. |
| `POST /api/layrr/live/<instance>/finalize` | Optional body `{"editIds": […]}` (default: all `applied`). Sets `finalizing: true`, writes the chunk message to stdin, returns `202`. The reader thread receives the chunk JSON and files tickets (below). |

`layrr_launcher.stop()` and registry eviction also kill the instance's live agent;
the ledger file is kept as the session record.

### 2. Patch contract — structured ops, not raw JS

Tool-less agent, pure text-in → JSON-out:

```json
{
  "editId": 3,
  "summary": "Larger red CTA",
  "ops": [
    { "type": "css", "css": ".hero .cta{background:#dc2626 !important;padding:12px 24px !important}" },
    { "type": "setText",   "selector": "…", "value": "…" },
    { "type": "setAttr",   "selector": "…", "name": "…", "value": "…" },
    { "type": "insertHTML","selector": "…", "position": "beforeend", "html": "…" }
  ]
}
```

Four op types only. `css` covers the dominant case; the three DOM ops cover
text/attribute/structure tweaks and are each mechanically undoable: the widget records
prior text/attribute values and tags inserted nodes (`data-layrr-live-edit="<id>"`), so
revert restores exactly.

### 3. `app/layrr/kanban-agent.mjs` — live branch in the sink

When `LAYRR_LIVE_EDIT=1`, `applyEdit` POSTs the request to
`${LAYRR_KANBAN_URL}/api/layrr/live/${LAYRR_INSTANCE_ID}/edit` — instruction, element
info (and per-element info for multi-select), and bundle-relative source strings built
with the existing `relToBundle` helper — and returns
`{success: true, message: "Live edit #N queued — applying…"}` (the overlay toasts this
immediately, before the patch lands; the message must not claim the edit is done).
On a non-2xx or unreachable server it returns `success: false` with the reason —
**no silent fallback to ticket mode** (a surprise ticket is worse than a visible error).
Ticket mode (`LAYRR_LIVE_EDIT` unset) is byte-for-byte today's behaviour. The
`[b2react] layrr → kanban sink` marker and the shared-install contract with b2-react's
`dev:layrr` are unchanged: each launcher still re-applies its own copy before spawning.

### 4. `static/layrr-widget.js` — apply, persist, revert, finalize

New "Live edits" section above the ticket list (only rendered when the script tag
carries `data-instance` and `data-live="1"`):

- **Polling:** the live GET at ~1s while any edit is `pending` or `finalizing`, backing
  off to the existing 4s cadence when idle. Ticket polling is unchanged.
- **Application:** on every poll and on every page load, reconcile the DOM to the
  ledger — apply ops for `applied`/`filed` edits not yet applied in this document,
  undo ops for edits that left that set. Page-load reconciliation is what makes
  patches survive the overlay's forced reload and SPA navigation. CSS ops live in one
  `<style id="kanban-layrr-live">` element rebuilt from active edits.
- **Rows:** one per edit — summary, state dot, on/off toggle (`applied ⇄ reverted`),
  spinner row for `pending`, error text for `failed`, ticket link for `filed`/`done`.
- **Finalize:** a "File tickets from N edits" button → `POST …/finalize`. While
  `finalizing`, the button shows progress; when edits flip to `filed`, rows link to
  their tickets.
- **Busy signal for the reload guard:**
  `window.__kanbanLayrrLive = { busy: () => /* any edit pending, or finalizing */ }`.

### 5. Launcher changes — `app/layrr_launcher.py`

- `sanitize_cfg` accepts `liveEdit` (bool) and `liveModel` (string) on the board's
  `layrr` block.
- `start()` computes `instance_id` before building env and passes `LAYRR_LIVE_EDIT=1`
  (when enabled) and `LAYRR_INSTANCE_ID`. The widget snippet gains
  `data-instance="${process.env.LAYRR_INSTANCE_ID || ''}"` and
  `data-live="${process.env.LAYRR_LIVE_EDIT || ''}"` (same per-request template-literal
  mechanism, still env-guarded for the b2-react flow).
- **Patch 4 — conditional reload suppression** in `dist/overlay.js`. The
  edit-success `setTimeout(() => location.reload(), 2500)` becomes marker-bounded:

  ```js
  setTimeout(() => { /*KANBAN_NORELOAD*/
    try { if (window.__kanbanLayrrLive && window.__kanbanLayrrLive.busy()) return; } catch (e) {}
    location.reload();
  }, 2500);
  ```

  Approved behaviour: **suppress while we're working with the agent; when there are no
  outstanding tweaks, the flash is fine.** The widget's `busy()` encodes exactly that.
  In pages without the live widget (ticket mode, b2-react's own `dev:layrr`) the
  function is undefined and the reload proceeds as stock — the widget's presence is the
  guard, since overlay.js cannot read process env. Only the edit-success reload is
  patched; the version-preview/restore/revert reloads keep stock behaviour. Applied
  idempotently with the same re-patch/marker discipline as the other three patches, and
  `apply_patches()` grows the call.

### 6. Finalize — ticket chunking

The chunk message asks the same conversation: "group the listed active edits into
coherent tickets; return JSON `[{"title": …, "editIds": […], "rationale": …}]`" —
grouping by component/area so one ticket carries all related tweaks. The reader thread
receives it and files tickets by **direct function call** into the server's task-create
path (never HTTP back into our own single-threaded port — that deadlocks; same
concurrency conventions as the orchestrator thread, which already writes tickets
alongside the server). Each ticket detail contains:

- the `Raised from **layrr**` marker (widget filtering relies on it),
- the original instruction(s) verbatim,
- bundle-relative source locations with the existing verify-before-editing caveat,
- **the approved patch** (CSS/ops JSON) as the acceptance spec: "make the source
  produce this end state, then the live patch is retired",
- the board's layrr `column` and `model` stamps, exactly like ticket mode.

Filed edits get `state: "filed"` + `ticketId`; their patches stay applied until the
ticket reaches `done`.

## Config

Board `_meta.json` `layrr` block (Project Settings form):

```json
{ "targetPort": 5173, "projectRoot": "…", "baseBranch": "working",
  "model": "claude-opus-4-8",
  "liveEdit": true,
  "liveModel": "claude-sonnet-4-6" }
```

- `liveEdit` — opt-in, default off.
- `liveModel` — default **`claude-sonnet-4-6`** (approved: sonnet over haiku; the
  field allows haiku for speed experiments).
- `model` keeps its existing meaning: the model stamped on filed tickets.

## Error handling

| Failure | Behaviour |
|---|---|
| Agent returns malformed JSON | One retry with the parse error fed back; then edit `failed`, raw text surfaced in the widget row. |
| Agent process dies | Ledger persists. Next enqueue respawns and primes with preamble + active-edit summary. |
| Kanban server restarts | Same respawn-and-prime path; ledger is the durable record. |
| Live endpoint unreachable from sink | `applyEdit` returns failure with the reason; overlay shows it. No silent ticket fallback. |
| Finalize returns bad JSON | Retry once; then `finalizing: false` with an `error` the widget shows; edits stay `applied` — nothing lost, retry is a button press. |
| Ops fail to apply in DOM (stale selector) | Widget marks the row with a warning; other edits unaffected. |

## Testing

`tests/test_layrr_live.py`, in the `test_layrr_launcher.py` style (fake `Popen`,
scripted stream-json stdout, temp kanban dir):

- enqueue → `pending` ledger entry → reader parses scripted patch → `applied`.
- malformed JSON → retry message written to stdin → second failure → `failed`.
- dead agent at enqueue → respawn + priming message contains active-edit summary.
- revert/reactivate toggling and its state guards.
- finalize end-to-end: scripted chunk JSON → tickets created in a temp board with
  marker, patch payload, and stamps → edits `filed` with `ticketId`.
- live GET `since`/mtime contract and the lazy `filed → done` join.

Launcher tests extended: patch-4 idempotency and re-patch (marker-bounded), new env
vars, `sanitize_cfg` for `liveEdit`/`liveModel`.

Widget and overlay guard: manual verification against a live b2react-local-dev session.

## Follow-ups (explicitly out of v1)

1. Port warm-worktree claiming into the Python finalize path.
2. Optional browser-side context enrichment (element `outerHTML`, computed styles).
3. Widget-side mode toggle (per-edit choice between live and ticket) if living in one
   mode per board proves limiting.
