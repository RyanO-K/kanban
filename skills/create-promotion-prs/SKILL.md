---
name: create-promotion-prs
description: Use when you need to raise Salesforce promotion PRs (promo/<source>-to-<target>) for the acme-sfdx repo from the CLI — dispatches the manual-create-promotion-prs.yml GitHub Actions workflow via `gh workflow run` instead of opening a throwaway PR into `partial`.
---

# Creating promotion PRs from the CLI

The `acme-sfdx` repo promotes branches into `production`/`full`/etc. by creating
`promo/<source>-to-<target>` branches and PRs. Normally that's triggered automatically
by `create-promotion-branch.yml` when a PR is opened against `partial`. The
**`manual-create-promotion-prs.yml`** workflow lets you skip the throwaway PR and kick
the same process off directly from the Actions tab — or, as documented here, from the CLI.

## Prerequisites

- `gh` (GitHub CLI) authenticated against `github.com` with access to `acme/acme-sfdx`
  (`gh auth status` to check).
- The source branch you want to promote must already be pushed to the remote.

## Key fact: where you run it from

Kanban workers run with `cwd` = the workspace root (`…/GitHub/`), **not** inside the
`acme-sfdx2` checkout. So you must point `gh` at the repo explicitly with `-R`:

```bash
gh workflow run manual-create-promotion-prs.yml \
  -R acme/acme-sfdx \
  -f source_branch=<your-branch> \
  -f target_branches="production full" \
  -f description_of_changes="<what changed>" \
  -f fixes_issue="<issue number, no #, or omit>" \
  -f test_coverage="<e.g. 85% or NA>"
```

Alternatively `cd acme-sfdx2` first and drop the `-R` flag — but `-R` is more robust
because it doesn't depend on the worktree layout.

## Workflow inputs

The workflow is `workflow_dispatch` only. Inputs (from
`.github/workflows/manual-create-promotion-prs.yml`):

| Input | Required | Default | Notes |
|---|---|---|---|
| `source_branch` | **yes** | — | Branch to promote (e.g. your feature/integration branch). Must exist on the remote. |
| `target_branches` | no | `production full` | Space-separated list. One promo branch + PR per target. |
| `description_of_changes` | **yes** | — | Free text; goes into the PR body template. |
| `fixes_issue` | no | — | Issue number **without** the `#`. Blank → PR body gets `Fixes #(XXX)` placeholder. |
| `test_coverage` | no | `NA` | E.g. `85%` or `NA`; goes into the PR body. |

The PR body is assembled from `fixes_issue`, `description_of_changes`, and `test_coverage`
into the repo's standard template. Branch creation reuses `create-branches.sh` (the same
script the automatic flow calls), driven by `SOURCE_BRANCH` / `TARGET_BRANCHES` / `PR_BODY`.

## Which ref the workflow definition comes from

`gh workflow run` resolves the workflow definition from a ref. By default that's the repo's
**default branch (`production`)**. The workflow is present on `production`, so the plain
command above works. If you've changed the workflow on a feature branch and want to test
that version, dispatch against it explicitly:

```bash
gh workflow run manual-create-promotion-prs.yml -R acme/acme-sfdx --ref <branch> -f …
```

(`--ref` must point at a ref where the workflow file exists, or the dispatch 404s.)

## After dispatching

`gh workflow run` returns immediately and prints no run id. To find and watch the run:

```bash
gh run list -R acme/acme-sfdx --workflow manual-create-promotion-prs.yml -L 5
gh run watch -R acme/acme-sfdx <run-id>
```

The workflow runs on `self-hosted` runners, simulates the deploy, and posts `check-deploy`
/ `check-issue` check-runs onto each created promo PR. A merge conflict on a target shows
up as a failed `check-deploy` on that target's PR rather than a failed dispatch.

## Common pitfalls

- **404 on dispatch** → the workflow file doesn't exist on the ref you targeted. Default ref
  is `production`; use `--ref <branch>` to target a branch that has it.
- **"could not determine a repository"** → you're in the workspace root, not a repo. Add
  `-R acme/acme-sfdx`.
- **Source branch not found** → push it to the remote first; the workflow checks out the
  remote, not your local working tree.
