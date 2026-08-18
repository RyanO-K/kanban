# Kanban VS Code Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the kanban board (Python server + web UI + embedded orchestrator) as a sideloadable VS Code extension that installs on a fresh Windows PC with no prerequisites beyond VS Code, popping up one-line install commands for the two host tools it cannot bundle (claude CLI, git).

**Architecture:** The extension supervises one child process — the existing `app/kanban_server.py` run on a bundled python-build-standalone runtime — and renders the existing web UI in a WebviewPanel iframe pointed at `127.0.0.1:<port>` via webview `portMapping`. Board/state data is split from code with a new `KANBAN_DATA_DIR` env override; the workspace's `.kanban/` folder is the data root. New TypeScript lives in `extension/` in this repo; the Python app ships as a build-time copy.

**Tech Stack:** TypeScript + esbuild + vitest (extension), Python 3 stdlib (app, unchanged runtime floor 3.7), python-build-standalone 3.12 (bundled runtime), `@vscode/vsce` (packaging).

**Spec:** `docs/superpowers/specs/2026-08-17-vscode-extension-design.md`

## Global Constraints

- All work happens in this repo (`.kanban/`), committed directly to `master` (repo convention: kanban files never branch).
- Python app changes must be backward-compatible: with no `KANBAN_DATA_DIR`/`KANBAN_CLAUDE_PATH` env set, behavior is byte-identical to today. No new required third-party Python deps; keep 3.7-compatible syntax in `app/`.
- Run the full Python suite from the repo root with `python -m pytest tests -q` after every Python task; it must stay green (408 passing on Windows today).
- Extension TS modules that contain logic must not import `vscode` at module top level — take injected dependencies so vitest can test them headlessly. Only glue files (`extension.ts`, `panel.ts`, `attention.ts` wiring) touch the `vscode` API directly.
- The repo `.gitignore` ignores `*.json` globally — every new tracked `.json` under `extension/` needs a negation rule (added in Task 4). After any task that creates JSON files, verify with `git status` that they are actually staged.
- v1 targets `win32-x64` only; keep platform switches (`process.platform`, runtime triples) explicit so darwin/linux are additive later.
- The Docker inner argv (`orchestrator.py:1203/1206`) must keep the bare `"claude"` literal — it executes inside the container.
- All shell commands below run from the repo root `C:\Users\AE04581\Documents\GitHub\.kanban` unless stated otherwise; `npm`/`node` commands run in `extension/`.

---

## Part A — Python app changes (landable independently of the extension)

### Task 1: `KANBAN_DATA_DIR` code/data split

**Files:**
- Modify: `app/kanban_server.py:90-97` (KANBAN_DIR/STATIC_DIR block)
- Modify: `app/orchestrator_core.py:14` (KANBAN_DIR constant)
- Test: `tests/test_data_dir.py` (new)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: env contract `KANBAN_DATA_DIR=<abs-or-rel path>` honored by both modules at import time; helper `_resolve_data_dir() -> str` in each module; `kanban_server.STATIC_DIR` (and `HTML_PATH`/`CSS_PATH`/`JS_PATH`/`LAYRR_WIDGET_PATH`) remain code-relative. Task 8's server supervisor sets this env var.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_data_dir.py`:

```python
"""KANBAN_DATA_DIR splits the board/state root from the app-code location."""
import importlib
import os

import orchestrator_core
import kanban_server


def _reload_with_env(monkeypatch, module, value):
    if value is None:
        monkeypatch.delenv("KANBAN_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("KANBAN_DATA_DIR", value)
    return importlib.reload(module)


def test_resolve_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("KANBAN_DATA_DIR", str(tmp_path))
    assert kanban_server._resolve_data_dir() == str(tmp_path)
    assert orchestrator_core._resolve_data_dir() == str(tmp_path)


def test_resolve_data_dir_default_is_app_parent(monkeypatch):
    monkeypatch.delenv("KANBAN_DATA_DIR", raising=False)
    expected = os.path.dirname(os.path.dirname(
        os.path.abspath(kanban_server.__file__)))
    assert kanban_server._resolve_data_dir() == expected
    assert orchestrator_core._resolve_data_dir() == expected


def test_resolve_data_dir_blank_env_falls_back(monkeypatch):
    monkeypatch.setenv("KANBAN_DATA_DIR", "   ")
    assert kanban_server._resolve_data_dir() == kanban_server._APP_PARENT


def test_boards_root_follows_data_dir(monkeypatch, tmp_path):
    try:
        ks = _reload_with_env(monkeypatch, kanban_server, str(tmp_path))
        assert ks.KANBAN_DIR == str(tmp_path)
        assert ks.boards_root() == os.path.join(str(tmp_path), "boards")
        # Static assets must NOT move with the data dir override.
        assert not ks.STATIC_DIR.startswith(str(tmp_path))
        assert ks.STATIC_DIR == os.path.join(ks._APP_PARENT, "static")
    finally:
        _reload_with_env(monkeypatch, kanban_server, None)


def test_orchestrator_core_kanban_dir_follows_data_dir(monkeypatch, tmp_path):
    try:
        oc = _reload_with_env(monkeypatch, orchestrator_core, str(tmp_path))
        assert oc.KANBAN_DIR == str(tmp_path)
    finally:
        _reload_with_env(monkeypatch, orchestrator_core, None)
```

The reload-in-`try/finally` pattern restores module state for the rest of the suite; keep it exactly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_data_dir.py -v`
Expected: FAIL — `AttributeError: module 'kanban_server' has no attribute '_resolve_data_dir'`

- [ ] **Step 3: Implement in `kanban_server.py`**

Replace lines 90-97 (`# This module lives in .kanban/app/...` through `LAYRR_WIDGET_PATH = ...`) with:

```python
# This module lives in .kanban/app/ (or a bundled copy inside the VS Code
# extension). Static assets are resolved relative to the CODE; the board/state
# root (boards/, config/, _orchestrator/) defaults to the code's parent but can
# be pointed elsewhere with KANBAN_DATA_DIR — the extension sets it to the
# workspace's .kanban folder so bundled code and workspace data stay separate.
_APP_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_data_dir():
    """Board/state root: KANBAN_DATA_DIR env var, else the app's parent dir."""
    override = os.environ.get("KANBAN_DATA_DIR", "").strip()
    return os.path.abspath(override) if override else _APP_PARENT


KANBAN_DIR = _resolve_data_dir()
STATIC_DIR = os.path.join(_APP_PARENT, "static")
HTML_PATH = os.path.join(STATIC_DIR, "kanban.html")
CSS_PATH = os.path.join(STATIC_DIR, "kanban.css")
JS_PATH = os.path.join(STATIC_DIR, "kanban.js")
# Ticket widget injected into layrr-proxied pages (see layrr_launcher.py).
LAYRR_WIDGET_PATH = os.path.join(STATIC_DIR, "layrr-widget.js")
```

- [ ] **Step 4: Implement in `orchestrator_core.py`**

Replace line 14 (`KANBAN_DIR = os.path.dirname(...)`) with:

```python
_APP_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_data_dir():
    """Board/state root: KANBAN_DATA_DIR env var, else the app's parent dir."""
    override = os.environ.get("KANBAN_DATA_DIR", "").strip()
    return os.path.abspath(override) if override else _APP_PARENT


KANBAN_DIR = _resolve_data_dir()
```

