# .kanban board — agent guide

A lightweight file-based kanban. Boards live under a dedicated, gitignored
`boards/` folder inside `.kanban/`; each board is one subdirectory holding plain
JSON ticket files. There's a small Python server + HTML UI, but **agents normally
just read and edit the JSON files directly** — no server needed.

## Layout

```
.kanban/
  kanban_server.py        # optional read/write API + UI server (port 8745)
  kanban.html             # the board UI (served at /)
  _meta.template.json     # template for a new board's _meta.json
  migrate_boards_folder.py  # one-shot: relocate loose root boards into boards/ (ticket #94)
  boards/                 # dedicated, gitignored folder holding every board (ticket #94)
    <board-slug>/         # one directory per board (slug = its id)
      _meta.json          # board metadata: project, updated, context, openQuestions, outOfScope
      <id>.json           # one ticket per file (id is numeric: 1.json, 2.json, ...)
```

A directory under `boards/` is only a board if it contains `_meta.json` (so
`__pycache__` etc. are ignored). The `boards/` folder is gitignored — board data
is local per-machine state and is never tracked. Board discovery
(`kanban_server.scan_boards`, `orchestrator.load_all_tasks`) and per-board path
resolution route through a single `boards_root()` helper, so the location is
defined in one place.

## Ticket shape

Key fields on a `<id>.json` ticket:

- `id` (string), `title`, `status`, `detail`
- `status` is one of: `todo`, `ready`, `in_progress`, `blocked`, `pending`, `completed`
  (`ready` = all `dependsOn` met/none and queued to start, but not yet picked up)
- `dependsOn` / `blocks`: arrays of ticket ids
- `steps`, `files`, `outputs`: plan/checklist arrays
- `history`: append-only audit log of `status_change` entries (with timestamps)
- `comments`: `{writer, message, timestamp}` notes
- `claudeSessionId`: see session tracking below

## How agents interact

**Direct file editing (preferred):** read `_meta.json` for context, read the relevant
`<id>.json`, and edit JSON in place. When changing `status`, append a `status_change`
entry to `history` with a UTC timestamp. To add a new ticket, create `<next-id>.json`.

**Via the server (optional):** `python .kanban/kanban_server.py` then use the API:

| Action | Request |
|---|---|
| List boards | `GET /api/files` |
| Load a board (or `__all__`) | `GET /api/board/<slug>` |
| Move ticket | `PATCH /api/board/<slug>/task/<id>` body `{"column":"in_progress"}` |
| Create ticket | `POST /api/board/<slug>/task` body `{"title":...,"detail":...}` |
| Add comment | `POST /api/board/<slug>/task/<id>/comment` body `{"writer":...,"message":...}` |
| Delete ticket | `DELETE /api/board/<slug>/task/<id>` |

The server auto-appends `history` entries and bumps `_meta.json`'s `updated` date.

### Server bind config

The server's bind host and port are resolved in `main()` with safe defaults
(loopback `127.0.0.1`, port `8745`). To change them persistently, create
`.kanban/_orchestrator/server.json`:

```json
{ "host": "127.0.0.1", "port": 8745 }
```

Both keys are optional and fall back per-field; a missing or malformed file is
ignored. Precedence, most explicit first:

- **host:** `KANBAN_HOST` env var → `server.json` → loopback default
- **port:** `argv[1]` (`python kanban_server.py 9000`) → `KANBAN_PORT` env var →
  `server.json` → default

Bind to loopback unless you deliberately need LAN exposure — the API exposes
destructive endpoints (perf kill, server restart, DELETE task).

---

## Conventions

Each subdirectory is a board: `_meta.json` (project metadata) + numbered ticket files (`<id>.json`).

## Skills

Reusable, board-wide skills live under `.kanban/skills/<skill-name>/SKILL.md`. Because
kanban workers run with `cwd` at the workspace root, a skill placed here is versioned with
the board **and** discoverable by every worker (a skill buried in a sub-repo's
`.claude/skills/` would not be). Before doing a task a skill covers, read its `SKILL.md`.

Current skills:

- **`create-promotion-prs`** — raise `acme-sfdx` promotion PRs from the CLI by
  dispatching `manual-create-promotion-prs.yml` via `gh workflow run` (instead of opening a
  throwaway PR into `partial`). See `.kanban/skills/create-promotion-prs/SKILL.md`.

