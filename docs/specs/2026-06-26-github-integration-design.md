# GitHub Integration — design

**Ticket:** `.kanban/kanban-dev/5.json`
**Date:** 2026-06-26
**Status:** Draft (spec only — implementation explicitly deferred per ticket comment)

## Goal

Let the kanban board exchange tickets with GitHub Issues, both directions, using
the `gh` CLI (already installed: `gh 2.74.1`):

1. **Export** — push a kanban ticket to GitHub as an Issue (`gh issue create`).
2. **Import** — pull a GitHub Issue by number into the board as a ticket
   (`gh issue view <n> --json ...`).
3. **Correlation field** — add a field on tickets that links a ticket to its
   GitHub Issue (and, per the ticket text, the GitHub *project* it belongs to),
   so a ticket is never double-exported and the two stay associated.

This is a thin, stdlib-only layer over `gh`. We do **not** add a GitHub API
client, OAuth, or `requests`/`PyGithub` dependencies — `gh` owns auth.

## Scope decisions & open constraints

These shape the design and are the most likely places to need human confirmation
before implementation begins.

- **Auth is delegated to `gh`.** The server shells out to `gh`; whatever account
  `gh auth status` reports is what's used. We never store a token. If `gh` is not
  installed or not authenticated, the feature degrades gracefully (see Errors).
- **Target repo.** `gh` needs to know which repo to act on. There is a real
  obstacle here: the workspace root (`C:/Users/you/Documents/GitHub`) is
  **not currently a git repository** and has no remote, so `gh` cannot infer a
  repo from the working directory. The design therefore makes the repo an
  explicit, configured value rather than relying on `gh`'s cwd inference:
  - Board-level: a `github` block in the board's `_meta.json`
    (`{"github": {"repo": "owner/name", "projectNumber": 7}}`).
  - All `gh` calls pass `--repo <owner/name>` explicitly when configured.
  - If no repo is configured for a board, export/import return a clear 400 with
    remediation text rather than guessing.
- **GitHub "project" semantics.** The ticket says "correlate the issues to a
  project." GitHub has *classic* projects and *Projects v2*; `gh project` targets
  v2. This spec treats the project link as **optional metadata stored on the
  ticket** (`github.projectNumber` + `github.projectItemId`) and, on export,
  best-effort adds the new issue to the configured project via
  `gh project item-add`. If project linking fails (no project configured, or
  permissions), export still succeeds and records a warning — the issue link is
  the hard requirement, the project link is best-effort.

> **NEEDS HUMAN CONFIRMATION before the plan is implemented** (captured as plan
> task 0): (a) the default target repo `owner/name` for the `kanban-dev` board,
> and (b) whether project correlation must use Projects v2 (`gh project`) or is
> satisfied by storing the project number as metadata only.

## The correlation field

Add an optional `github` object to the ticket JSON shape:

```jsonc
{
  "github": {
    "repo": "owner/name",        // repo the issue lives in
    "issueNumber": 42,            // GitHub issue number
    "issueUrl": "https://github.com/owner/name/issues/42",
    "state": "open",             // mirror of issue state at last sync
    "projectNumber": 7,           // optional: GitHub Project (v2) number
    "projectItemId": "PVTI_...",  // optional: project item id, set on add
    "lastSyncedAt": "2026-06-26T18:45:00+00:00"
  }
}
```

- A ticket with `github.issueNumber` set is "linked" — re-export is a no-op
  (returns the existing link) unless `force` is passed.
- The field is purely additive; existing tickets and the front-end are
  unaffected when it is absent. `load_board` passes it through unchanged.

## API surface (matches existing `kanban_server.py` patterns)

All handlers return the project's `(data_dict, status_int)` tuple convention and
are dispatched from `do_POST` by URL-part length/shape, exactly like
`/comment`, `/kill`, `/answer`.

| Action | Request | Behavior |
|---|---|---|
| Export ticket → issue | `POST /api/board/<slug>/task/<id>/github/export` body `{"force": false}` | Runs `gh issue create`, stores `github` block, appends a `comment` + `history`-free note, returns `{issueNumber, issueUrl}`. |
| Import issue → ticket | `POST /api/board/<slug>/github/import` body `{"number": 42}` | Runs `gh issue view 42 --json ...`, creates a new ticket (via existing `create_task` path) with `github` block populated; if an existing ticket already links that issue, updates it instead of duplicating. |
| (optional) Sync status | `POST /api/board/<slug>/task/<id>/github/sync` | Re-reads the linked issue and refreshes `github.state`/`lastSyncedAt`. Nice-to-have; can ship in a follow-up. |

Pure logic (building `gh` argv, parsing `gh` JSON output, mapping issue⇄ticket,
deciding export/no-op) lives in a new **`github_integration.py`** module with no
network/subprocess calls in the mapping functions, so it is unit-testable the
same way `orchestrator_core.py` is. `kanban_server.py` holds the thin handlers
that actually invoke `subprocess.run(["gh", ...])` and call into the module.

### Field mapping

Export (ticket → `gh issue create`):
- `--title` ← ticket `title`
- `--body` ← ticket `detail` + a footer line `Kanban: <slug>/<id>` so an
  imported-back issue can be traced to its origin board.
- `--repo` ← board `_meta.github.repo`
- `--label` ← optional, from a configurable map (out of scope for v1; default none)

Import (`gh issue view --json number,title,body,state,url,labels` → ticket):
- ticket `title` ← issue `title`
- ticket `detail` ← issue `body`
- ticket `status` ← `todo` (imported issues land in the first column; we do
  **not** try to map GitHub state→kanban column beyond open=todo/closed=completed)
- `github` block ← number, url, state, repo

## Error handling & graceful degradation

- `gh` missing (`FileNotFoundError` from `subprocess`) → `502`/`500` with
  `{"error": "GitHub CLI (gh) is not installed"}`.
- `gh` present but not authed (non-zero exit, stderr mentions auth) → `502` with
  the stderr surfaced and a hint to run `gh auth login`.
- No `github.repo` configured for the board → `400` with remediation text.
- `gh` non-zero for other reasons → `502` carrying `gh`'s stderr (trimmed).
- Subprocess calls use an explicit `timeout=` and never `shell=True`; argv is a
  list so ticket text can't inject shell.

## Out of scope (v1)

- Webhooks / push-from-GitHub, comment sync, label/milestone sync, PR linking.
- Bulk export of a whole board (v1 is per-ticket; bulk can wrap the same handler).
- Mapping the full kanban column set onto GitHub state (only open/closed).
- Front-end UI buttons. v1 ships the API + module + tests; a follow-up ticket can
  add "Export to GitHub" / "Import #" controls to `kanban.html`.

## Testing strategy

- `github_integration.py` is pure: unit-test argv construction, JSON-output
  parsing, and the export/no-op/force decision with table-driven cases. No `gh`
  invoked.
- Handler-level tests stub the subprocess boundary (a `_run_gh` seam) to feed
  canned stdout/stderr/returncode, asserting status codes, the stored `github`
  block, and that ticket text is passed as argv (never interpolated into a
  shell string).
- A `gh`-missing test patches the seam to raise `FileNotFoundError` and asserts
  graceful degradation.
- **Write the failing test first** for each unit of logic (per ticket profile
  rules), then implement.
