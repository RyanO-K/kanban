# Workspace image for AI-kanban Docker-mode agents (ticket #16).
#
# The orchestrator builds one image per board from this template
# (`docker build -t ai-kanban-workspace:<board> -f Dockerfile .`) and runs the
# dispatched `claude -p` agent inside a container from it, with the workspace
# root bind-mounted at /workspace. The image therefore only needs the Claude
# Code CLI, git, and the runtime the agent expects to find on PATH.
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