- [ ] **Step 5: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_data_dir.py -v` — Expected: 5 PASS.
Run: `python -m pytest tests -q` — Expected: green (same count as before + 5). If anything newly fails, it is a test that assumed `KANBAN_DIR` and `STATIC_DIR` share a root — fix the production code, not the test, unless the test itself hardcoded that assumption.

- [ ] **Step 6: Grep for missed same-root assumptions**

Run: `grep -n "KANBAN_DIR" app/*.py | grep -v "_APP_PARENT\|_resolve_data_dir\|boards_root\|KANBAN_DATA_DIR"` and review each hit: every use should want the DATA dir (boards, config, `_orchestrator`, docs scanning). Any hit that actually wants code-relative assets must switch to `_APP_PARENT`. (Known-correct data uses: `boards_root()`, `config/`, `_orchestrator/server.json`, docs/specs scanning.)

- [ ] **Step 7: Commit**

```bash
git add app/kanban_server.py app/orchestrator_core.py tests/test_data_dir.py
git commit -m "app: split code and data roots via KANBAN_DATA_DIR env override"
```

---

### Task 2: `KANBAN_CLAUDE_PATH` override + route host-side `claude` call sites through `_claude_cmd()`

**Files:**
- Modify: `app/orchestrator.py:1085-1089` (`_claude_cmd`), `:1101` (probe), `:1650` (summarizer), `:2087` (model-assign), `:2114` (triage)
- Test: `tests/test_claude_cmd.py` (new)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: env contract `KANBAN_CLAUDE_PATH=<abs path to claude executable>`; `_claude_cmd() -> str` with precedence env > `shutil.which("claude")` > `"claude"`. Task 8's supervisor sets the env var from the `kanban.claudePath` setting.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_claude_cmd.py`:

```python
"""_claude_cmd resolution and its use by every HOST-side claude invocation."""
import orchestrator


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAUDE_PATH", r"C:\tools\claude\claude.exe")
    monkeypatch.setattr(orchestrator.shutil, "which", lambda n: r"D:\other\claude.exe")
    assert orchestrator._claude_cmd() == r"C:\tools\claude\claude.exe"


def test_path_lookup_fallback(monkeypatch):
    monkeypatch.delenv("KANBAN_CLAUDE_PATH", raising=False)
    monkeypatch.setattr(orchestrator.shutil, "which", lambda n: r"D:\bin\claude.exe")
    assert orchestrator._claude_cmd() == r"D:\bin\claude.exe"


def test_bare_name_last_resort(monkeypatch):
    monkeypatch.delenv("KANBAN_CLAUDE_PATH", raising=False)
    monkeypatch.setattr(orchestrator.shutil, "which", lambda n: None)
    assert orchestrator._claude_cmd() == "claude"


def _capture_run_tracked(monkeypatch):
    seen = {}

    def fake_run(cmd, label, **kw):
        seen["argv0"] = cmd[0]

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(orchestrator, "_run_tracked", fake_run)
    return seen


def test_probe_uses_resolved_claude(monkeypatch):
    monkeypatch.setenv("KANBAN_CLAUDE_PATH", "CLAUDE_OVERRIDE")
    seen = _capture_run_tracked(monkeypatch)
    orchestrator._probe_fable_available()
    assert seen["argv0"] == "CLAUDE_OVERRIDE"
```

- [ ] **Step 2: Run tests to verify the new behavior fails**

Run: `python -m pytest tests/test_claude_cmd.py -v`
Expected: `test_env_override_wins` and `test_probe_uses_resolved_claude` FAIL (env var not honored; probe argv[0] is the literal `"claude"`); the two fallback tests may already pass.

- [ ] **Step 3: Implement**

Replace `_claude_cmd` (`orchestrator.py:1085-1089`) with:

```python
def _claude_cmd():
    """Resolve the `claude` executable to a full path. On Windows the CLI is
    often a .cmd/.exe shim that bare Popen can't launch, so prefer an explicit
    path from PATH; fall back to the bare name. KANBAN_CLAUDE_PATH (set by the
    VS Code extension from its kanban.claudePath setting) wins outright —
    GUI-launched hosts often lack the shell's PATH."""
    override = os.environ.get("KANBAN_CLAUDE_PATH", "").strip()
    if override:
        return override
    return shutil.which("claude") or "claude"
```

Then change the first element of the argv list from `"claude"` to `_claude_cmd()` at exactly these four host-side sites (leave `orchestrator.py:1203/1206` — the Docker in-container argv — untouched):

1. `orchestrator.py:1101` — `_probe_fable_available`: `[_claude_cmd(), "--model", oc.FABLE_MODEL, ...]`
2. `orchestrator.py:1650` — ticket summarizer: `[_claude_cmd(), "-p", prompt, "--model", model, ...]`
3. `orchestrator.py:2087` — model assignment: `[_claude_cmd(), "-p", prompt, "--model", model, ...]`
4. `orchestrator.py:2114` — triage: `[_claude_cmd(), "-p", full, "--model", model, ...]`

- [ ] **Step 4: Run tests, then the full suite**

Run: `python -m pytest tests/test_claude_cmd.py -v` — Expected: 4 PASS.
Run: `python -m pytest tests -q` — Expected: green. Watch for existing tests that assert dispatch argv `== "claude"`; if any exist, update them to assert `orchestrator._claude_cmd()` equality (behavior, not literal).

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator.py tests/test_claude_cmd.py
git commit -m "orchestrator: honor KANBAN_CLAUDE_PATH and resolve claude on every host-side call site"
```

---

### Task 3: Cross-platform claude process name in `perf_monitor.py`

**Files:**
- Modify: `app/perf_monitor.py:176` (name filter in `discover_sessions`)
- Test: `tests/test_perf_monitor.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `perf_monitor._is_claude_proc(name: str | None) -> bool` (pure helper).

- [ ] **Step 1: Write the failing test** — append to `tests/test_perf_monitor.py`:

```python
def test_is_claude_proc_matches_all_platform_names():
    import perf_monitor
    assert perf_monitor._is_claude_proc("claude.exe")
    assert perf_monitor._is_claude_proc("Claude.EXE")
    assert perf_monitor._is_claude_proc("claude")      # macOS/Linux process name
    assert not perf_monitor._is_claude_proc("clang")
    assert not perf_monitor._is_claude_proc(None)
    assert not perf_monitor._is_claude_proc("")
```

- [ ] **Step 2: Run it** — `python -m pytest tests/test_perf_monitor.py -v -k is_claude` — Expected: FAIL, no attribute `_is_claude_proc`.

- [ ] **Step 3: Implement** — in `app/perf_monitor.py`, add near the top (after imports):

```python
def _is_claude_proc(name):
    """True when *name* is the claude CLI process on any platform
    (claude.exe on Windows, claude on macOS/Linux)."""
    return (name or "").lower() in ("claude", "claude.exe")
```

and replace the filter at line 176 (`if name != "claude.exe": continue` — exact current text may differ slightly; find the single `"claude.exe"` comparison in `discover_sessions`) with:

```python
if not _is_claude_proc(name):
    continue
```

- [ ] **Step 4: Run the file's tests, then the suite** — `python -m pytest tests/test_perf_monitor.py -v` then `python -m pytest tests -q` — Expected: green.

- [ ] **Step 5: Commit**

```bash
git add app/perf_monitor.py tests/test_perf_monitor.py
git commit -m "perf_monitor: match the claude process name on all platforms"
```

---

## Part B — The extension

### Task 4: Extension scaffold (manifest, build, test harness, gitignore)

**Files:**
- Create: `extension/package.json`, `extension/tsconfig.json`, `extension/esbuild.mjs`, `extension/vitest.config.ts`, `extension/.vscodeignore`, `extension/README.md`, `extension/src/extension.ts`, `extension/test/smoke.test.ts`, `.vscode/launch.json`
- Modify: `.gitignore` (append negations)

**Interfaces:**
- Consumes: nothing.
- Produces: `npm run build` → `dist/extension.js`; `npm test` → vitest; command id `kanban.open`; settings `kanban.claudePath` (string), `kanban.port` (number, 0=auto), `kanban.appDir` (string, dev override), `kanban.attentionPollSeconds` (number). All later tasks add source under `extension/src/` and tests under `extension/test/`.

- [ ] **Step 1: Append to `.gitignore`** (order matters — negations must come after the `*.json` rule, i.e. at end of file):

```gitignore
# VS Code extension (ticket: vscode-extension plan) — un-ignore its tracked JSON
!extension/*.json
!extension/schemas/*.json
!.vscode/*.json
extension/node_modules/
extension/dist/
extension/bundled/
*.vsix
```

- [ ] **Step 2: Create `extension/package.json`:**

```json
{
  "name": "ai-kanban-board",
  "displayName": "AI Kanban Board",
  "description": "File-based kanban with autonomous Claude agent dispatch",
  "version": "0.1.0",
  "publisher": "barnum",
  "license": "UNLICENSED",
  "engines": { "vscode": "^1.90.0" },
  "categories": ["Other"],
  "extensionKind": ["workspace"],
  "main": "./dist/extension.js",
  "activationEvents": ["workspaceContains:.kanban"],
  "contributes": {
    "commands": [
      { "command": "kanban.open", "title": "Kanban: Open Board" },
      { "command": "kanban.restartServer", "title": "Kanban: Restart Server" },
      { "command": "kanban.checkDependencies", "title": "Kanban: Check Dependencies" },
      { "command": "kanban.resumeTicketSession", "title": "Kanban: Resume Ticket Session" }
    ],
    "configuration": {
      "title": "Kanban",
      "properties": {
        "kanban.claudePath": { "type": "string", "default": "", "description": "Absolute path to the claude CLI. Empty = resolve from PATH." },
        "kanban.port": { "type": "number", "default": 0, "description": "Server port. 0 = allocate a free port automatically." },
        "kanban.appDir": { "type": "string", "default": "", "description": "Developer override: run the kanban Python app from this directory instead of the bundled copy." },
        "kanban.attentionPollSeconds": { "type": "number", "default": 30, "description": "How often to poll for blocked tickets awaiting an answer." }
      }
    }
  },
  "scripts": {
    "build": "node esbuild.mjs",
    "watch": "node esbuild.mjs --watch",
    "test": "vitest run",
    "fetch-runtime": "node scripts/fetch-python.mjs",
    "vscode:prepublish": "npm run build && npm run fetch-runtime",
    "package": "vsce package --target win32-x64 --allow-missing-repository"
  },
  "devDependencies": {
    "@types/node": "^20.11.0",
    "@types/vscode": "^1.90.0",
    "@vscode/vsce": "^3.1.0",
    "esbuild": "^0.23.0",
    "typescript": "^5.5.0",
    "vitest": "^2.0.0"
  }
}
```

- [ ] **Step 3: Create `extension/tsconfig.json`:**

```json
{
  "compilerOptions": {
    "module": "Node16",
    "moduleResolution": "Node16",
    "target": "ES2022",
    "lib": ["ES2022"],
    "outDir": "dist",
    "strict": true,
    "sourceMap": true,
    "skipLibCheck": true,
    "types": ["node"]
  },
  "include": ["src", "test"]
}
```

- [ ] **Step 4: Create `extension/esbuild.mjs`:**

```js
import esbuild from "esbuild";

const watch = process.argv.includes("--watch");
const ctx = await esbuild.context({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outfile: "dist/extension.js",
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  target: "node18",
  sourcemap: true,
});
if (watch) {
  await ctx.watch();
  console.log("esbuild watching…");
} else {
  await ctx.rebuild();
  await ctx.dispose();
}
```

- [ ] **Step 5: Create `extension/vitest.config.ts`:**

```ts
import { defineConfig } from "vitest/config";
export default defineConfig({
  test: { include: ["test/**/*.test.ts"], environment: "node" },
});
```

- [ ] **Step 6: Create `extension/.vscodeignore`:**

```
src/**
test/**
node_modules/**
scripts/**
defaults/**
esbuild.mjs
vitest.config.ts
tsconfig.json
**/__pycache__/**
**/*.map
```

- [ ] **Step 7: Create minimal `extension/src/extension.ts`:**

```ts
import * as vscode from "vscode";

export function activate(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("kanban.open", () => {
      void vscode.window.showInformationMessage("Kanban: wiring in progress");
    })
  );
}

export function deactivate(): void {}
```

- [ ] **Step 8: Create `extension/test/smoke.test.ts` and `extension/README.md`:**

```ts
import { describe, expect, it } from "vitest";

describe("harness", () => {
  it("runs", () => {
    expect(1 + 1).toBe(2);
  });
});
```

`extension/README.md` (vsce packages it as the extension page; without one `vsce package` warns interactively):

```markdown
# AI Kanban Board

