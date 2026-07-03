# Copy-me template for AI-kanban Docker-mode agents (tickets #16, #6).
#
# This file is a TEMPLATE, not the image any board actually builds from. A
# `useDocker` board must have its own per-board Dockerfile at
# `_orchestrator/docker/<board-slug>.Dockerfile`; the orchestrator builds from
# that (`docker build -t ai-kanban-workspace:<board> -f <board>.Dockerfile .`)
# and runs the dispatched `claude -p` agent inside a container from it, with the
# workspace root bind-mounted at /workspace. If a useDocker board has no per-board
# Dockerfile, the orchestrator BLOCKS the ticket with a question (it does NOT fall
# back to this generic node image, which can't run most boards' tests).
#
# To onboard a board: copy this file to `<board-slug>.Dockerfile` and add the
# toolchain that board's tests need (e.g. Python + pytest). The image only needs
# the Claude Code CLI, git, and the runtime the agent expects to find on PATH.
#
# Credentials are NOT baked in — the orchestrator forwards ANTHROPIC_API_KEY
# (and friends) from its own environment via `docker run -e`, and per-board env
# vars arrive via `--env-file`. Nothing secret is written into this image.
#
# Customize per your projects: add language toolchains (Python, JDK, the SF CLI,
# etc.) your board repos need to build/test. Rebuilds are cached by Docker, so a
# heavier image only pays its cost once.
FROM node:20-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI, published on npm. Pinning a version here (…@x.y.z) makes
# builds reproducible; leaving it unpinned tracks latest.
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /workspace

# `spawn_agent` supplies the full command (`claude -p <prompt> …`); no ENTRYPOINT
# is set so `docker run <image> claude -p …` runs exactly what the host would.
CMD ["bash"]
