"""Layrr live-edit launcher — put the layrr overlay in front of a running site.

Layrr is a point-and-click editing proxy: you browse the PROXY url, click an
element, type what you want changed, and layrr maps the click back to a source
file. Here its agent is swapped for a "kanban sink" that files each request as
a ticket on a board, so the orchestrator works the edit under supervision (see
app/layrr/kanban-agent.mjs).

This module is the kanban-owned counterpart to b2-react's `npm run dev:layrr`
(scripts/dev-layrr.mjs in that repo). The difference in premise: dev:layrr
boots its own vite dev server first; this launcher attaches to a dev server
that is ALREADY listening on a port the board's settings name. Per-board
config lives on `_meta.json` as a `layrr` block:

    { "targetPort": 5273, "projectRoot": "<worktree serving that port>",
      "baseBranch": "working", "column": "ready" }

Multiple instances may be live at once (across boards or ports); the registry
is persisted to `_orchestrator/layrr.json` and re-validated by port probe, so
running overlays survive a kanban-server restart.

Before spawning, three patches are (re)applied to the resolved layrr install —
npm install restores upstream, so they re-apply on every start:

  1. git-write guard — layrr ships opinionated auto-commit behaviour; the guard
     wraps its execSync import so git WRITES become no-ops (reads still work,
     the overlay diff view needs them). The guard block is kept BYTE-IDENTICAL
     to the one b2-react's dev-layrr.mjs writes: both launchers may patch the
     same (global) layrr install, and any difference would make them ping-pong
     rewrite each other's patch on every start.
  2. kanban sink — app/layrr/kanban-agent.mjs is copied over layrr's
     dist/agents/claude.js (upstream preserved beside it), so an edit request
     becomes a POST to this server's create-task API instead of an inline
     Claude Code run. This copy extends the b2-react original (adds the
     LAYRR_TICKET_MODEL default-model stamp), so the two launchers rewrite
     the file back and forth on a shared install — harmless: each re-applies
     its own copy before spawning, and running processes loaded theirs at
     startup.
  3. ticket widget injection — layrr's HTML injection point gains a second,
     env-guarded <script> tag loading /layrr-widget.js from this server, which
     renders a floating panel of the layrr-filed tickets and their live status
     inside the proxied app. Env-guarded so the b2-react dev:layrr flow (which
     doesn't set LAYRR_WIDGET_URL) is unaffected by the shared-install patch.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone

APP_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_SOURCE = os.path.join(APP_DIR, "layrr", "kanban-agent.mjs")

PROXY_PORT_BASE = 4567
# How long a "starting" instance may sit with a closed proxy port before
# status() writes it off (layrr's own preflight timeout is 8s; startup is
# normally 2-3s on top of that).
START_GRACE_SECONDS = 90

# ── git-write guard ─────────────────────────────────────────────────────────
# Byte-identical to b2-react scripts/layrr-git-guard.mjs (see module docstring
# for why identical matters). Do not reword even the attribution comment.

GIT_GUARD_MARKER = "[b2react] layrr git writes disabled"
GIT_GUARD_ANCHOR = "import { execSync } from 'child_process';"
GIT_GUARD_HEAD = "import { execSync as __layrrExecSync } from 'child_process';"
GIT_GUARD_END = "// [b2react] end git guard"
GIT_GUARD = GIT_GUARD_HEAD + r"""
// [b2react] layrr git writes disabled — re-applied by b2react scripts/dev-layrr.mjs, which
// rewrites this line on every run because npm install restores the original.
const __LAYRR_GIT_WRITES = new Set([
  'add', 'am', 'apply', 'branch', 'checkout', 'cherry-pick', 'clean', 'commit',
  'filter-branch', 'gc', 'init', 'merge', 'mv', 'prune', 'push', 'rebase',
  'reset', 'restore', 'rm', 'stash', 'switch', 'tag', 'update-ref', 'worktree',
]);
// Find the real subcommand rather than pattern-matching the string: global
// options come first, and -C/-c each swallow a separate value. Getting this
// wrong in the lenient direction would let a write through; getting it wrong
// in the strict direction would break `git log --grep=commit`.
function __layrrIsGitWrite(cmd) {
  const t = String(cmd).trim().split(/\s+/);
  if (t[0] !== 'git') return false;
  let i = 1;
  while (i < t.length) {
    if (t[i] === '-C' || t[i] === '-c') { i += 2; continue; }
    if (t[i].startsWith('-')) { i += 1; continue; }
    break;
  }
  return __LAYRR_GIT_WRITES.has(t[i]);
}
function execSync(cmd, opts) {
  if (typeof cmd === 'string' && __layrrIsGitWrite(cmd)) return '';
  // windowsHide forced on: layrr shells out to git on every single edit
  // (status, diff --name-only, ls-files), and each one flashes a console
  // window open and shut while you are trying to use the overlay.
  return __layrrExecSync(cmd, { ...opts, windowsHide: true });
}
""" + GIT_GUARD_END

KANBAN_SINK_MARKER = "[b2react] layrr → kanban sink"

# ── ticket-widget injection ─────────────────────────────────────────────────
# layrr's proxy injects its overlay into every proxied HTML page by replacing
# this exact tag inside a template literal in dist/server/proxy.js. The widget
# snippet appended after it is a ${} expression IN that template literal, so
# it resolves per-request from the layrr process's env — each live instance
# carries its own board/urls, and a process without LAYRR_WIDGET_URL (the
# b2-react dev:layrr flow) injects nothing. The /*...*/ sentinels are JS
# comments inside the expression: invisible in served HTML, and they bound the
# block so a changed snippet can be swapped without re-anchoring.
WIDGET_ANCHOR = '<script src="/__layrr__/overlay.js"></script>'
WIDGET_START = "/*KANBAN_WIDGET_START*/"
WIDGET_END = "/*KANBAN_WIDGET_END*/"
WIDGET_SNIPPET = (
    "\n          ${" + WIDGET_START
    + "process.env.LAYRR_WIDGET_URL ? `<script src=\"${process.env.LAYRR_WIDGET_URL}\""
    + " data-kanban=\"${process.env.LAYRR_KANBAN_URL || ''}\""
    + " data-board=\"${process.env.LAYRR_KANBAN_BOARD || ''}\" defer></script>` : ''"
    + WIDGET_END + "}"
)

# Live Popen handles, keyed by instance id. Only this process's own spawns are
# here; instances adopted from the registry after a server restart have no
# handle and are judged purely by their proxy port answering.
_PROCS = {}

_GLOBAL_NPM_ROOT = None  # cached; `npm root -g` shells out and this box is slow


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── port probing ────────────────────────────────────────────────────────────

def _probe(host, port):
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def port_open(port):
    """True if anything answers on *port* on either loopback stack.

    Vite binds `localhost`, which on this machine resolves to ::1 alone —
    nothing answers on 127.0.0.1. Probing only one stack either waits forever
    on a listening server or calls a half-held port free.
    """
    return _probe("127.0.0.1", port) or _probe("::1", port)


def find_free_port(start, taken):
    """First port >= start that neither answers a probe nor is in *taken*."""
    port = start
    while port < 65536:
        if port not in taken and not port_open(port):
            return port
        port += 1
    raise RuntimeError("no free proxy port found")


# ── layrr install resolution ────────────────────────────────────────────────

def _global_npm_root():
    global _GLOBAL_NPM_ROOT
    if _GLOBAL_NPM_ROOT is not None:
        return _GLOBAL_NPM_ROOT
    try:
        # shell=True: on Windows npm is npm.cmd, which needs the shell to run.
        out = subprocess.run(
            "npm root -g", shell=True, capture_output=True, text=True,
            timeout=30,
        )
        _GLOBAL_NPM_ROOT = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        _GLOBAL_NPM_ROOT = ""
    return _GLOBAL_NPM_ROOT


def resolve_layrr(project_root):
    """Path of a usable layrr install, or None.

    The project-local devDependency is preferred (pinned, reproducible); the
    global install is the fallback. Under the kanban sink layrr never spawns
    Claude Code, so an install missing the bundled @anthropic-ai/claude-code
    (npm silently drops it on this machine) is perfectly usable.
    """
    candidates = [os.path.join(project_root, "node_modules", "layrr")]
    g = _global_npm_root()
    if g:
        candidates.append(os.path.join(g, "layrr"))
    for root in candidates:
        if os.path.isfile(os.path.join(root, "dist", "cli.js")):
            return root
    return None


# ── patches ─────────────────────────────────────────────────────────────────

def _js_files_under(directory):
    out = []
    for base, _dirs, files in os.walk(directory):
        for name in files:
            if name.endswith(".js"):
                out.append(os.path.join(base, name))
    return out


def ensure_no_git_writes(layrr_root):
    """Wrap layrr's execSync import so git writes become no-ops.

    Mirrors b2-react's ensureNoGitWrites() exactly, including the re-patch
    logic: a file already carrying the marker gets its bounded guard block
    compared and swapped if the guard has since changed, so a guard fix
    reaches installs patched by an older version. Raises RuntimeError when no
    file matches — refusing to start beats silently letting layrr commit.
    """
    dist = os.path.join(layrr_root, "dist")
    patched = already = 0

    for path in _js_files_under(dist):
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()

        if GIT_GUARD_MARKER in src:
            start = src.find(GIT_GUARD_HEAD)
            tail = src.find("__layrrExecSync(cmd", start) if start != -1 else -1
            brace = src.find("\n}", tail) if tail != -1 else -1
            if start == -1 or brace == -1:
                already += 1
                continue
            end = brace + 2
            if src[end:].startswith("\n" + GIT_GUARD_END):
                end += 1 + len(GIT_GUARD_END)
            if src[start:end] == GIT_GUARD:
                already += 1
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(src[:start] + GIT_GUARD + src[end:])
            patched += 1
            continue

        if GIT_GUARD_ANCHOR not in src:
            continue  # file doesn't shell out at all
        with open(path, "w", encoding="utf-8") as f:
            f.write(src.replace(GIT_GUARD_ANCHOR, GIT_GUARD, 1))
        patched += 1

    if patched + already == 0:
        raise RuntimeError(
            "could not disable layrr's git writes — no file matched the "
            f"expected import line ({GIT_GUARD_ANCHOR!r}); layrr has probably "
            "changed shape. Refusing to start rather than let it commit."
        )

    # Best-effort: its log lines still announce commits that can't happen.
    cli = os.path.join(dist, "cli.js")
    if os.path.isfile(cli):
        with open(cli, "r", encoding="utf-8") as f:
            before = f.read()
        after = (before
                 .replace("Done (committed)",
                          "Done — not committed, yours to commit")
                 .replace("Committing existing changes before starting...",
                          "Leaving your working tree alone (git writes disabled)")
                 .replace("Existing changes committed", "Working tree untouched"))
        if after != before:
            with open(cli, "w", encoding="utf-8") as f:
                f.write(after)

    return {"patched": patched, "already": already}


def ensure_kanban_sink(layrr_root):
    """Copy the kanban-sink agent over layrr's dist/agents/claude.js.

    layrr imports { ClaudeAgent, checkClaude } from that path; matching those
    exports is the whole contract. Upstream is preserved beside it as
    claude.upstream.js so the stock behaviour is recoverable without a
    reinstall (npm install restores upstream over both, which is fine — the
    backup is re-taken here on the next start).
    """
    agent_path = os.path.join(layrr_root, "dist", "agents", "claude.js")
    backup_path = os.path.join(layrr_root, "dist", "agents", "claude.upstream.js")
    if not os.path.isfile(agent_path):
        raise RuntimeError(f"layrr is missing dist/agents/claude.js at {layrr_root}")
    with open(agent_path, "r", encoding="utf-8") as f:
        current = f.read()
    with open(AGENT_SOURCE, "r", encoding="utf-8") as f:
        desired = f.read()
    if KANBAN_SINK_MARKER not in current:
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(current)
    if current != desired:
        with open(agent_path, "w", encoding="utf-8") as f:
            f.write(desired)
        return True
    return False


def ensure_widget_injection(layrr_root):
    """Add the env-guarded ticket-widget <script> to layrr's HTML injection."""
    proxy_path = os.path.join(layrr_root, "dist", "server", "proxy.js")
    if not os.path.isfile(proxy_path):
        raise RuntimeError(f"layrr is missing dist/server/proxy.js at {layrr_root}")
    with open(proxy_path, "r", encoding="utf-8") as f:
        src = f.read()

    if WIDGET_START in src:
        start = src.find("\n          ${" + WIDGET_START)
        end = src.find(WIDGET_END + "}", start)
        if start == -1 or end == -1:
            return False  # someone half-removed it; leave it alone
        end += len(WIDGET_END) + 1
        if src[start:end] == WIDGET_SNIPPET:
            return False
        with open(proxy_path, "w", encoding="utf-8") as f:
            f.write(src[:start] + WIDGET_SNIPPET + src[end:])
        return True

    if WIDGET_ANCHOR not in src:
        raise RuntimeError(
            "could not find layrr's overlay injection point in "
            "dist/server/proxy.js — layrr has probably changed shape, so the "
            "ticket widget cannot be injected."
        )
    with open(proxy_path, "w", encoding="utf-8") as f:
        f.write(src.replace(WIDGET_ANCHOR, WIDGET_ANCHOR + WIDGET_SNIPPET, 1))
    return True