File-based kanban board with autonomous Claude agent dispatch, a bundled Python
runtime, and the existing web UI in a webview. Sideloaded platform vsix; design
notes live in docs/superpowers/specs/2026-08-17-vscode-extension-design.md.
```

- [ ] **Step 9: Create `.vscode/launch.json`** (repo root — F5 opens an Extension Development Host on this workspace):

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Run Kanban Extension",
      "type": "extensionHost",
      "request": "launch",
      "args": [
        "--extensionDevelopmentPath=${workspaceFolder}/extension",
        "${workspaceFolder}/.."
      ]
    }
  ]
}
```

- [ ] **Step 10: Install, build, test**

Run (in `extension/`): `npm install`, then `npm run build`, then `npm test`
Expected: install succeeds; `dist/extension.js` exists; 1 test passes.

- [ ] **Step 11: Verify git actually tracks the JSON files**

Run: `git status --short --untracked-files=all` — the `--untracked-files=all` flag is REQUIRED: the default collapses a wholly-untracked directory to a single `?? extension/` line, which proves nothing about the JSON files inside. `extension/package.json`, `extension/tsconfig.json`, `.vscode/launch.json` must each be listed individually. Cross-check with `git check-ignore -v extension/package.json extension/tsconfig.json .vscode/launch.json` — it must print NOTHING (any output names the `.gitignore` rule still swallowing that file; fix before committing). Nothing under `extension/node_modules/` or `extension/dist/` may appear.

- [ ] **Step 12: Commit**

```bash
git add .gitignore extension/ .vscode/launch.json
git commit -m "extension: scaffold VS Code extension (esbuild + vitest + manifest)"
```

---

### Task 5: Free-port allocation and bundled-runtime resolution

**Files:**
- Create: `extension/src/ports.ts`, `extension/src/runtime.ts`
- Test: `extension/test/ports.test.ts`, `extension/test/runtime.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `findFreePort(): Promise<number>`; `pythonExePath(extensionRoot: string, platform?: NodeJS.Platform): string`; `findPython(extensionRoot: string, exists?: (p: string) => boolean, platform?: NodeJS.Platform): string | null`. Used by Task 8 (server) and Task 10 (wiring).

- [ ] **Step 1: Write failing tests**

`extension/test/ports.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import * as net from "node:net";
import { findFreePort } from "../src/ports";

describe("findFreePort", () => {
  it("returns a bindable loopback port", async () => {
    const port = await findFreePort();
    expect(port).toBeGreaterThan(0);
    await new Promise<void>((resolve, reject) => {
      const srv = net.createServer();
      srv.once("error", reject);
      srv.listen(port, "127.0.0.1", () => srv.close(() => resolve()));
    });
  });
});
```

`extension/test/runtime.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { findPython, pythonExePath } from "../src/runtime";

describe("pythonExePath", () => {
  // Expectations are literal strings on purpose: computing them with the host's
  // path.join would hide a separator bug (host-joined posix paths come out as
  // backslashes on Windows and the test would "agree" with the broken output).
  it("resolves the win32 layout with win32 separators", () => {
    expect(pythonExePath("C:\\ext", "win32")).toBe(
      "C:\\ext\\bundled\\python\\python.exe"
    );
  });
  it("resolves the posix layout with posix separators on any host", () => {
    expect(pythonExePath("/ext", "linux")).toBe("/ext/bundled/python/bin/python3");
  });
});

describe("findPython", () => {
  it("returns the bundled exe when present", () => {
    const exe = pythonExePath("/ext", "win32");
    expect(findPython("/ext", (p) => p === exe, "win32")).toBe(exe);
  });
  it("returns null when absent", () => {
    expect(findPython("/ext", () => false, "win32")).toBeNull();
  });
});
```

- [ ] **Step 2: Run** — `npm test` — Expected: FAIL (modules missing).

- [ ] **Step 3: Implement `extension/src/ports.ts`:**

```ts
import * as net from "node:net";

/** Ask the OS for a free loopback TCP port (bind port 0, read it back). */
export function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address() as net.AddressInfo;
      srv.close(() => resolve(addr.port));
    });
  });
}
```

- [ ] **Step 4: Implement `extension/src/runtime.ts`:**

```ts
import * as fs from "node:fs";
import * as path from "node:path";

/** Location of the bundled python-build-standalone interpreter.
 *  install_only tarballs extract to python/python.exe (win) or python/bin/python3 (posix).
 *  Joined with the TARGET platform's separators (path.win32/path.posix), not the
 *  build host's, so darwin/linux stay correct when those targets are added. */
export function pythonExePath(
  extensionRoot: string,
  platform: NodeJS.Platform = process.platform
): string {
  const p = platform === "win32" ? path.win32 : path.posix;
  return platform === "win32"
    ? p.join(extensionRoot, "bundled", "python", "python.exe")
    : p.join(extensionRoot, "bundled", "python", "bin", "python3");
}

export function findPython(
  extensionRoot: string,
  exists: (p: string) => boolean = fs.existsSync,
  platform: NodeJS.Platform = process.platform
): string | null {
  const bundled = pythonExePath(extensionRoot, platform);
  return exists(bundled) ? bundled : null;
}
```

- [ ] **Step 5: Run** — `npm test` — Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add extension/src/ports.ts extension/src/runtime.ts extension/test/ports.test.ts extension/test/runtime.test.ts
git commit -m "extension: free-port allocation and bundled-python resolution"
```

---

### Task 6: Build-time staging — fetch Python runtime, copy app/static/defaults

**Files:**
- Create: `extension/scripts/fetch-python.mjs`

**Interfaces:**
- Consumes: repo layout (`app/`, `static/`, `config/`).
- Produces: `extension/bundled/python/` (runtime with psutil installed), `extension/bundled/app/`, `extension/bundled/static/`, `extension/bundled/defaults/config/`, `extension/bundled/defaults/orchestrator_triage_prompt.md`. Task 7 reads `bundled/defaults/`, Task 13 packages `bundled/`.

- [ ] **Step 1: Create `extension/scripts/fetch-python.mjs`:**

```js
// Stages everything the vsix bundles: the standalone Python runtime (with
// psutil for the Performance tab) plus a copy of the kanban app, static UI,
// and default config profiles from the repo root (two levels up).
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const EXT_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REPO_ROOT = path.dirname(EXT_ROOT);
const BUNDLED = path.join(EXT_ROOT, "bundled");

// Pin tag + version; bump deliberately. Asset naming varies across release
// tags — if the download 404s, list the tag's assets and adjust PBS_ASSET.
const PBS_TAG = "20241219";
const PY_VER = "3.12.8";
const TRIPLES = { "win32-x64": "x86_64-pc-windows-msvc" };
const target = process.argv[2] || "win32-x64";
const triple = TRIPLES[target];
if (!triple) throw new Error(`unknown target ${target}`);
const PBS_ASSET = `cpython-${PY_VER}+${PBS_TAG}-${triple}-install_only.tar.gz`;
const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}`;

function copyDir(src, dest, skip = /(__pycache__|\.pyc$)/) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (skip.test(entry.name)) continue;
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDir(s, d, skip);
    else fs.copyFileSync(s, d);
  }
}

// 1. Runtime (skipped when already staged — delete bundled/python to re-fetch).
const pyDir = path.join(BUNDLED, "python");
if (!fs.existsSync(pyDir)) {
  fs.mkdirSync(BUNDLED, { recursive: true });
  const tarball = path.join(BUNDLED, PBS_ASSET);
  console.log(`fetching ${url}`);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`download failed: ${res.status} ${url}`);
  fs.writeFileSync(tarball, Buffer.from(await res.arrayBuffer()));
  // bsdtar ships with Windows 10+; extracts the top-level python/ dir.
  execFileSync("tar", ["-xzf", tarball, "-C", BUNDLED], { stdio: "inherit" });
  fs.rmSync(tarball);
  const exe = target.startsWith("win32")
    ? path.join(pyDir, "python.exe")
    : path.join(pyDir, "bin", "python3");
  execFileSync(exe, ["-m", "pip", "install", "--no-warn-script-location", "psutil"], {
    stdio: "inherit",
  });
}

// 2. App + static + default config profiles (always refreshed).
for (const dir of ["app", "static"]) {
  fs.rmSync(path.join(BUNDLED, dir), { recursive: true, force: true });
  copyDir(path.join(REPO_ROOT, dir), path.join(BUNDLED, dir));
}
fs.rmSync(path.join(BUNDLED, "defaults"), { recursive: true, force: true });
copyDir(path.join(REPO_ROOT, "config"), path.join(BUNDLED, "defaults", "config"));
// The dispatch-triage prompt is read from the DATA dir (orchestrator.py
// `_triage_prompt`); a fresh workspace without it still dispatches (backfill
// fills the cap) but burns a triage model call per tick on a "dispatch
// nothing" fallback prompt and loses all prioritization/model selection.
fs.copyFileSync(
  path.join(REPO_ROOT, "orchestrator_triage_prompt.md"),
  path.join(BUNDLED, "defaults", "orchestrator_triage_prompt.md")
);
console.log("staged:", fs.readdirSync(BUNDLED).join(", "));
```

- [ ] **Step 2: Verify the pinned asset URL is live** (before running the full download):

Run: `curl -sIL -o /dev/null -w "%{http_code}" https://github.com/astral-sh/python-build-standalone/releases/download/20241219/cpython-3.12.8+20241219-x86_64-pc-windows-msvc-install_only.tar.gz`
Expected: `200`. If `404`: run `gh release view 20241219 --repo astral-sh/python-build-standalone --json assets -q '.assets[].name' | grep windows` (read-only `gh` is fine) and correct `PBS_ASSET`/`PY_VER` in the script to an existing `x86_64-pc-windows-msvc…install_only.tar.gz` asset name.

- [ ] **Step 3: Run the staging script**

Run (in `extension/`): `npm run fetch-runtime`
Expected: `bundled/` contains `python/`, `app/`, `static/`, `defaults/`.

- [ ] **Step 4: Verify the staged runtime runs the staged app**

Run: `extension\bundled\python\python.exe --version` — Expected: `Python 3.12.8`.
Run: `extension\bundled\python\python.exe -c "import psutil, sys; sys.path.insert(0, r'extension\bundled\app'); import kanban_server; print('ok', kanban_server.PORT)"` — Expected: `ok 8745`.

