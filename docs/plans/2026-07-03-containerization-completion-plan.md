# Containerization completion — implementation plan

Spec: `docs/superpowers/specs/2026-07-03-containerization-completion-design.md`
Board: ai-kanban (`useWorktrees: true`; commitRequirements: all tests pass via
`python -m pytest` from repo root, then merge into `release`).

Four tickets. #6/#7/#8 are independent; #10 depends on all three. Concurrency cap
is 1, so they serialize. Every ticket updates docs (CLAUDE.md Docker bullet +
Project Settings help text) as part of its acceptance, and adds tests.

---

## Ticket #6 — Per-board Dockerfile required + preflight block

**Goal:** `useDocker` boards build from `_orchestrator/docker/<board>.Dockerfile`;
a missing one blocks the ticket with an actionable question instead of silently
using the generic node image.

- `orchestrator_core.py`
  - `board_dockerfile_name(board)` → `f"{_docker_safe(board)}.Dockerfile"`.
  - `resolve_board_dockerfile(docker_dir, board)` → per-board path if it exists,
    else `None`.
  - `docker_preflight(docker_dir, board_meta, board)` → `(ok: bool, reason: str)`:
    ok only when `use_docker` and a per-board Dockerfile exists; reason is a
    human-readable fix ("create `_orchestrator/docker/<board>.Dockerfile`").
- `orchestrator.py`
  - `_build_docker_image` builds from the resolved per-board Dockerfile.
  - Before dispatch, when `use_docker` is on, run the preflight; on failure move
    the ticket to `blocked` and write `orchestrator.question` (type `input`) with
    the reason. Do **not** spawn.
- Keep generic `Dockerfile` as a documented template.
- Tests: resolver prefers per-board; preflight passes/fails correctly; dispatch is
  blocked (not spawned) when the per-board Dockerfile is missing.
- Docs: CLAUDE.md Docker bullet notes the per-board Dockerfile requirement.

## Ticket #7 — Name-only secret passthrough (`passthroughEnv`)

**Goal:** forward named secrets from the orchestrator env into the container
without writing any value to disk.

- `orchestrator_core.py`
  - `board_passthrough_env(board_meta)` → de-duplicated list of valid env-var
    names from `passthroughEnv` (drop invalid names, non-strings).
- `orchestrator.py`
  - `_docker_dispatch` forwards `_DOCKER_PASSTHROUGH_ENV` (as today) **plus** the
    board's `passthroughEnv` names that are present in `os.environ`, de-duplicated.
    A listed-but-absent name is skipped and logged as a warning.
- `kanban_server.py`
  - `update_board_meta` persists/sanitizes `passthroughEnv` (list of valid names);
    empty list removes the field. `load_board` exposes it.
- `kanban.html`
  - Project Settings: "Forwarded secret names (one per line)" field. Help text:
    names only — values come from the orchestrator's environment and are never
    stored.
- Tests: sanitizing keeps valid names / drops junk; dispatch adds `-e NAME` for
  present names and skips absent ones; server persist/sanitize round-trip.
- Docs: CLAUDE.md + UI help updated to describe `passthroughEnv` vs `envVars`.

## Ticket #8 — Git works inside the container

**Goal:** in-container `git commit` and worktree creation succeed on the mounted
volume; push stays host-side.

- Generic Dockerfile template + (later) per-board templates include:
  - `git config --global --add safe.directory '*'`
  - a fallback identity (`git config --global user.name/email`).
- Optionally allow git identity via `passthroughEnv` (documented).
- Test: a unit test asserting the template/`docker_run` path sets safe.directory
  (or that the dispatch forwards identity when named). Full end-to-end commit is
  exercised by #10.
- Docs: note that push is performed host-side by the orchestrator.

## Ticket #10 — End-to-end validation on ai-kanban (depends on #6, #7, #8)

**Goal:** prove the full path with a real container.

- Add `_orchestrator/docker/ai-kanban.Dockerfile`: Python 3 + Node + git + Claude
  CLI + `pip install pytest psutil` (and anything else the suite imports).
- Real smoke: build the image and run `python -m pytest` inside a container with
  the workspace mounted; capture the result. (A script under
  `_orchestrator/docker/` or a documented manual run is fine — it must actually
  invoke real `docker`, not mocks.)
- Confirm secret passthrough end-to-end (e.g. a dummy named var forwarded and
  visible in-container; absent one skipped with a warning).
- Leave ai-kanban `useDocker` **off** after the probe; document the on-switch and
  the fact that turning it on requires the ai-kanban Dockerfile to stay green.
- Report results in the ticket comment (image size, pytest summary, any caveats).

---

## Sequencing & risk

- Cap=1 serializes work; #10 last via `dependsOn: [6,7,8]`.
- #10 flipping `useDocker` on affects later ai-kanban dispatches — it must restore
  `useDocker:false` after the probe. Flagged in the ticket.
- If a real `docker build`/`run` is slow or fails on this host, #10 blocks with a
  question rather than faking success.