## Session tracking on tickets

When a Claude session starts actively working on a ticket (typically when moving it to
`in_progress`), record the session so it can be found later with `claude --resume <id>`:

1. Set a top-level `"claudeSessionId"` field on the ticket JSON to the value of the
   `CLAUDE_CODE_SESSION_ID` environment variable.
2. Also include `"sessionId": "<id>"` on the `status_change` history entry for that
   transition, so the audit trail shows which session made which change.

Example ticket fields:

```json
{
  "id": "3",
  "status": "in_progress",
  "claudeSessionId": "98a1faf5-71ca-43a6-b7df-6a03b0a4f471",
  "history": [
    {
      "action": "status_change",
      "from": "todo",
      "to": "in_progress",
      "timestamp": "2026-06-15T00:00:00+00:00",
      "sessionId": "98a1faf5-71ca-43a6-b7df-6a03b0a4f471"
    }
  ],
  "comments": [
    {
      "writer": "Claude",
      "message": "Implemented in kanban.html with three CSS changes: (1) set `html, body` to `height: 100%; overflow: hidden` to lock the page itself, (2) changed `.board` from `align-items: flex-start` to `align-items: stretch` so columns fill the full board height, (3) added `overflow-y: auto` to `.col-body` so each column's card list scrolls independently. Column headers stay fixed above the scroll area. Side panel and modal are unaffected. Added a mobile media query override (`overflow-y: visible` on `.col-body`, `overflow-y: auto` on `.board`) so stacked columns on narrow screens revert to a single board scroll instead.",
      "timestamp": "2026-06-24T12:00:00+00:00"
    }
  ]
}
```

`claudeSessionId` reflects the most recent session to work the ticket — overwrite it each
time a (new or resumed) session picks the ticket back up. The history entries preserve the
full record of which session did what.

## Live updates on tickets

When working a ticket, the user needs to know the current status live. As you work tickets or are blocked and need input,
make sure that you are moving hte ticket appropriately. When finished, move the ticket to Completed and make sure to leave
a comment summarizing your work in <200 words.
Do NOT leave notes in the history.

## Git workflow

**Check `useWorktrees` in the board's `_meta.json` first** — it determines whether you branch, use a worktree, or commit directly to the main branch.

**Use a worktree only if this project enables it.**
Worktrees are a **per-project setting**: the board's `_meta.json` carries a `useWorktrees`
boolean (toggled from the Project Settings page). Read it before choosing how to isolate:

- **`useWorktrees: true`** — create a branch, then work your code changes in a git worktree.
  Name the branch `<id>-Feature-Name` (e.g. `25-Worktree-Guidance`). If the `EnterWorktree`
  tool is available, prefer it (it handles placement and cleanup automatically); otherwise
  `git worktree add .claude/worktrees/ticket-<id> -b <id>-Feature-Name` from the project's
  repo root (the `directory` field on `_meta.json`), verifying `.claude/worktrees/` is in
  `.gitignore` first.
- **`useWorktrees: false` or unset** — do **not** create a worktree and do **not** create a
  new branch. Work directly on the default branch (master/main/production) in the repo root
  directory (`directory` in `_meta.json`). Commit your changes there when done.

**Board path anchoring.** A worktree moves your working directory away from the workspace
root, so a cwd-relative `.kanban/...` path no longer resolves. Always read and edit your
ticket JSON, `_meta.json`, and any `.kanban/` skills via their **absolute** paths (the
dispatch prompt gives your ticket file as an absolute path) — never a relative `.kanban/...`.