- [ ] **Step 5: Verify git ignores `bundled/`**

Run: `git status --short --untracked-files=all extension/` — Expected: `extension/scripts/fetch-python.mjs` only; nothing under `bundled/`.

- [ ] **Step 6: Commit**

```bash
git add extension/scripts/fetch-python.mjs
git commit -m "extension: build-time staging of python runtime, app copy, and default profiles"
```

---

### Task 7: Workspace data-dir bootstrap + bundled board guide

**Files:**
- Create: `extension/src/bootstrap.ts`, `extension/defaults/KANBAN_GUIDE.md`
- Test: `extension/test/bootstrap.test.ts`
- Modify: `extension/scripts/fetch-python.mjs` (copy `extension/defaults/KANBAN_GUIDE.md` into `bundled/defaults/`)

**Interfaces:**
- Consumes: `bundled/defaults/` layout from Task 6.
- Produces: `ensureDataDir(dataDir: string, defaultsDir: string): { created: string[] }` — idempotent; creates `boards/`, `_orchestrator/`, `skills/` (empty — the server's dispatch prompt tells agents to read this path, so it must exist), seeds `config/*.json`, `CLAUDE.md`, and `orchestrator_triage_prompt.md` only when absent. Task 10 calls it on activation.

- [ ] **Step 1: Write failing tests** — `extension/test/bootstrap.test.ts`:

```ts
import { describe, expect, it, beforeEach } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { ensureDataDir } from "../src/bootstrap";

function makeDefaults(root: string): string {
  const d = path.join(root, "defaults");
  fs.mkdirSync(path.join(d, "config"), { recursive: true });
  fs.writeFileSync(path.join(d, "config", "general.json"), "{}\n");
  fs.writeFileSync(path.join(d, "KANBAN_GUIDE.md"), "# guide\n");
  fs.writeFileSync(path.join(d, "orchestrator_triage_prompt.md"), "triage\n");
  return d;
}

describe("ensureDataDir", () => {
  let tmp: string;
  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "kanban-boot-"));
  });

  it("scaffolds a fresh data dir", () => {
    const dataDir = path.join(tmp, ".kanban");
    const result = ensureDataDir(dataDir, makeDefaults(tmp));
    expect(fs.existsSync(path.join(dataDir, "boards"))).toBe(true);
    expect(fs.existsSync(path.join(dataDir, "_orchestrator"))).toBe(true);
    expect(fs.existsSync(path.join(dataDir, "skills"))).toBe(true);
    expect(fs.existsSync(path.join(dataDir, "config", "general.json"))).toBe(true);
    expect(fs.existsSync(path.join(dataDir, "CLAUDE.md"))).toBe(true);
    expect(fs.existsSync(path.join(dataDir, "orchestrator_triage_prompt.md"))).toBe(true);
    expect(result.created.length).toBeGreaterThan(0);
  });

  it("never overwrites existing files", () => {
    const dataDir = path.join(tmp, ".kanban");
    fs.mkdirSync(path.join(dataDir, "config"), { recursive: true });
    fs.writeFileSync(path.join(dataDir, "config", "general.json"), '{"mine": true}\n');
    fs.writeFileSync(path.join(dataDir, "CLAUDE.md"), "customized\n");
    ensureDataDir(dataDir, makeDefaults(tmp));
    expect(fs.readFileSync(path.join(dataDir, "config", "general.json"), "utf8")).toContain("mine");
    expect(fs.readFileSync(path.join(dataDir, "CLAUDE.md"), "utf8")).toBe("customized\n");
  });

  it("is idempotent", () => {
    const dataDir = path.join(tmp, ".kanban");
    const defaults = makeDefaults(tmp);
    ensureDataDir(dataDir, defaults);
    const second = ensureDataDir(dataDir, defaults);
    expect(second.created).toEqual([]);
  });
});
```

- [ ] **Step 2: Run** — `npm test` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `extension/src/bootstrap.ts`:**

```ts
import * as fs from "node:fs";
import * as path from "node:path";

/** Idempotently scaffold the workspace .kanban data dir. Creates missing
 *  dirs/files only — never overwrites user data. Safe on a workspace that
 *  holds a full clone of the kanban repo (superset layout). */
export function ensureDataDir(
  dataDir: string,
  defaultsDir: string
): { created: string[] } {
  const created: string[] = [];
  const mkdir = (p: string) => {
    if (!fs.existsSync(p)) {
      fs.mkdirSync(p, { recursive: true });
      created.push(p);
    }
  };
  mkdir(dataDir);
  mkdir(path.join(dataDir, "boards"));
  mkdir(path.join(dataDir, "_orchestrator"));
  mkdir(path.join(dataDir, "config"));
  // The server's dispatch prompt tells every agent to read .kanban/skills/ —
  // the directory must exist even though no generic skills ship in v1.
  mkdir(path.join(dataDir, "skills"));

  const defaultConfig = path.join(defaultsDir, "config");
  if (fs.existsSync(defaultConfig)) {
    for (const f of fs.readdirSync(defaultConfig)) {
      const dest = path.join(dataDir, "config", f);
      if (!fs.existsSync(dest)) {
        fs.copyFileSync(path.join(defaultConfig, f), dest);
        created.push(dest);
      }
    }
  }
  const seedFile = (srcName: string, destName: string) => {
    const src = path.join(defaultsDir, srcName);
    const dest = path.join(dataDir, destName);
    if (fs.existsSync(src) && !fs.existsSync(dest)) {
      fs.copyFileSync(src, dest);
      created.push(dest);
    }
  };
  seedFile("KANBAN_GUIDE.md", "CLAUDE.md");
  // Dispatch-triage prompt: read from the DATA dir by orchestrator.py; without
  // it every tick wastes a triage call on a "dispatch nothing" fallback prompt.
  seedFile("orchestrator_triage_prompt.md", "orchestrator_triage_prompt.md");
  return { created };
}
```

- [ ] **Step 4: Author `extension/defaults/KANBAN_GUIDE.md`** — start from the repo's `CLAUDE.md` and make it generic: keep the layout/ticket-shape/session-tracking/git-workflow/orchestrator sections verbatim; DELETE the Barnum-specific parts (the `create-promotion-prs` skill listing and any org-named examples). This file is the board guide agents read in a fresh workspace, so it must stand alone. Word the skills section for an empty dir ("add reusable skills under `.kanban/skills/<name>/SKILL.md`" — `skills/` is created empty; no skills ship in v1).

- [ ] **Step 5: Wire into staging** — in `extension/scripts/fetch-python.mjs`, after the `config` copy, add:

```js
fs.copyFileSync(
  path.join(EXT_ROOT, "defaults", "KANBAN_GUIDE.md"),
  path.join(BUNDLED, "defaults", "KANBAN_GUIDE.md")
);
```

- [ ] **Step 6: Run** — `npm test` (all PASS) and `npm run fetch-runtime` (guide appears in `bundled/defaults/`).

- [ ] **Step 7: Commit**

```bash
git add extension/src/bootstrap.ts extension/test/bootstrap.test.ts extension/defaults/KANBAN_GUIDE.md extension/scripts/fetch-python.mjs
git commit -m "extension: idempotent workspace data-dir bootstrap with bundled defaults"
```

---

### Task 8: Server supervisor

**Files:**
- Create: `extension/src/server.ts`
- Test: `extension/test/server.test.ts`

**Interfaces:**
- Consumes: `findFreePort` (Task 5) is used by the CALLER (Task 10) — this class takes a concrete port. Env contracts from Tasks 1–2 (`KANBAN_DATA_DIR`, `KANBAN_CLAUDE_PATH`) plus existing `KANBAN_PORT`, `KANBAN_TOKEN`.
- Produces:

```ts
interface ServerOptions {
  pythonExe: string;   // from findPython()
  appDir: string;      // bundled/app or kanban.appDir override
  dataDir: string;     // <workspace>/.kanban
  workspaceRoot: string;
  port: number;
  token: string;       // random per-activation, passed as KANBAN_TOKEN
  claudePath?: string; // kanban.claudePath setting, "" = unset
  log: (msg: string) => void;
  onCrash: (info: { code: number | null; restarting: boolean }) => void;
  spawnFn?: typeof import("node:child_process").spawn;  // test injection
  fetchFn?: typeof fetch;                               // test injection
}
class KanbanServer {
  constructor(opts: ServerOptions);
  start(health?: { timeoutMs?: number; intervalMs?: number }): Promise<void>;
                             // spawn + waitHealthy; rejects on timeout/dispose
  baseUrl(): string;         // http://127.0.0.1:<port>
  dispose(): void;           // kill child, stop restarts
}
```

- [ ] **Step 1: Write failing tests** — `extension/test/server.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import { KanbanServer, ServerOptions } from "../src/server";

class FakeChild extends EventEmitter {
  killed = false;
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  kill(): boolean {
    this.killed = true;
    this.emit("exit", null, "SIGTERM");
    return true;
  }
}

function makeServer(overrides: Partial<ServerOptions> = {}) {
  const child = new FakeChild();
  const spawnFn = vi.fn(() => child as never);
  const fetchFn = vi.fn(async () => ({ ok: true }) as Response);
  const opts: ServerOptions = {
    pythonExe: "PY", appDir: "APP", dataDir: "DATA", workspaceRoot: "ROOT",
    port: 9999, token: "TOK", claudePath: "CLAUDE",
    log: () => {}, onCrash: () => {},
    spawnFn: spawnFn as never, fetchFn: fetchFn as never,
    ...overrides,
  };
  return { server: new KanbanServer(opts), child, spawnFn, fetchFn };
}

describe("KanbanServer", () => {
  it("spawns python with the app entrypoint and contract env", async () => {
    const { server, spawnFn } = makeServer();
    await server.start();
    const [exe, args, spawnOpts] = spawnFn.mock.calls[0] as unknown as [
      string, string[], { env: Record<string, string>; cwd: string }
    ];
    expect(exe).toBe("PY");
    expect(args[0]).toContain("kanban_server.py");
    expect(spawnOpts.env.KANBAN_PORT).toBe("9999");
    expect(spawnOpts.env.KANBAN_DATA_DIR).toBe("DATA");
    expect(spawnOpts.env.KANBAN_TOKEN).toBe("TOK");
    expect(spawnOpts.env.KANBAN_CLAUDE_PATH).toBe("CLAUDE");
    expect(spawnOpts.cwd).toBe("ROOT");
  });

  it("start() resolves once the health endpoint answers", async () => {
    const { server, fetchFn } = makeServer();
    await server.start();
    expect(fetchFn).toHaveBeenCalledWith("http://127.0.0.1:9999/api/files");
  });

  it("start() rejects when health never answers", async () => {
    const fetchFn = vi.fn(async () => { throw new Error("refused"); });
    const { server } = makeServer({ fetchFn: fetchFn as never });
    await expect(server.start({ timeoutMs: 300, intervalMs: 50 })).rejects.toThrow(/health/i);
  });

  it("dispose() kills the child and suppresses crash handling", () => {
    const onCrash = vi.fn();
    const { server, child } = makeServer({ onCrash });
    void server.start();
    server.dispose();
    expect(child.killed).toBe(true);
    expect(onCrash).not.toHaveBeenCalled();
  });

  it("crash with a dead port fires onCrash then respawns", async () => {
    vi.useFakeTimers();
    const onCrash = vi.fn();
    let portAlive = true;
    const fetchFn = vi.fn(async () => {
      if (!portAlive) throw new Error("refused");
      return { ok: true } as Response;
    });
    const { server, child, spawnFn } = makeServer({ onCrash, fetchFn: fetchFn as never });
    await server.start();
    portAlive = false;                       // the port died with the process
    child.emit("exit", 1, null);
    await vi.advanceTimersByTimeAsync(1500); // adoption probe fails → crash path → 0ms respawn
    expect(onCrash).toHaveBeenCalledWith({ code: 1, restarting: true });
    expect(spawnFn.mock.calls.length).toBeGreaterThan(1);
    vi.useRealTimers();
  });

  it("adopts a self-restarted server (UI restart button) instead of double-spawning", async () => {
    vi.useFakeTimers();
    const onCrash = vi.fn();
    const { server, child, spawnFn } = makeServer({ onCrash }); // fetch stays ok: replacement owns the port
    await server.start();
    child.emit("exit", 0, null);             // kanban_server server_restart(): spawn copy, exit 0
    await vi.advanceTimersByTimeAsync(1500);
    expect(onCrash).not.toHaveBeenCalled();
    expect(spawnFn).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
```

- [ ] **Step 2: Run** — `npm test` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `extension/src/server.ts`:**

```ts
import { spawn as realSpawn, ChildProcess } from "node:child_process";
import * as path from "node:path";

export interface ServerOptions {
  pythonExe: string;
  appDir: string;
  dataDir: string;
  workspaceRoot: string;
  port: number;
  token: string;
  claudePath?: string;
  log: (msg: string) => void;
  onCrash: (info: { code: number | null; restarting: boolean }) => void;
  spawnFn?: typeof realSpawn;
  fetchFn?: typeof fetch;
}

const MAX_RESTARTS = 3;      // within RESTART_WINDOW_MS, then give up
const RESTART_WINDOW_MS = 60_000;

export class KanbanServer {
  private child: ChildProcess | null = null;
  private disposed = false;
  private restartTimes: number[] = [];

  constructor(private readonly opts: ServerOptions) {}

  baseUrl(): string {
    return `http://127.0.0.1:${this.opts.port}`;
  }

  async start(health?: { timeoutMs?: number; intervalMs?: number }): Promise<void> {
    this.spawnChild();
    await this.waitHealthy(health?.timeoutMs ?? 20_000, health?.intervalMs ?? 250);
  }

  private spawnChild(): void {
    const spawnFn = this.opts.spawnFn ?? realSpawn;
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      KANBAN_PORT: String(this.opts.port),
      KANBAN_DATA_DIR: this.opts.dataDir,
      KANBAN_TOKEN: this.opts.token,
    };
    if (this.opts.claudePath) env.KANBAN_CLAUDE_PATH = this.opts.claudePath;
    this.child = spawnFn(
      this.opts.pythonExe,
      [path.join(this.opts.appDir, "kanban_server.py")],
      { env, cwd: this.opts.workspaceRoot, stdio: ["ignore", "pipe", "pipe"] }
    );
    this.child.stdout?.on("data", (d) => this.opts.log(String(d).trimEnd()));
    this.child.stderr?.on("data", (d) => this.opts.log(String(d).trimEnd()));
    this.child.on("exit", (code) => this.handleExit(code));
  }

  private handleExit(code: number | null): void {
    if (this.disposed) return;
    // The server restarts ITSELF on the UI's "Restart server" button: on
    // Windows it spawns a replacement on the same port and exits 0
    // (kanban_server.py:1282-1284, server_restart). Respawning here would put
    // TWO servers on one port, so probe the port first and adopt a live
    // replacement instead.
    setTimeout(() => void this.respawnUnlessReplaced(code), 1000);
  }

  private async respawnUnlessReplaced(code: number | null): Promise<void> {
    if (this.disposed) return;
    const fetchFn = this.opts.fetchFn ?? fetch;
    try {
      const res = await fetchFn(`${this.baseUrl()}/api/files`);
      if (res.ok) {
        // Known v1 limitation: the adopted replacement is a process we hold no
        // handle to, so dispose() cannot kill it (it outlives deactivate()).
        this.opts.log("server restarted itself; adopted the replacement on the same port");
        this.child = null;
        return;
      }
    } catch {
      /* port is dead: a real crash — fall through to respawn */
    }
    if (this.disposed) return;
    const now = Date.now();
    this.restartTimes = this.restartTimes.filter((t) => now - t < RESTART_WINDOW_MS);
    const restarting = this.restartTimes.length < MAX_RESTARTS;
    this.opts.onCrash({ code, restarting });
    if (restarting) {
      // Delay scales with recent crash count: first respawn is immediate.
      const delay = 500 * this.restartTimes.length;
      this.restartTimes.push(now);
      setTimeout(() => {
        if (!this.disposed) this.spawnChild();
      }, delay);
    }
  }

  private async waitHealthy(timeoutMs: number, intervalMs: number): Promise<void> {
    const fetchFn = this.opts.fetchFn ?? fetch;
    const deadline = Date.now() + timeoutMs;
    let lastErr: unknown = null;
    while (Date.now() < deadline) {
      if (this.disposed) throw new Error("kanban server disposed during startup");
      try {
        const res = await fetchFn(`${this.baseUrl()}/api/files`);
        if (res.ok) return;
      } catch (err) {
        lastErr = err;
      }
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    throw new Error(`kanban server failed health check on ${this.baseUrl()}: ${lastErr}`);
  }

  dispose(): void {
    this.disposed = true;
    this.child?.kill();
    this.child = null;
  }
}
```

Note: dispatched claude agents deliberately survive this kill — they are spawned detached by the orchestrator and re-adopted via ticket markers on next start (existing behavior).

- [ ] **Step 4: Run** — `npm test` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add extension/src/server.ts extension/test/server.test.ts
git commit -m "extension: kanban server supervisor (spawn, health check, crash restart, dispose)"
```