def apply_patches(layrr_root):
    ensure_no_git_writes(layrr_root)
    ensure_kanban_sink(layrr_root)
    ensure_widget_injection(layrr_root)


# ── registry ────────────────────────────────────────────────────────────────

def registry_path(kanban_dir):
    return os.path.join(kanban_dir, "_orchestrator", "layrr.json")


def read_registry(kanban_dir):
    try:
        with open(registry_path(kanban_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data.get("instances"), list):
        data["instances"] = []
    return data


def write_registry(kanban_dir, data):
    path = registry_path(kanban_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_json(path, data)


def _log_dir(kanban_dir):
    return os.path.join(kanban_dir, "_orchestrator", "layrr-logs")


def _log_tail(path, lines=12):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "\n".join(f.read().splitlines()[-lines:])
    except OSError:
        return ""


def _age_seconds(started_at):
    try:
        t = datetime.fromisoformat(started_at)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except (ValueError, TypeError):
        return None


# ── repo context for the sink's ticket prose ────────────────────────────────

def _repo_root_for(project_root):
    """The MAIN checkout for the repo containing *project_root* (which is
    usually a linked worktree) — the path agents `git push` into."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=project_root, capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return os.path.abspath(os.path.dirname(out.stdout.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ── spawn / kill (separated so tests can stub them) ─────────────────────────

def _spawn(cmd, cwd, env, log_path):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "a", encoding="utf-8")
    kwargs = {"cwd": cwd, "env": env, "stdout": log, "stderr": subprocess.STDOUT,
              "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        # Own process group + no console flash; the server's Job Object lets
        # children break away, so layrr runs outside the CPU cap.
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kwargs)
    finally:
        # Popen dup'd the handle (or raised); either way ours can close.
        log.close()


def _kill_tree(pid):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass  # already gone


def _kick_prepare(prepare_script, node):
    """Warm the next ticket's worktree in the background. Never blocks or
    fails a start — a missing spare only means the worker sets up itself."""
    if not prepare_script or not os.path.isfile(prepare_script):
        return
    try:
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                  "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([node, prepare_script], **kwargs)
    except OSError:
        pass


# ── board config ────────────────────────────────────────────────────────────

def sanitize_cfg(value):
    """Sanitize a board's `layrr` settings block (from the Project Settings
    form). Returns the clean dict, or None when nothing survives (caller
    removes the field)."""
    if not isinstance(value, dict):
        return None
    out = {}
    try:
        port = int(value.get("targetPort"))
        if 0 < port < 65536:
            out["targetPort"] = port
    except (TypeError, ValueError):
        pass
    for key in ("projectRoot", "baseBranch", "column", "model"):
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    return out or None


# ── API surface (each returns (payload, http_status)) ───────────────────────

def start(kanban_dir, slug, meta, kanban_url):
    """Spawn a layrr proxy for board *slug* per its `layrr` settings block.

    Returns immediately with the instance in state "starting" — this server is
    single-threaded, and layrr's own preflight calls back into our /api/files,
    so waiting here for the proxy port to open would deadlock. The UI (and
    status()) watch the port come up instead.
    """
    cfg = (meta or {}).get("layrr") or {}
    target_port = cfg.get("targetPort")
    if not target_port:
        return {"error": "no dev-server port configured — set one in this "
                         "project's settings first"}, 400
    project_root = cfg.get("projectRoot") or ""
    if not project_root or not os.path.isdir(project_root):
        return {"error": f"layrr project root not found on disk: "
                         f"{project_root or '(unset)'}"}, 400
    if not port_open(target_port):
        return {"error": f"nothing is listening on port {target_port} — start "
                         "the dev server hosting the site first, then Go "
                         "live"}, 409

    registry = read_registry(kanban_dir)
    for inst in registry["instances"]:
        if (inst.get("board") == slug
                and inst.get("targetPort") == target_port
                and port_open(inst.get("proxyPort", 0))):
            return {"ok": True, "alreadyRunning": True, "instance": inst}, 200

    node = shutil.which("node")
    if not node:
        return {"error": "node was not found on the server's PATH"}, 500
    layrr_root = resolve_layrr(project_root)
    if not layrr_root:
        return {"error": "no layrr install found (looked in the project's "
                         "node_modules and the global npm root). Fix with: "
                         "npm install -g layrr"}, 500
    try:
        apply_patches(layrr_root)
    except (OSError, RuntimeError) as e:
        return {"error": f"could not patch layrr: {e}"}, 500

    taken = {i.get("proxyPort") for i in registry["instances"]}
    try:
        proxy_port = find_free_port(PROXY_PORT_BASE, taken)
    except RuntimeError as e:
        return {"error": str(e)}, 500

    base_branch = cfg.get("baseBranch") or "working"
    repo_root = _repo_root_for(project_root)
    pool_state = ""
    if repo_root:
        candidate = os.path.join(os.path.dirname(repo_root), ".worktrees",
                                 ".layrr-pool.json")
        if os.path.isfile(candidate):
            pool_state = candidate
    prepare_script = os.path.join(project_root, "scripts",
                                  "layrr-prepare-worktree.mjs")
    if not os.path.isfile(prepare_script):
        prepare_script = ""

    env = dict(os.environ)
    env.update({
        "LAYRR_KANBAN_URL": kanban_url,
        "LAYRR_KANBAN_BOARD": slug,
        "LAYRR_KANBAN_COLUMN": cfg.get("column") or "ready",
        "LAYRR_TICKET_MODEL": cfg.get("model") or "",
        "LAYRR_BASE_BRANCH": base_branch,
        "LAYRR_REPO_ROOT": repo_root,
        "LAYRR_POOL_STATE": pool_state,
        "LAYRR_PREPARE_SCRIPT": prepare_script,
        "LAYRR_WIDGET_URL": f"{kanban_url}/layrr-widget.js",
    })

    instance_id = f"{slug}-{proxy_port}"
    log_path = os.path.join(_log_dir(kanban_dir), f"{instance_id}.log")
    cmd = [node, os.path.join(layrr_root, "dist", "cli.js"),
           "--port", str(target_port), "--proxy-port", str(proxy_port),
           "--agent", "claude", "--no-open", project_root]
    try:
        proc = _spawn(cmd, project_root, env, log_path)
    except OSError as e:
        return {"error": f"failed to spawn layrr: {e}"}, 500

    instance = {
        "id": instance_id,
        "board": slug,
        "targetPort": target_port,
        "proxyPort": proxy_port,
        "url": f"http://localhost:{proxy_port}",
        "projectRoot": project_root,
        "baseBranch": base_branch,
        "pid": proc.pid,
        "state": "starting",
        "startedAt": now_iso(),
        "logFile": log_path,
    }
    _PROCS[instance_id] = proc
    registry["instances"].append(instance)
    write_registry(kanban_dir, registry)

    _kick_prepare(prepare_script, node)
    return {"ok": True, "instance": instance}, 200


def status(kanban_dir):
    """All registered instances, re-judged by whether their proxy answers.

    An instance whose port answers is `running` (whoever spawned it — this
    covers adoption after a server restart). A closed port means `starting`
    while within the grace window, `failed` when our own child visibly exited
    (log tail surfaced as lastError), and eviction once stale — a failed entry
    survives one status pass so the UI can show why, then is dropped on the
    next call after its grace expires.
    """
    registry = read_registry(kanban_dir)
    kept, changed = [], False
    for inst in registry["instances"]:
        inst = dict(inst)
        prev_state = inst.get("state")
        if port_open(inst.get("proxyPort", 0)):
            inst["state"] = "running"
            inst.pop("lastError", None)
        else:
            proc = _PROCS.get(inst.get("id"))
            age = _age_seconds(inst.get("startedAt"))
            exited = proc is not None and proc.poll() is not None
            if exited and prev_state != "failed":
                inst["state"] = "failed"
                tail = _log_tail(inst.get("logFile", ""))
                inst["lastError"] = (
                    f"layrr exited with code {proc.poll()}"
                    + (f"\n{tail}" if tail else ""))
            elif not exited and prev_state == "starting" and (
                    age is None or age < START_GRACE_SECONDS):
                pass  # still coming up
            elif prev_state == "failed" and (
                    age is not None and age < START_GRACE_SECONDS):
                pass  # keep the failure visible briefly
            else:
                _PROCS.pop(inst.get("id"), None)
                changed = True
                continue  # stale/dead — evict
        if inst.get("state") != prev_state:
            changed = True
        kept.append(inst)
    if changed:
        write_registry(kanban_dir, {**registry, "instances": kept})
    return {"instances": kept}, 200


def stop(kanban_dir, instance_id):
    registry = read_registry(kanban_dir)
    remaining, found = [], None
    for inst in registry["instances"]:
        if inst.get("id") == instance_id:
            found = inst
        else:
            remaining.append(inst)
    if found is None:
        return {"error": "no such layrr instance"}, 404
    pid = found.get("pid")
    if pid:
        _kill_tree(pid)
    _PROCS.pop(instance_id, None)
    write_registry(kanban_dir, {**registry, "instances": remaining})
    return {"ok": True, "stopped": instance_id}, 200
