# VS Code Extension for the Kanban Board — Design

**Date:** 2026-08-17
**Status:** Approved direction (conversation with Ryan, 2026-08-17)

## Goal

Package the kanban board (server + web UI + orchestrator) as a VS Code extension that
installs on a fresh PC and works seamlessly. "Fresh PC" means: VS Code installed,
nothing else — no Python, no Node, no git, no Claude CLI.

## Decisions

### D1: Bundle a standalone Python runtime (Option B)

The extension ships a [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
CPython inside platform-specific `.vsix` packages (`vsce package --target win32-x64`,
initially Windows-only). The ~7,150 lines of tested Python (`app/`) ship unchanged as a
bundled copy. Rejected alternatives:

- **Require system Python** — fails the fresh-PC test (Windows ships a Store stub, not Python).
- **Port to TypeScript** — forfeits the 41-file pytest suite; possible later, not now.
- **Download runtime on first activation** — smaller vsix but breaks offline/corp-proxy
  installs; embedding at build time is deterministic.

`psutil` is pip-installed into the bundled runtime at build time, so the Performance tab
works out of the box.

### D2: Dependencies that cannot be bundled → popup with a one-line install command

`claude` CLI and `git` are host tools the extension cannot ship. On activation the
extension probes each (`<tool> --version`; the claude probe honors the
`kanban.claudePath` setting, so an off-PATH install configured there never
false-alarms); a missing tool raises a warning popup with two buttons:
**Install in Terminal** (opens an integrated terminal — pinned to PowerShell on
Windows, since the one-liner is PowerShell syntax and the default profile may be
cmd/Git Bash — and types the command without auto-executing) and **Copy Command**.
One-liners:

| Tool | Windows | macOS/Linux |
|---|---|---|
| claude | `irm https://claude.ai/install.ps1 \| iex` | `curl -fsSL https://claude.ai/install.sh \| bash` |
| git | `winget install --id Git.Git -e --source winget` | `xcode-select --install` / `sudo apt-get install -y git` |

Native installers (not npm) because a fresh PC has no Node. Claude *authentication* is
not preflighted — the orchestrator already detects a not-logged-in agent and blocks the
ticket with a "run `claude /login`" comment (`orchestrator.py:1844-1848`).

### D3: UI = webview panel containing an iframe to the localhost server

The extension keeps the Python HTTP server and renders `<iframe src="http://127.0.0.1:<port>/">`
inside a `WebviewPanel`, using the webview `portMapping` option (fixed in-webview port
8745 mapped to the dynamically allocated real port). Rationale:

- The server injects `window.KANBAN_TOKEN` into the served HTML (`kanban_server.py:2157-2168`)
  and the UI references root-relative `/kanban.css`, `/kanban.js` — both only work
  same-origin. Re-hosting assets on `vscode-webview://` URIs would force a postMessage
  bridge rewrite of `kanban.js` (2,322 lines). The iframe keeps all of it unchanged —
  with one known exception: the `vscode://` file links opened via `window.open`
  (`kanban.js:911-919`) were written for Simple Browser and may be blocked by the
  nested sandboxed iframe; smoke-tested during panel work, with a small postMessage
  bridge as the fallback.
- The webview CSP allows any loopback port (`frame-src http://127.0.0.1:* http://localhost:*`):
  the port mapping can surface as a redirect to the real port, and CSP re-evaluates
  `frame-src` against redirect targets.
- `extensionKind: ["workspace"]` is the right long-term shape for Remote-SSH/WSL (the
  server runs next to the workspace; `portMapping` forwards it), but v1 ships only a
  win32-x64 runtime, so on a non-Windows remote host the extension refuses with an
  explicit "needs a platform build" message. Remote support = adding linux-x64/darwin
  targets later; nothing architectural.
- The UI polls (no WebSockets), which is iframe-friendly.

### D4: Code/data split via `KANBAN_DATA_DIR`

Today code and data are entangled: `kanban_server.py:91` and `orchestrator_core.py:14`
both derive `KANBAN_DIR` from `__file__`'s parent, so boards/state live next to the app.
As an extension, code lives under `~/.vscode/extensions/...` while data must live in the
workspace. A `KANBAN_DATA_DIR` env var (set by the extension) overrides the data root
(`boards/`, `config/`, `_orchestrator/`, `skills/`, docs scanning); static assets stay
code-relative. Default (no env var) is unchanged, so standalone
`python app/kanban_server.py` keeps working exactly as today.

Data dir is always `<workspaceFolder>/.kanban` (no setting — YAGNI). A workspace that
contains a full clone of this repo (with `app/` inside `.kanban`) is automatically
compatible: the data-dir layout is a superset, and non-board dirs are already excluded
from board scans.

Fresh workspaces are seeded (never overwritten) with: `config/*.json` profiles, a
generic board guide as `CLAUDE.md`, `orchestrator_triage_prompt.md` (dispatch triage
reads it from the data dir — without it, dispatch still works via backfill but every
tick wastes a triage model call on a "dispatch nothing" fallback and loses all
prioritization/model selection), and an empty `skills/` dir (the dispatch prompt tells
agents the path exists).

### D5: One Python process; lifecycle owned by the extension

`kanban_server.py` already runs the orchestrator tick loop as an internal thread
(`kanban_server.py:1240-1242`), so the extension supervises exactly one child process:
spawn on activation with env (`KANBAN_PORT`, `KANBAN_DATA_DIR`, `KANBAN_TOKEN`,
`KANBAN_CLAUDE_PATH`), health-check by polling `GET /api/files`, restart with backoff on
crash, kill on `deactivate()`. One wrinkle: the UI's own "Restart server" button makes
the server replace ITSELF (on Windows: spawn a copy on the same port, exit 0 —
`kanban_server.py:1282-1284`), so on any child exit the supervisor probes the port
first and ADOPTS a live replacement instead of double-spawning onto one port; the
adopted process is unkillable by `dispose()` (accepted v1 limitation). The extension's
own `kanban.restartServer` command allocates a fresh port, so it recreates any open
board panel (a live panel's `portMapping` is frozen at creation). Window reload is already survivable by design: dispatched
agents are spawned detached, in-flight markers (PID + log file) persist on ticket JSON,
and the reap loop adopts them on restart. The port is auto-allocated by the extension
(free-port probe) and passed via `KANBAN_PORT` — no Python change needed; the
orchestrator single-instance lock (`orchestrator_core.py:735`) already handles two
windows on one workspace.

### D6: `claude` resolution hardening

Only the primary dispatch path resolves `claude` via `shutil.which` (`orchestrator.py:1089`);
four other **host-side** call sites use the bare literal (`1101` probe, `1650` summarizer,
`2087` model-assign, `2114` triage) — a problem because GUI-launched VS Code often lacks
the shell's PATH. All host-side sites route through `_claude_cmd()`, which gains a
`KANBAN_CLAUDE_PATH` env override (wired to a `kanban.claudePath` setting). The Docker
argv at `orchestrator.py:1203/1206` is the *in-container* command and stays bare.

### D7: Native VS Code touches (v1)

- **Status bar item**: orchestrator on/off + in-flight agent count; warning background
  when a blocked ticket has an unanswered `orchestrator.question`; click opens the board.
- **Notifications**: poll the board API on an interval; a *newly appearing* unanswered
  question raises `showWarningMessage` with an "Open Board" button.
- **Command `kanban.resumeTicketSession`**: quickpick a ticket with a `claudeSessionId`,
  open an integrated terminal running `claude --resume <sessionId>`.
- **`contributes.jsonValidation`**: JSON schemas for ticket files and `_meta.json`.

### D8: Descoped from v1

- **Layrr live-edit** — most fragile subsystem (needs node/npm on PATH, external layrr
  npm package, `prompt.js` expected inside it). Ships as-is in the bundle; the UI tab
  simply won't go live without node. No extension work.
- **Docker mode** — already opt-in per board with a preflight block when Docker is
  absent. No extension work.
- **Marketplace publishing** — v1 is a sideloaded `.vsix` (`code --install-extension`).
- **TreeView of tickets, FileSystemWatcher push updates** — the iframe UI already covers
  browsing; revisit later.
- **macOS/Linux vsix targets** — build script is triple-parameterized from day one, but
  only `win32-x64` is built and smoke-tested in v1. (Prereq for later: the
  `perf_monitor.py` name-match fix, which v1 does include.)

## Repo layout (new code lives in this repo, committed to master per repo convention)

```
.kanban/
  extension/
    package.json, tsconfig.json, .vscodeignore, esbuild.mjs, vitest.config.ts, README.md
    src/          extension.ts, runtime.ts, ports.ts, server.ts, bootstrap.ts,
                  deps.ts, panel.ts, attention.ts
    test/         *.test.ts (vitest; modules take injected deps, no top-level vscode import)
    schemas/      ticket.schema.json, board-meta.schema.json
    defaults/     KANBAN_GUIDE.md (generic board guide seeded into new workspaces)
    scripts/      fetch-python.mjs (downloads+stages runtime, copies app/static/config)
    bundled/      (build output, gitignored: python/, app/, static/, defaults/config/)
    dist/         (esbuild output, gitignored)
```

**Gotcha:** the repo `.gitignore` ignores `*.json` globally (only `config/*.json` is
negated). Negations for `extension/**` JSON files must be appended or every manifest and
schema is silently untracked.

## Risks

- **python-build-standalone asset naming** varies across release tags — the fetch script
  pins tag + version and the plan includes a URL-liveness verification step.
- **CrowdStrike/corp AV** on target laptops adds per-spawn overhead and may flag a
  Python runtime inside an extensions dir; sideload smoke test on a real corp machine
  is a required validation step.
- **Job Object CPU cap** (`cpu_limiter.py`) now applies to a child of the extension
  host. Nested job objects are supported on Win8+; the smoke checklist verifies the cap
  still applies and that dispatched agents still break away.
- **vsix size** (~40–70 MB with the runtime): acceptable for sideloading.
- **Orphaned server on hard extension-host death**: `deactivate()` never runs and the
  Python child survives. The orchestrator single-instance lock self-heals on dead PIDs
  (`orchestrator_core.py:738`) and the next activation binds a fresh port, so this
  degrades rather than wedges — but orphans accumulate until reboot. A PID-file
  kill-on-activate is a v1.1 candidate.