---

### Task 9: Dependency checks with one-line install popups

**Files:**
- Create: `extension/src/deps.ts`
- Test: `extension/test/deps.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:

```ts
interface DepCheck { id: "claude" | "git"; label: string; probe: string[];
                     install: Partial<Record<NodeJS.Platform, string>>; }
const DEPS: DepCheck[];
function installCommandFor(dep: DepCheck, platform?: NodeJS.Platform): string | undefined;
function checkDep(dep: DepCheck, runner?: ProbeRunner): Promise<boolean>;
type ProbeRunner = (cmd: string, args: string[]) => Promise<{ ok: boolean }>;
```

Task 10 wires the popup UI (`showWarningMessage` + terminal) around these.

- [ ] **Step 1: Write failing tests** — `extension/test/deps.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { DEPS, checkDep, installCommandFor } from "../src/deps";

describe("DEPS", () => {
  it("covers claude and git with per-platform one-liners", () => {
    const ids = DEPS.map((d) => d.id).sort();
    expect(ids).toEqual(["claude", "git"]);
    const claude = DEPS.find((d) => d.id === "claude")!;
    expect(installCommandFor(claude, "win32")).toBe("irm https://claude.ai/install.ps1 | iex");
    expect(installCommandFor(claude, "linux")).toContain("curl -fsSL https://claude.ai/install.sh");
    const git = DEPS.find((d) => d.id === "git")!;
    expect(installCommandFor(git, "win32")).toContain("winget install --id Git.Git");
  });
});

describe("checkDep", () => {
  const claude = DEPS.find((d) => d.id === "claude")!;
  it("passes when the probe exits ok", async () => {
    expect(await checkDep(claude, async () => ({ ok: true }))).toBe(true);
  });
  it("fails when the probe errors", async () => {
    expect(await checkDep(claude, async () => { throw new Error("ENOENT"); })).toBe(false);
  });
});
```

- [ ] **Step 2: Run** — `npm test` — Expected: FAIL.

- [ ] **Step 3: Implement `extension/src/deps.ts`:**

```ts
import { execFile } from "node:child_process";

export interface DepCheck {
  id: "claude" | "git";
  label: string;
  probe: string[];
  install: Partial<Record<NodeJS.Platform, string>>;
}

// One-liners use native installers, NOT npm — a fresh PC has no Node.
export const DEPS: DepCheck[] = [
  {
    id: "claude",
    label: "Claude Code CLI",
    probe: ["claude", "--version"],
    install: {
      win32: "irm https://claude.ai/install.ps1 | iex",
      darwin: "curl -fsSL https://claude.ai/install.sh | bash",
      linux: "curl -fsSL https://claude.ai/install.sh | bash",
    },
  },
  {
    id: "git",
    label: "Git",
    probe: ["git", "--version"],
    install: {
      win32: "winget install --id Git.Git -e --source winget",
      darwin: "xcode-select --install",
      linux: "sudo apt-get install -y git",
    },
  },
];