**Before committing, honor the board's commit requirements.**
A board may set a free-text `commitRequirements` field in its `_meta.json` (editable
from the UI's **Board** settings button). It states, in natural language, what must
hold before you commit — e.g. "all tests must pass before committing." Read it from the
board's `_meta.json` and satisfy it before running `git commit`. If you cannot satisfy
it, do not commit — escalate (move the ticket to `blocked` with an `orchestrator.question`).

When a `commitRequirements` field is set, also record the outcome on the ticket so the
orchestrator's auto-commit (below) can gate on it: write a top-level `commitGate` object
`{ "requirementsMet": <bool>, "summary": "<what you ran/verified>" }`. The orchestrator
will NOT commit on an unverified gate (no `commitGate`, or `requirementsMet` false).

**3. Commit your changes when done.**
Write a short, descriptive commit message referencing the ticket id:

```bash
git add <changed files>
git commit -m "ticket #<id>: <what changed>"
```

**4. Push the branch.**

```bash
git push -u origin <id>-Feature-Name
```

**Exception:** All `.kanban/` files — ticket JSON, `CLAUDE.md`, config profiles, orchestrator state, skills, and any other file under `.kanban/` — are always committed directly to `master`. Never create a branch for kanban-only changes. Only non-kanban source files require a branch.

**Auto-commit on completion.** When the orchestrator reaps a ticket as `completed`, it
inspects the working-tree changes: if they are **entirely** under `.kanban/` (kanban-only
work), it auto-commits them to the current branch (master) with a `ticket #<id>: <title>`
message and the agent's `commitGate.summary` as the body — gated by the board's
`commitRequirements` (see above). If any changed file is outside `.kanban/`, it instead
publishes the work to the isolated `ticket/<id>-<slug>` branch as before. Auto-commit is
best-effort (a missing git / non-repo tree never blocks completion); the outcome is always
recorded in a ticket comment.

> **Which repo gets the commit.** The workspace root (the parent of `.kanban`) is **not** a
> single git repo — `.kanban` and each sibling top-level directory (a checked-out Salesforce
> repo, etc.) are independent repos. So the orchestrator does not run git at the root; it
> `cd`s into the repo the changed files actually live in and commits from there
> (`discover_changed_paths` walks each sub-repo, prefixing its `git status --porcelain` paths
> with the repo dir name; `repo_dir_for_paths` then maps a change set back to its repo —
> `.kanban/…` → the `.kanban` repo, `acme-sfdx2/…` → that sub-repo). A change set spanning
> two different sub-repos can't pick one and falls back to the (no-op) root.

## Orchestrator

`orchestrator.py` is a headless loop that works the board autonomously. Each ~60s tick it
reaps finished/killed/stalled sub-agents, then (if enabled) asks Opus to triage eligible
tickets and dispatches headless `claude -p` sub-agents up to the concurrency cap. Decision
logic lives in `orchestrator_core.py` (unit-tested); `orchestrator.py` is the runtime that
spawns real processes.

Run it: `python .kanban/orchestrator.py` (alongside `kanban_server.py`). You can also open a
normal `claude` CLI in this workspace to talk to it — it reads the same files and the same
triage prompt (`orchestrator_triage_prompt.md`).

- **Profiles** live in `.kanban/config/<name>.json` (`whenToUse`, `model`, `allowedTools`,
  `systemPrompt`). Opus picks the best-fit profile by `whenToUse` and may override the model
  per ticket. `config/` is never a board (no `_meta.json`). Manage them in the **Profiles** tab.
- **Control state** is `.kanban/_orchestrator/state.json` (`enabled`, `concurrencyCap`,
  `stopAllRequested`), toggled from the **Orchestrator** tab. `enabled:false` pauses new
  dispatch (reaping still runs); "Stop all" kills everything in flight.
- **Docker dev-workspace (tickets #16, #6).** A board can set `useDocker: true` (Project
  Settings) to run its dispatched agent INSIDE a per-repo Docker container instead of a
  plain host subprocess. **A `useDocker` board must ship its own per-board Dockerfile at
  `_orchestrator/docker/<board-slug>.Dockerfile`** — the orchestrator builds each board's
  image from that file (tag `ai-kanban-workspace:<board>`), NOT from the generic
  `_orchestrator/docker/Dockerfile` (which is only a copy-me template and can't run most
  boards' tests). If a useDocker board has no per-board Dockerfile, a dispatch-time
  **preflight blocks the ticket** with an actionable `orchestrator.question` telling the
  human to create the file — it never silently falls back to the generic node image.
  The container bind-mounts the workspace root at `/workspace` (so the agent sees both its
  repo and the `.kanban` tree), translates host paths in the prompt onto that mount, and
  supplies the board's editable `envVars` map via `--env-file`.
  **Env split (ticket #7):** `envVars` is a `{KEY: VALUE}` map of NON-secret config
  (`NODE_ENV`, feature flags) — values live on disk in `_meta.json` (gitignored) and ride in
  via `--env-file`. `passthroughEnv` is a list of secret env-var **NAMES** (GitHub push
  token, `DATABASE_URL`, `RENDER_API_KEY`, Discord token) — only the name is stored; the
  orchestrator forwards each present name with a bare `docker run -e NAME` so the VALUE is
  inherited from its own environment and is never written to `_meta.json`, the env-file, or
  the image. A `passthroughEnv` name absent from the orchestrator env is skipped but logged
  as a warning in the run log (visible, not silently dropped). The hardcoded Anthropic
  credentials (`ANTHROPIC_API_KEY` and friends) always forward first, then the board's
  `passthroughEnv`, de-duplicated. Requires Docker on the host. Container reap/kill is
  by name (`marker.containerName` → `docker kill`). Off by default; existing boards are
  unaffected.
  **Git in the container (ticket #8):** the `/workspace` mount is host-owned, so git
  inside the container would trip its dubious-ownership guard and has no identity of its
  own. The Dockerfile template (copied per board) runs
  `git config --global --add safe.directory '*'` and sets a fallback
  `user.name`/`user.email`, so in-container `git commit` and `git worktree add` succeed on
  the shared volume. **Push stays host-side:** the container only makes local commits; the
  orchestrator auto-commits/pushes after reap using host credentials (`autoCommit`/`autoPush`
  in `state.json`). A board may forward a real author via `passthroughEnv`
  (`GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_*`), which overrides the fallback.
  **End-to-end validated on ai-kanban (ticket #10):** `_orchestrator/docker/ai-kanban.Dockerfile`
  (node:20-slim + Python 3 + git + Claude CLI + `pytest`/`psutil`) was built for real and ran
  `python -m pytest tests/` against the workspace-root mount in-container, plus a real
  secret-passthrough and in-container `git commit` check. In-container the suite is **407/410
  passing**: the 3 failures are `test_spawn_agent_*`, whose `fake_popen` mocks model only the
  Windows spawn path — the Linux container takes the POSIX `start_new_session` branch, so those
  tests are host-OS-specific, not a container/toolchain defect (on the Windows host the suite is
  fully green, 408/408). **`useDocker` is OFF for ai-kanban by default** — turning it on
  (Project Settings → `useDocker: true`) containerizes *every* future ai-kanban dispatch and
  requires `ai-kanban.Dockerfile` to stay green (the image must keep building and the suite must
  keep passing in it), so leave it off unless you are deliberately exercising Docker mode.
  Validation is a manual `docker build`/`docker run` (see the ticket #10 comment), not a
  standing test — the pytest suite mocks Docker to stay fast and host-independent.
- **Activity feed** is `.kanban/_orchestrator/activity.json`; per-run sub-agent logs are under
  `_orchestrator/runs/`. Both `config/` and `_orchestrator/` are excluded from board scans.
- **In-flight marker** on a ticket: an `orchestrator` block (`state`, `profile`, `model`,
  `pid`, `dispatchedAt`, `killRequested`, `logFile`). Field ownership: the loop writes only the
  `orchestrator` block + `status` + appends `history`; sub-agents write only `comments` and
  `question`/result fields.
- **Eligibility:** a ticket is dispatchable when its `dependsOn` are all `completed` (or none)
  and it is not in-flight/`completed`/`in_progress`. A `blocked` ticket whose
  `orchestrator.question.answer` is set re-dispatches automatically.
- **Human-attention questions:** a sub-agent that needs input sets `status:"blocked"` and
  writes `orchestrator.question` (`type` ∈ `input`|`choice`; every answer also carries free-text
  `notes`, answer shape `{value, notes}`). Answer it in the Orchestrator tab; the ticket
  auto re-dispatches next tick.
- **Kill:** Orchestrator-tab kill buttons hit `POST /api/orchestrator/kill/<board>/<id>`
  (instant if the PID is alive, else queued via `killRequested`). The loop also reaps stalled
  agents on its own (gated by a productivity check).

## Performance tab

The **Performance** tab (`perf_monitor.py` + `GET /api/performance`) discovers every
`claude.exe` session on the PC — including orphaned/external ones the orchestrator never
spawned — rolls up each session's subprocess-tree CPU/memory, graphs usage over a rolling
~5-minute window, and can kill a session's whole tree (`POST /api/performance/kill/<pid>`).

> The Performance tab needs `psutil` (`pip install psutil`). Without it the tab shows an
> install hint and the rest of the server works normally.
