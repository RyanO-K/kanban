# GitHub Integration Implementation Plan

**Ticket:** `.kanban/kanban-dev/5.json`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development
> for every code unit below — write the failing test first, then implement.
> Use superpowers:subagent-driven-development or superpowers:executing-plans to
> work this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> Stdlib only; no new pip dependencies. `gh` owns GitHub auth.

**Goal:** Two-way exchange of kanban tickets with GitHub Issues via the `gh` CLI:
export a ticket as an issue, import an issue by number, and store a `github`
correlation block on the ticket (issue number/url/state + optional project).
See the companion spec: `docs/specs/2026-06-26-github-integration-design.md`.

**Architecture:** A pure-logic module `github_integration.py` (argv building,
`gh` JSON-output parsing, issue⇄ticket mapping, export/no-op decision) plus thin
handlers in `kanban_server.py` that own the single `subprocess.run(["gh", ...])`
seam and return the existing `(data, status)` tuple. No front-end in v1.

---

## Task 0 — Resolve the two human decisions (BLOCKING)

Do not write code until these are answered; they change argv and storage.

- [ ] Confirm the default target **repo** (`owner/name`) for the `kanban-dev`
      board. (Workspace is not a git repo, so `gh` can't infer it — it must be
      configured in `_meta.json`.)
- [ ] Confirm whether **project correlation** must use GitHub Projects v2
      (`gh project item-add`, requires a project number + permissions) or is
      satisfied by storing `projectNumber` as ticket metadata only.

If either is unknown, escalate via the ticket `orchestrator.question` flow rather
than guessing.

## Task 1 — Add the `github` config + ticket field (no behavior yet)

- [ ] Document the `github` block on the ticket shape in `.kanban/CLAUDE.md`
      (Ticket shape section) — additive, optional.
- [ ] Add a board-level `github` block (`repo`, optional `projectNumber`) to
      `kanban-dev/_meta.json` once Task 0 gives the repo. Confirm `load_board`
      passes board-level metadata through (it already passes `context`,
      `openQuestions`, `outOfScope`; add `github` to that pass-through list).
- [ ] Failing test: `load_board` includes the board `github` block in its result.

## Task 2 — `github_integration.py` pure module (TDD)

- [ ] Failing test: `build_create_argv(ticket, repo)` → correct `gh issue create`
      argv list (title, body-with-`Kanban: slug/id` footer, `--repo`), with ticket
      text passed as list items (never shell-interpolated).
- [ ] Failing test: `build_view_argv(number, repo)` → `gh issue view <n> --repo
      <r> --json number,title,body,state,url,labels`.
- [ ] Failing test: `parse_issue_json(stdout)` → dict mapping issue → ticket
      fields; handles missing optional keys.
- [ ] Failing test: `issue_to_ticket(issue, slug)` → new-ticket payload
      (title, detail, status `todo`, populated `github` block).
- [ ] Failing test: `export_decision(ticket, force)` → `noop` when already linked
      and not forced, else `create`.
- [ ] Implement each to green. No subprocess/network in this module.

## Task 3 — Subprocess seam + handlers in `kanban_server.py` (TDD)

- [ ] Add a `_run_gh(argv, timeout=...)` helper: `subprocess.run` with a list
      argv, `capture_output=True`, `text=True`, explicit timeout, `shell=False`.
      Returns `(returncode, stdout, stderr)`; raises nothing for non-zero.
- [ ] Failing test (seam stubbed): `github_export(slug, id, payload)` stores the
      `github` block, appends a `comment` summarizing the link, returns
      `{issueNumber, issueUrl}`, status 200. Re-export without `force` → no-op 200
      returning the existing link.
- [ ] Failing test (seam stubbed): `github_import(slug, payload)` with `number`
      creates a ticket via the existing `create_task` path with the `github`
      block; importing a number already linked updates that ticket instead of
      duplicating.
- [ ] Failing test: `gh` missing (seam raises `FileNotFoundError`) → graceful
      `{"error": "GitHub CLI (gh) is not installed"}`, non-2xx.
- [ ] Failing test: `gh` non-zero / auth error → 502 carrying trimmed stderr.
- [ ] Implement handlers to green.

## Task 4 — Wire routes into `do_POST`

- [ ] `POST /api/board/<slug>/task/<id>/github/export` (8 parts) → `github_export`.
- [ ] `POST /api/board/<slug>/github/import` (6 parts) → `github_import`.
- [ ] (optional) `POST /api/board/<slug>/task/<id>/github/sync` → `github_sync`.
- [ ] Match the existing length/shape dispatch style; add 404 fallthrough cases.
- [ ] Failing test: each route maps to its handler and rejects malformed paths.

## Task 5 — (Optional, can be a follow-up ticket) UI controls

- [ ] Add "Export to GitHub" on the ticket side panel and an "Import #__" input
      to `kanban.html`, calling the new endpoints. Behind the same graceful-error
      messaging. Defer if v1 is API-only.

## Task 6 — Verify & wrap up

- [ ] Run the full board test suite; all green (superpowers:verification-before-completion).
- [ ] Manual smoke against a real repo once Task 0's repo is set:
      `gh auth status`, export one ticket, confirm the issue exists, import it
      back, confirm round-trip via the `Kanban: slug/id` footer.
- [ ] Git: work on branch `5-Github-Integration`, commit referencing ticket #5,
      push (per `.kanban/CLAUDE.md` git workflow). Note: workspace must be
      `git init`'d first (it currently is not).
- [ ] Move ticket to `completed`, leave a <200-word summary comment (writer
      `Claude`).