export type ProbeRunner = (cmd: string, args: string[]) => Promise<{ ok: boolean }>;

const defaultRunner: ProbeRunner = (cmd, args) =>
  new Promise((resolve) => {
    execFile(cmd, args, { timeout: 10_000, shell: process.platform === "win32" },
      (err) => resolve({ ok: !err }));
  });

export function installCommandFor(
  dep: DepCheck,
  platform: NodeJS.Platform = process.platform
): string | undefined {
  return dep.install[platform];
}

export async function checkDep(
  dep: DepCheck,
  runner: ProbeRunner = defaultRunner
): Promise<boolean> {
  try {
    return (await runner(dep.probe[0], dep.probe.slice(1))).ok;
  } catch {
    return false;
  }
}
```

(`shell: true` on Windows so `.cmd` shims like a npm-installed `claude` still probe correctly.)

- [ ] **Step 4: Run** — `npm test` — Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add extension/src/deps.ts extension/test/deps.test.ts
git commit -m "extension: claude/git dependency probes with per-platform install one-liners"
```

---

### Task 10: Webview panel + full activation wiring

**Files:**
- Create: `extension/src/panel.ts`
- Modify: `extension/src/extension.ts` (replace the Task 4 stub entirely)
- Test: `extension/test/panel.test.ts`

**Interfaces:**
- Consumes: `KanbanServer` (Task 8), `findPython` (Task 5), `findFreePort` (Task 5), `ensureDataDir` (Task 7), `DEPS`/`checkDep`/`installCommandFor` (Task 9).
- Produces: `boardHtml(webviewPort: number): string`; command `kanban.open` opens the panel; commands `kanban.restartServer`, `kanban.checkDependencies` work. Task 11 wires its poller INSIDE `startServer` (where the module-level `server` variable is in scope) — no cross-module accessor needed.

- [ ] **Step 1: Write failing test** — `extension/test/panel.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { WEBVIEW_PORT, boardHtml } from "../src/panel";

describe("boardHtml", () => {
  it("embeds a full-viewport iframe to the mapped loopback port", () => {
    const html = boardHtml(WEBVIEW_PORT);
    expect(html).toContain(`http://127.0.0.1:${WEBVIEW_PORT}/`);
    expect(html).toMatch(/<iframe/);
    // Port-wildcard frame-src: portMapping may redirect to the real port and
    // CSP re-evaluates frame-src against redirect targets.
    expect(html).toContain("frame-src http://127.0.0.1:* http://localhost:*");
    expect(html).toContain("height:100%");
  });
});
```

- [ ] **Step 2: Run** — `npm test` — Expected: FAIL.

- [ ] **Step 3: Implement `extension/src/panel.ts`:**

```ts
// The in-webview port is a fixed constant; webview portMapping rewrites it to
// the real (auto-allocated) server port. Keeping it constant means the iframe
// HTML never changes across activations. CSP note: the mapping can surface as
// a redirect to the REAL port, and CSP re-evaluates frame-src against redirect
// targets — so frame-src must allow ANY loopback port, not just 8745.
export const WEBVIEW_PORT = 8745;

export function boardHtml(webviewPort: number): string {
  const origin = `http://127.0.0.1:${webviewPort}`;
  return `<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; frame-src http://127.0.0.1:* http://localhost:*; style-src 'unsafe-inline'">
<style>html,body{height:100%;margin:0;padding:0;overflow:hidden}
iframe{width:100%;height:100%;border:0;display:block}</style>
</head>
<body>
<iframe src="${origin}/" allow="clipboard-read; clipboard-write"></iframe>
</body>
</html>`;
}
```

- [ ] **Step 4: Replace `extension/src/extension.ts`:**

```ts
import * as crypto from "node:crypto";
import * as path from "node:path";
import * as vscode from "vscode";
import { DEPS, checkDep, installCommandFor } from "./deps";
import { ensureDataDir } from "./bootstrap";
import { findFreePort } from "./ports";
import { findPython } from "./runtime";
import { KanbanServer } from "./server";
import { WEBVIEW_PORT, boardHtml } from "./panel";

let server: KanbanServer | null = null;
let panel: vscode.WebviewPanel | null = null;
let output: vscode.OutputChannel;

async function startServer(context: vscode.ExtensionContext): Promise<void> {
  if (process.platform !== "win32") {
    // extensionKind "workspace" runs this on the REMOTE host under
    // Remote-SSH/WSL, where the win32-x64 bundled runtime cannot exist. Fail
    // with an accurate message instead of the generic "runtime missing" one.
    void vscode.window.showErrorMessage(
      "Kanban: this build bundles a Windows-only Python runtime (win32-x64). " +
        "Remote/WSL/macOS/Linux need a matching platform build (not in v1)."
    );
    return;
  }
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    void vscode.window.showErrorMessage("Kanban: open a folder first.");
    return;
  }
  const cfg = vscode.workspace.getConfiguration("kanban");
  const workspaceRoot = folder.uri.fsPath;
  const dataDir = path.join(workspaceRoot, ".kanban");
  const appDir =
    cfg.get<string>("appDir") || path.join(context.extensionPath, "bundled", "app");
  const defaultsDir = path.join(context.extensionPath, "bundled", "defaults");
  ensureDataDir(dataDir, defaultsDir);

  const pythonExe = findPython(context.extensionPath);
  if (!pythonExe) {
    void vscode.window.showErrorMessage(
      "Kanban: bundled Python runtime missing — reinstall the extension " +
        "(or run `npm run fetch-runtime` in a dev checkout)."
    );
    return;
  }
  const port = cfg.get<number>("port") || (await findFreePort());
  server = new KanbanServer({
    pythonExe,
    appDir,
    dataDir,
    workspaceRoot,
    port,
    token: crypto.randomBytes(24).toString("base64url"),
    claudePath: cfg.get<string>("claudePath") || undefined,
    log: (m) => output.appendLine(m),
    onCrash: ({ code, restarting }) => {
      output.appendLine(`server exited (code ${code}); restarting=${restarting}`);
      if (!restarting) {
        void vscode.window
          .showErrorMessage("Kanban server crashed repeatedly.", "Show Log", "Restart")
          .then((pick) => {
            if (pick === "Show Log") output.show();
            if (pick === "Restart") void vscode.commands.executeCommand("kanban.restartServer");
          });
      }
    },
  });
  await server.start();
  output.appendLine(`kanban server healthy on ${server.baseUrl()}`);
}

async function checkDependencies(): Promise<void> {
  const claudePath = vscode.workspace.getConfiguration("kanban").get<string>("claudePath");
  for (const dep of DEPS) {
    // Honor kanban.claudePath: a user who set it precisely BECAUSE claude is
    // off PATH must not get a false "not found" popup on every activation.
    const effective =
      dep.id === "claude" && claudePath
        ? { ...dep, probe: [claudePath, ...dep.probe.slice(1)] }
        : dep;
    if (await checkDep(effective)) continue;
    const cmd = installCommandFor(dep);
    const detail = cmd ? ` Install with: ${cmd}` : "";
    void vscode.window
      .showWarningMessage(
        `Kanban: ${dep.label} not found — agent dispatch needs it.${detail}`,
        "Install in Terminal",
        "Copy Command"
      )
      .then((pick) => {
        if (!cmd) return;
        if (pick === "Install in Terminal") {
          // The win32 one-liners are PowerShell syntax (`irm … | iex`); the
          // user's default terminal profile may be cmd or Git Bash, where they
          // are syntax errors — pin the shell to PowerShell on Windows.
          const term = vscode.window.createTerminal(
            process.platform === "win32"
              ? { name: `Install ${dep.label}`, shellPath: "powershell.exe" }
              : { name: `Install ${dep.label}` }
          );
          term.show();
          term.sendText(cmd, false); // typed, not executed — user presses Enter
        } else if (pick === "Copy Command") {
          void vscode.env.clipboard.writeText(cmd);
        }
      });
  }
}

function openPanel(context: vscode.ExtensionContext): void {
  if (!server) {
    void vscode.window.showErrorMessage("Kanban: server is not running.");
    return;
  }
  if (panel) {
    panel.reveal();
    return;
  }
  const port = Number(new URL(server.baseUrl()).port);
  panel = vscode.window.createWebviewPanel(
    "kanbanBoard",
    "Kanban",
    vscode.ViewColumn.One,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      portMapping: [{ webviewPort: WEBVIEW_PORT, extensionHostPort: port }],
    }
  );
  panel.webview.html = boardHtml(WEBVIEW_PORT);
  panel.onDidDispose(() => (panel = null), null, context.subscriptions);
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output = vscode.window.createOutputChannel("Kanban");
  context.subscriptions.push(output);
  context.subscriptions.push(
    vscode.commands.registerCommand("kanban.open", () => openPanel(context)),
    vscode.commands.registerCommand("kanban.checkDependencies", () => checkDependencies()),
    vscode.commands.registerCommand("kanban.restartServer", async () => {
      server?.dispose();
      server = null;
      // A live panel's portMapping is frozen at creation and the restarted
      // server gets a fresh port — a surviving panel would point at the dead
      // port forever. Recreate the panel after restart if it was open.
      const hadPanel = panel !== null;
      panel?.dispose();
      panel = null;
      await startServer(context);
      if (hadPanel && server) openPanel(context);
    })
  );
  try {
    await startServer(context);
  } catch (err) {
    output.appendLine(String(err));
    void vscode.window.showErrorMessage(`Kanban server failed to start: ${err}`);
  }
  void checkDependencies();
}

