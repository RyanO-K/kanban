# Per-board Docker image for the **barnumhardis2-sf-dev** board.
#
# Built by the orchestrator as:
#   docker build -t ai-kanban-workspace:barnumhardis2-sf-dev \
#     -f _orchestrator/docker/barnumhardis2-sf-dev.Dockerfile \
#     _orchestrator/docker
# and used when `useDocker: true` is set on the board in _meta.json (currently
# false — create this image first, verify it builds, then flip the flag).
#
# Toolchain rationale
# -------------------
# Node 22: required by @salesforce/cli's bundled undici 8.0.3+, which calls
#   worker_threads.markAsUncloneable — that API does not exist in Node 20.
#   The runner/Dockerfile in barnumHardis2 pins the same version for this reason.
#
# @salesforce/cli pinned at 2.140.6: matches the pin in runner/Dockerfile.
#   Unpinned installs risk pulling an undici/Node version mismatch that silently
#   fails at sf module load time. Bump deliberately when updating the runner image.
#
# PUPPETEER_SKIP_DOWNLOAD: sfdx-hardis depends on puppeteer which otherwise
#   downloads ~150 MB of headless Chrome. The kanban agent does not use browser
#   features, and the download inflates the image and can cause ENOSPC during
#   build — skip it.
#
# Python 3 + simple-salesforce: kanban agents may run scripts/deploy-to-org.sh,
#   scripts/object_import.py, scripts/parse_org_alias.py, and other helpers under
#   scripts/ that import simple-salesforce, requests, python-dotenv, and PyYAML.
#   The full list matches runner/admin_scripts_requirements.txt.
#
# Credentials are NOT baked in — ANTHROPIC_API_KEY (and any passthroughEnv
# names) forward via `docker run -e NAME`; non-secret board config arrives via
# `--env-file`. SF org auth (SFDX_AUTH_URL or JWT key) must be in passthroughEnv.
FROM node:22-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PUPPETEER_SKIP_DOWNLOAD=true

# --- Base system deps -----------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        jq \
        ca-certificates \
        gnupg \
        unzip \
        zip \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- Salesforce CLI + plugins ---------------------------------------------------
# Pin version to match runner/Dockerfile so undici stays in lockstep with Node 22.
RUN npm install -g @salesforce/cli@2.140.6 \
    && sf plugins install @salesforce/plugin-packaging \
    && echo 'y' | sf plugins install sfdx-hardis \
    && echo 'y' | sf plugins install sfdx-git-delta

# --- Claude Code CLI ------------------------------------------------------------
RUN npm install -g @anthropic-ai/claude-code

# --- Python deps ----------------------------------------------------------------
# Matches runner/admin_scripts_requirements.txt (the set scripts/ helpers need).
# --break-system-packages: node:22-slim is a PEP 668 externally-managed env.
# Safe here: the container is ephemeral (--rm) and a dedicated image.
RUN pip install --no-cache-dir --break-system-packages \
        PyYAML \
        cryptography \
        python-dotenv \
        aiofiles \
        zeep \
        requests \
        simple-salesforce

# --- Git inside the container (ticket #8 pattern) --------------------------------
# The workspace is bind-mounted from the host so repos are owned by the host user.
# Git's dubious-ownership guard blocks in-container commits without this.
RUN git config --global --add safe.directory '*'

# Fallback commit identity for in-container git commits. Push stays host-side;
# the orchestrator auto-commits/pushes after reap using host credentials.
# Override via passthroughEnv: GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL /
# GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL.
RUN git config --global user.name "AI Kanban Agent" \
    && git config --global user.email "agent@ai-kanban.local"

WORKDIR /workspace

# spawn_agent supplies the full `claude -p <prompt> …` command; no ENTRYPOINT
# is set so `docker run <image> claude -p …` runs exactly what the host would.
CMD ["bash"]
