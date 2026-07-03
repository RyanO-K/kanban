# Per-board Docker image for the **ai-kanban** board (ticket #10).
#
# This is a REAL per-board Dockerfile (not the copy-me template at
# `_orchestrator/docker/Dockerfile`). The orchestrator builds it as
#   docker build -t ai-kanban-workspace:ai-kanban \
#     -f _orchestrator/docker/ai-kanban.Dockerfile _orchestrator/docker
# and runs the dispatched `claude -p` agent inside a container from it, with the
# workspace root bind-mounted at /workspace.
#
# The ai-kanban board's tests run with `python -m pytest` from the .kanban repo
# root, so this image adds Python 3 + pytest + psutil on top of the base
# toolchain (Node + git + the Claude CLI). Nothing secret is baked in — the
# orchestrator forwards ANTHROPIC_API_KEY (and any `passthroughEnv` names) via
# `docker run -e NAME`, and non-secret board config arrives via `--env-file`.
FROM node:20-slim

# --- Base toolchain: git + Python 3 -----------------------------------------
# node:20-slim is Debian bookworm. Python 3 + venv + pip cover the test suite;
# git is needed for in-container commits on the mounted volume (ticket #8).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI, published on npm. Unpinned tracks latest; pin (…@x.y.z)
# for reproducible builds.
RUN npm install -g @anthropic-ai/claude-code

# --- Python test dependencies -----------------------------------------------
# The suite imports only pytest (runner) and psutil (perf_monitor.py); every
# other import is stdlib. bookworm's system Python is PEP 668
# "externally-managed", so `pip install` needs --break-system-packages. That is
# safe here: the container is ephemeral (`--rm`) and this is a dedicated test
# image, not a shared host interpreter.
RUN pip install --no-cache-dir --break-system-packages pytest psutil

# --- Git inside the container (ticket #8) ------------------------------------
# The workspace is bind-mounted from the host, so every repo under it is owned by
# the host user, not the container user. Git refuses to operate on a repo it sees
# as owned by someone else ("detected dubious ownership"), which breaks
# in-container `git commit` and `git worktree add`. Trusting every path clears
# that guard. This only relaxes git's ownership check; the container is ephemeral
# (`--rm`) and already runs on the host user's own volume.
RUN git config --global --add safe.directory '*'

# A fallback commit identity, so `git commit` never aborts for lack of a
# user.name / user.email in the fresh container. PUSH STAYS HOST-SIDE: the
# orchestrator auto-commits/pushes after reap using host credentials. A board
# may forward a real author via `passthroughEnv` (GIT_AUTHOR_NAME/EMAIL,
# GIT_COMMITTER_NAME/EMAIL), which overrides this at commit time.
RUN git config --global user.name "AI Kanban Agent" \
    && git config --global user.email "agent@ai-kanban.local"

WORKDIR /workspace

# `spawn_agent` supplies the full command (`claude -p <prompt> …`); no ENTRYPOINT
# is set so `docker run <image> claude -p …` runs exactly what the host would.
CMD ["bash"]