export function deactivate(): void {
  server?.dispose();
  server = null;
}
```

- [ ] **Step 5: Run tests + build** — `npm test` (all PASS) then `npm run build` (clean compile).

- [ ] **Step 6: Manual smoke in the Extension Development Host** — press F5 in VS Code (launch config from Task 4; it opens the parent workspace `C:\Users\AE04581\Documents\GitHub`, which contains `.kanban`). Verify: the Kanban output channel shows "server healthy"; run "Kanban: Open Board"; the board UI renders inside the panel and existing boards are visible (data dir = the real `.kanban`, app = `kanban.appDir` unset → bundled copy). Drag a test ticket between columns to prove mutations work (token flows same-origin through the iframe). Then three known-risk checks: (1) click a spec/file link (`.sp-file-link`) in a ticket side panel — `kanban.js:911-919` opens `vscode://` URLs via `window.open`, a Simple-Browser workaround the nested sandboxed iframe may block; if blocked, add a postMessage bridge (iframe posts the href → webview script forwards via `acquireVsCodeApi().postMessage` → `panel.ts` handler calls `vscode.env.openExternal`) and fold it into this task. (2) Any clipboard-copy feature in the board — clipboard access in a nested cross-origin iframe may need more `allow` entries on the iframe. (3) In the Setup tab press "Restart server" — the output channel must log "adopted the replacement", the board must keep working, and there must NOT be a second respawn.

- [ ] **Step 7: Commit**

```bash
git add extension/src/panel.ts extension/src/extension.ts extension/test/panel.test.ts
git commit -m "extension: activation wiring, dependency popups, and iframe board panel with portMapping"
```

---

### Task 11: Status bar + blocked-question notifications

**Files:**
- Create: `extension/src/attention.ts`
- Modify: `extension/src/extension.ts` (wire poller into `activate`/`startServer`)
- Test: `extension/test/attention.test.ts`

**Interfaces:**
- Consumes: the module-local `server` variable in `extension.ts` (the poller is wired inside `startServer`, where it is in scope — Task 10 exports NO accessor); server API `GET /api/board/__all__` returning `{tasks: [...]}` where each task carries the server-stamped `_board` (NOT `board` — `kanban_server.py:598`), `id`, `title`, `status`, and optionally `orchestrator.question` (shape per `orchestrator.py:1554`: `prompt`, with `answer`/`answeredAt` null until a human answers). Shape verified against the source 2026-08-18; still run `curl http://127.0.0.1:8745/api/board/__all__` once before coding as a cheap sanity re-check.
- Produces:

```ts
interface AttentionSummary { inProgress: number; pendingQuestions: { key: string; title: string }[]; }
function summarize(payload: unknown): AttentionSummary;                       // pure
function newQuestions(prev: Set<string>, cur: AttentionSummary): { key: string; title: string }[]; // pure
class AttentionPoller { constructor(deps); start(): void; dispose(): void; }
```

- [ ] **Step 1: Write failing tests** — `extension/test/attention.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { newQuestions, summarize } from "../src/attention";

// Mirrors the REAL /api/board/__all__ shape: the server stamps `_board` on
// every task (kanban_server.py:598) and questions use `prompt` with a null
// `answer` until the human fills it (orchestrator.py:1554).
const payload = {
  tasks: [
    { id: "1", _board: "b", title: "run", status: "in_progress" },
    { id: "2", _board: "b", title: "ask", status: "blocked",
      orchestrator: { question: { type: "input", prompt: "which env?", answer: null } } },
    { id: "3", _board: "b", title: "answered", status: "blocked",
      orchestrator: { question: { type: "input", prompt: "which env?", answer: { value: "x" } } } },
    { id: "4", _board: "b", title: "done", status: "completed" },
  ],
};

describe("summarize", () => {
  it("counts in-progress and unanswered questions only", () => {
    const s = summarize(payload);
    expect(s.inProgress).toBe(1);
    expect(s.pendingQuestions).toEqual([{ key: "b/2", title: "ask" }]);
  });
  it("tolerates malformed payloads", () => {
    expect(summarize(null).inProgress).toBe(0);
    expect(summarize({}).pendingQuestions).toEqual([]);
  });
});

describe("newQuestions", () => {
  it("reports only unseen question keys", () => {
    const s = summarize(payload);
    expect(newQuestions(new Set(), s)).toHaveLength(1);
    expect(newQuestions(new Set(["b/2"]), s)).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Run** — `npm test` — Expected: FAIL.

- [ ] **Step 3: Implement `extension/src/attention.ts`:**

```ts
export interface AttentionSummary {
  inProgress: number;
  pendingQuestions: { key: string; title: string }[];
}

export function summarize(payload: unknown): AttentionSummary {
  const empty: AttentionSummary = { inProgress: 0, pendingQuestions: [] };
  if (!payload || typeof payload !== "object") return empty;
  const tasks = (payload as { tasks?: unknown }).tasks;
  if (!Array.isArray(tasks)) return empty;
  const out = { ...empty, pendingQuestions: [] as AttentionSummary["pendingQuestions"] };
  for (const t of tasks) {
    if (!t || typeof t !== "object") continue;
    const task = t as Record<string, any>;
    if (task.status === "in_progress") out.inProgress++;
    const q = task.orchestrator?.question;
    if (task.status === "blocked" && q && !q.answer) {
      out.pendingQuestions.push({
        // The server stamps `_board` on every task (kanban_server.py:598);
        // ticket ids are only unique per board, so a bare-id key would let
        // one board's question mask another's.
        key: `${task._board ?? task.board ?? "?"}/${task.id ?? "?"}`,
        title: String(task.title ?? ""),
      });
    }
  }
  return out;
}

export function newQuestions(
  prev: Set<string>,
  cur: AttentionSummary
): { key: string; title: string }[] {
  return cur.pendingQuestions.filter((q) => !prev.has(q.key));
}

export interface PollerDeps {
  fetchSummary: () => Promise<AttentionSummary>;
  intervalMs: number;
  onUpdate: (s: AttentionSummary) => void;
  onNewQuestion: (q: { key: string; title: string }) => void;
}

export class AttentionPoller {
  private timer: ReturnType<typeof setInterval> | null = null;
  private seen = new Set<string>();

  constructor(private readonly deps: PollerDeps) {}

  start(): void {
    const tick = async () => {
      try {
        const s = await this.deps.fetchSummary();
        for (const q of newQuestions(this.seen, s)) {
          this.seen.add(q.key);
          this.deps.onNewQuestion(q);
        }
        // Answered/cleared questions leave `seen` so a re-ask notifies again.
        const live = new Set(s.pendingQuestions.map((q) => q.key));
        this.seen = new Set([...this.seen].filter((k) => live.has(k)));
        this.deps.onUpdate(s);
      } catch {
        /* server briefly down: skip the tick */
      }
    };
    void tick();
    this.timer = setInterval(tick, this.deps.intervalMs);
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}
```

- [ ] **Step 4: Wire into `extension/src/extension.ts`** — add module state `let statusBar: vscode.StatusBarItem;` and `let poller: AttentionPoller | null = null;`, import `AttentionPoller, AttentionSummary, summarize` from `./attention`. In `activate`, after `output` creation:

```ts
statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
statusBar.command = "kanban.open";
statusBar.text = "$(project) Kanban";
statusBar.show();
context.subscriptions.push(statusBar);
```

At the end of `startServer` (after "server healthy"), start the poller:

```ts
const pollSeconds = cfg.get<number>("attentionPollSeconds") || 30;
poller?.dispose();
poller = new AttentionPoller({
  intervalMs: pollSeconds * 1000,
  fetchSummary: async () => {
    const res = await fetch(`${server!.baseUrl()}/api/board/__all__`);
    return summarize(await res.json());
  },
  onUpdate: (s: AttentionSummary) => {
    const q = s.pendingQuestions.length;
    statusBar.text = q > 0
      ? `$(warning) Kanban: ${q} question${q > 1 ? "s" : ""}`
      : `$(project) Kanban: ${s.inProgress} running`;
    statusBar.backgroundColor = q > 0
      ? new vscode.ThemeColor("statusBarItem.warningBackground")
      : undefined;
  },
  onNewQuestion: (q) => {
    void vscode.window
      .showWarningMessage(`Kanban ticket needs input: ${q.title}`, "Open Board")
      .then((pick) => {
        if (pick === "Open Board") void vscode.commands.executeCommand("kanban.open");
      });
  },
});
poller.start();
```

In `deactivate` (and in `kanban.restartServer` before restart): `poller?.dispose(); poller = null;`.

- [ ] **Step 5: Run** — `npm test` + `npm run build` — Expected: green. F5 smoke: status bar shows a running count; set a test ticket to `blocked` with an unanswered `orchestrator.question` in its JSON and confirm a notification fires within one poll interval.

- [ ] **Step 6: Commit**

```bash
git add extension/src/attention.ts extension/test/attention.test.ts extension/src/extension.ts
git commit -m "extension: status bar rollup and notifications for blocked-question tickets"
```

---

### Task 12: Resume-session command + ticket JSON schemas

**Files:**
- Create: `extension/src/resume.ts`, `extension/schemas/ticket.schema.json`, `extension/schemas/board-meta.schema.json`
- Modify: `extension/src/extension.ts` (register command), `extension/package.json` (add `jsonValidation` contribution)
- Test: `extension/test/resume.test.ts`

**Interfaces:**
- Consumes: data-dir layout (`<dataDir>/boards/<slug>/<id>.json` with `claudeSessionId` field).
- Produces: `listResumableTickets(dataDir: string): { board: string; id: string; title: string; sessionId: string }[]` (pure fs walk); command `kanban.resumeTicketSession`.

- [ ] **Step 1: Write failing test** — `extension/test/resume.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { listResumableTickets } from "../src/resume";

describe("listResumableTickets", () => {
  it("finds tickets carrying a claudeSessionId", () => {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "kanban-resume-"));
    const board = path.join(tmp, "boards", "demo");
    fs.mkdirSync(board, { recursive: true });
    fs.writeFileSync(path.join(board, "_meta.json"), JSON.stringify({ project: "demo" }));
    fs.writeFileSync(path.join(board, "1.json"),
      JSON.stringify({ id: "1", title: "with session", claudeSessionId: "abc-123" }));
    fs.writeFileSync(path.join(board, "2.json"),
      JSON.stringify({ id: "2", title: "no session" }));
    fs.writeFileSync(path.join(board, "notes.txt"), "ignore me");
    const found = listResumableTickets(tmp);
    expect(found).toEqual([
      { board: "demo", id: "1", title: "with session", sessionId: "abc-123" },
    ]);
  });

