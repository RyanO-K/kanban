# Containerization completion — design spec

**Date:** 2026-07-03
**Board:** ai-kanban
**Builds on:** ai-kanban ticket #2 "Containerization" (branch `containerized`, merged)

## Problem

A board can set `useDocker: true` so the orchestrator runs its dispatched
`claude -p` agent inside a per-board Docker container instead of a host
subprocess. The mechanism is built and unit-tested, but it is not yet usable for
a real board:

1. **Toolchain** — the only Dockerfile is `node:20-slim` + git + Claude CLI. Most
   boards need Python/pytest to satisfy their `commitRequirements`. A
   containerized agent cannot run the tests it is required to pass.
2. **Secrets** — env passthrough is hardcoded to four Anthropic vars. Every other
   secret a board needs (GitHub push token, `DATABASE_URL`, `RENDER_API_KEY`,
   Discord token) is forced into `envVars` as plaintext on disk. There is no way
   to give a container a non-Anthropic secret *without* writing it to disk.
3. **Git** — the container has no git identity, and bind-mounted repos trip git's
   dubious-ownership guard, so in-container `git commit` / worktree creation fail.
4. **Never validated** — every test mocks `docker`; no container has been built or
   run end-to-end.

## What is already solid (do not rebuild)

- `orchestrator_core.py` pure logic: `use_docker`, `board_env_vars` (sanitizing),
  `render_env_file`, `docker_image_tag`, `docker_container_name`,
  `translate_host_paths`, `docker_build_argv`, `docker_run_argv`.
- `orchestrator.py` wiring: `spawn_agent` → `_docker_dispatch` (build image,
  bind-mount workspace root at `/workspace`, `--env-file`, forward Anthropic creds
  by name), `_kill_container` teardown by name on reap/kill.
- Server persistence + Project Settings UI for `useDocker` + `envVars`.
- **Security baseline (keep):** Anthropic creds forwarded by *name*
  (`docker run -e NAME`, value inherited) — never written to `_meta.json` or the
  image. `envVars` sanitized. Rendered env-file lives under gitignored
  `_orchestrator/docker/env/`. `*.json` is gitignored, so `_meta.json` (and
  `envVars`) never reaches the published repo. Containers are `--rm` ephemeral.

## Design

### 1. Secret handling — name-only passthrough (`passthroughEnv`)

A board's `_meta.json` gains `passthroughEnv`: a list of environment-variable
**names**. The orchestrator forwards each present name with `docker run -e NAME`
so the value is inherited from the orchestrator's own environment and is **never
written to `_meta.json`, the env-file, or the image**. Split of responsibility:

- `passthroughEnv` → secrets (GitHub token, DB URL, API keys). Names on disk,
  values only in the orchestrator's process env.
- `envVars` → non-secret config (`NODE_ENV`, feature flags). Values on disk
  (gitignored) is acceptable because they are not secret.

Rules: each name validated by `valid_env_key`; a listed name that is **absent**
from the orchestrator env is skipped but logged as a warning (so a missing secret
is visible, not silently dropped). Hardcoded Anthropic defaults still always
forward. Order: Anthropic defaults, then board `passthroughEnv`, de-duplicated.

### 2. Toolchain — per-board Dockerfile required

Image build resolves `_orchestrator/docker/<board-slug>.Dockerfile`. If a board
has `useDocker: true` but no per-board Dockerfile, the orchestrator does **not**
silently fall back to the generic node image (which cannot run the board's
tests). Instead a preflight blocks the ticket with an actionable
`orchestrator.question` telling the human to create the file. The generic
`_orchestrator/docker/Dockerfile` remains as a copy-me template.

### 3. Git in the container

The per-board Dockerfile template sets a fallback git identity and
`git config --global --add safe.directory '*'` (bind-mounted repos are owned by
the host user; without this git refuses to operate inside the container).
In-container agents make commits on the shared volume; **push stays host-side** —
the orchestrator already auto-commits/pushes after reap (`autoCommit`/`autoPush`
in `state.json`) using host credentials. Git identity may also be forwarded via
`passthroughEnv` (`GIT_AUTHOR_NAME`, etc.) for boards that prefer that.

### 4. End-to-end validation on ai-kanban

Add `_orchestrator/docker/ai-kanban.Dockerfile` (Python 3 + Node + git + Claude
CLI + the repo's test deps: pytest, psutil). Run one real container that builds
the image and executes `python -m pytest` against the mounted repo, proving
toolchain + git + secret passthrough work together. Leave the board's
`useDocker` **off** by default after the probe; document how to turn it on.

## Security posture when finished

- Secrets reach containers **by name only**; no secret is written to disk at any
  layer. Non-secret config in `envVars` stays gitignored.
- Container auth uses the forwarded `ANTHROPIC_API_KEY` (API-key mode). Host MCP
  servers and subscription credentials are **not** mounted by default — a
  deliberate secure-by-default choice. An opt-in read-only `~/.claude` mount for
  MCP parity is deferred as future work, not part of this effort.
- `/workspace` still mounts the whole workspace root (the agent needs its ticket
  JSON + skills + sibling repos). Narrowing the mount is noted as future
  hardening, out of scope here.

## Out of scope / future

- Opt-in read-only `~/.claude` mount for MCP/subscription-auth parity.
- Narrowing the bind mount below the full workspace root.
- Network-egress restriction on agent containers.

## Acceptance

A board with a valid per-board Dockerfile and `useDocker: true` runs its agent in
a container that: builds from the board's own Dockerfile, runs the board's tests,
commits on the shared volume for host-side push, and receives exactly the secrets
named in `passthroughEnv` with none written to disk — proven by a real
`python -m pytest` run in-container on ai-kanban. All non-docker boards unchanged.