  it("returns empty for a missing boards dir", () => {
    expect(listResumableTickets(path.join(os.tmpdir(), "nope-xyz"))).toEqual([]);
  });
});
```

- [ ] **Step 2: Run** — `npm test` — Expected: FAIL.

- [ ] **Step 3: Implement `extension/src/resume.ts`:**

```ts
import * as fs from "node:fs";
import * as path from "node:path";

export interface ResumableTicket {
  board: string;
  id: string;
  title: string;
  sessionId: string;
}

/** Walk <dataDir>/boards/<slug>/<id>.json for tickets with a claudeSessionId.
 *  A dir is only a board if it contains _meta.json (mirrors the server rule). */
export function listResumableTickets(dataDir: string): ResumableTicket[] {
  const out: ResumableTicket[] = [];
  const boardsDir = path.join(dataDir, "boards");
  let slugs: string[];
  try {
    slugs = fs.readdirSync(boardsDir);
  } catch {
    return out;
  }
  for (const slug of slugs) {
    const boardDir = path.join(boardsDir, slug);
    if (!fs.existsSync(path.join(boardDir, "_meta.json"))) continue;
    for (const f of fs.readdirSync(boardDir)) {
      if (!f.endsWith(".json") || f === "_meta.json") continue;
      try {
        const t = JSON.parse(fs.readFileSync(path.join(boardDir, f), "utf8"));
        if (t && typeof t.claudeSessionId === "string" && t.claudeSessionId) {
          out.push({
            board: slug,
            id: String(t.id ?? f.replace(/\.json$/, "")),
            title: String(t.title ?? ""),
            sessionId: t.claudeSessionId,
          });
        }
      } catch {
        /* unreadable ticket: skip */
      }
    }
  }
  return out;
}
```

- [ ] **Step 4: Register the command in `extension.ts`** (inside `activate`, with the other registrations; import `listResumableTickets`):

```ts
vscode.commands.registerCommand("kanban.resumeTicketSession", async () => {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) return;
  const tickets = listResumableTickets(path.join(folder.uri.fsPath, ".kanban"));
  if (tickets.length === 0) {
    void vscode.window.showInformationMessage("Kanban: no tickets carry a session id.");
    return;
  }
  const pick = await vscode.window.showQuickPick(
    tickets.map((t) => ({
      label: `#${t.id} ${t.title}`,
      description: t.board,
      ticket: t,
    })),
    { placeHolder: "Resume which ticket's Claude session?" }
  );
  if (!pick) return;
  const term = vscode.window.createTerminal({
    name: `claude #${pick.ticket.id}`,
    cwd: folder.uri.fsPath,
  });
  term.show();
  // Prefer the configured CLI path (set when claude is off PATH); terminals
  // normally have the shell PATH, so the bare name is only the fallback.
  const claudeCmd =
    vscode.workspace.getConfiguration("kanban").get<string>("claudePath") || "claude";
  term.sendText(`${claudeCmd} --resume ${pick.ticket.sessionId}`, true);
})
```

- [ ] **Step 5: Create `extension/schemas/ticket.schema.json`** (permissive — validates shape, never blocks extra fields):

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Kanban ticket",
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "title": { "type": "string" },
    "status": { "enum": ["todo", "ready", "in_progress", "blocked", "pending", "completed"] },
    "detail": { "type": "string" },
    "dependsOn": { "type": "array", "items": { "type": "string" } },
    "blocks": { "type": "array", "items": { "type": "string" } },
    "steps": { "type": "array" },
    "files": { "type": "array" },
    "outputs": { "type": "array" },
    "claudeSessionId": { "type": "string" },
    "history": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "action": { "type": "string" },
          "from": { "type": "string" },
          "to": { "type": "string" },
          "timestamp": { "type": "string" },
          "sessionId": { "type": "string" }
        }
      }
    },
    "comments": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "writer": { "type": "string" },
          "message": { "type": "string" },
          "timestamp": { "type": "string" }
        },
        "required": ["message"]
      }
    }
  },
  "required": ["id", "title", "status"]
}
```

- [ ] **Step 6: Create `extension/schemas/board-meta.schema.json`:**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Kanban board metadata",
  "type": "object",
  "properties": {
    "project": { "type": "string" },
    "updated": { "type": "string" },
    "context": { "type": "string" },
    "directory": { "type": "string" },
    "openQuestions": { "type": "array" },
    "outOfScope": { "type": "array" },
    "useWorktrees": { "type": "boolean" },
    "useDocker": { "type": "boolean" },
    "commitRequirements": { "type": "string" },
    "envVars": { "type": "object" },
    "passthroughEnv": { "type": "array", "items": { "type": "string" } },
    "layrr": { "type": "object" }
  }
}
```

- [ ] **Step 7: Add to `extension/package.json` `contributes`:**

```json
"jsonValidation": [
  { "fileMatch": ["**/.kanban/boards/*/*.json", "!**/_meta.json"], "url": "./schemas/ticket.schema.json" },
  { "fileMatch": "**/.kanban/boards/*/_meta.json", "url": "./schemas/board-meta.schema.json" }
]
```

The `**/` prefixes are REQUIRED: the JSON language service compiles each `fileMatch` glob into a fully-anchored regex tested against the whole document URI (`file:///c%3A/...`), so a pattern starting with `.kanban/` never matches anything. The `!` negation entry is supported as-is.

- [ ] **Step 8: Run** — `npm test` + `npm run build`; F5 smoke: open a ticket JSON, confirm hover/validation from the schema; run "Kanban: Resume Ticket Session" and confirm the quickpick + terminal command.

- [ ] **Step 9: Commit**

```bash
git add extension/src/resume.ts extension/test/resume.test.ts extension/schemas/ extension/src/extension.ts extension/package.json
git commit -m "extension: resume-session command and ticket/board-meta JSON schemas"
```

---

### Task 13: Package the vsix + fresh-PC validation checklist

**Files:**
- Create: `extension/media/icon.png` (any 128×128 placeholder is fine; add `"icon": "media/icon.png"` to package.json)
- Modify: `docs/superpowers/specs/2026-08-17-vscode-extension-design.md` (append validation results)

**Interfaces:**
- Consumes: everything.
- Produces: the platform vsix (vsce names it `ai-kanban-board-win32-x64-0.1.0.vsix`-style — use whatever filename `vsce package` prints).

- [ ] **Step 1: Full build from clean** — in `extension/`: `npm test`, `npm run build`, `npm run fetch-runtime` — all green. Also run `python -m pytest tests -q` at repo root one more time.

- [ ] **Step 2: Package** — in `extension/`: `npm run package`. This must be NON-interactive: the script carries `--allow-missing-repository`, `"license": "UNLICENSED"` suppresses the LICENSE prompt, the Task 4 README satisfies the readme warning, and `vscode:prepublish` rebuilds `dist/` and re-stages `bundled/` automatically (no stale-bundle risk). Expected: a `.vsix` of roughly 40–80 MB. If vsce balks at file count, tighten `.vscodeignore` (the runtime's `Lib/` is large — that is expected and must ship).

- [ ] **Step 3: Sideload into real VS Code on THIS machine** — `code --install-extension <the vsix filename vsce printed>`. Open the parent workspace (`C:\Users\AE04581\Documents\GitHub`). Verify against the real boards: activation (output channel "server healthy"), Open Board renders, drag a ticket, status bar counts, Performance tab works (bundled psutil), spec/file links open in VS Code (the `vscode://` path smoke-tested in Task 10 Step 6), and the Setup tab's "Restart server" is adopted without a double-spawn.

- [ ] **Step 4: Fresh-workspace simulation** — open a brand-new empty folder, run "Kanban: Open Board". Expected: `.kanban/` scaffolded (boards/, skills/, config/ seeded, CLAUDE.md guide, orchestrator_triage_prompt.md), empty board UI, and — if you temporarily rename `claude` off PATH or use a machine without it — the dependency popup with the one-liner and working "Install in Terminal" (must open PowerShell, not the default profile) / "Copy Command" buttons. Confirm no popup appears when `kanban.claudePath` points at a valid off-PATH claude.

- [ ] **Step 5: Fresh-PC smoke (the actual acceptance test)** — on a machine (or Windows Sandbox/VM) with only VS Code: install the vsix, open an empty folder, confirm: (a) board opens with zero prerequisites; (b) claude + git popups appear with correct commands; (c) after installing claude via the popup command and logging in, ENABLE the orchestrator in the Orchestrator tab first — a fresh `state.json` defaults to `enabled: false` (`orchestrator_core.py:87`), and forgetting this makes dispatch look broken — then a `ready` ticket on a test board dispatches and completes; (d) `cpu_limiter` cap applies to the server (Task Manager: python.exe capped ~5%) while a dispatched claude runs uncapped. Record all four outcomes.

- [ ] **Step 6: Append results to the design doc** under a new `## Validation (v0.1.0)` heading — one line per smoke item with pass/fail and any deviations.

- [ ] **Step 7: Commit**

```bash
git add extension/media/icon.png extension/package.json docs/superpowers/specs/2026-08-17-vscode-extension-design.md
git commit -m "extension: v0.1.0 win32-x64 packaging and fresh-PC validation"
```

---

## Execution notes

- Tasks 1–3 (Part A) are independent of each other and of Part B; they can land first in any order. Tasks 4→13 are sequential (each consumes the previous scaffolding), except Task 6 (staging) and Task 7 (bootstrap) which only join at Task 7 Step 5.
- Task 11's payload shape was verified against the source on 2026-08-18 (`tasks` array; server-stamped `_board`, not `board`; `orchestrator.question.prompt` with null `answer` until answered). The curl in its Interfaces block is a cheap sanity re-check, not open-ended discovery.
- Known v1 limitations (accepted, do not "fix" mid-execution): a server that restarted itself via the UI button is adopted but unkillable by `dispose()` (it outlives the window); if the extension host dies without running `deactivate()`, the Python child orphans until reboot (the orchestrator lock self-heals on dead PIDs and the next activation binds a new port) — a PID-file kill-on-activate is a v1.1 candidate.
- The `.vsix` and `extension/bundled/` are never committed; the fetch script re-creates them on any machine with Node + network.
