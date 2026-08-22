"""Layrr live-edit launcher: patchers, config, registry lifecycle, routes.

The launcher (app/layrr_launcher.py) attaches the layrr point-and-click
overlay to a dev server ALREADY hosting a board's site, after re-applying
three patches to the resolved layrr install (git-write guard, kanban-sink
agent, ticket-widget injection). Instances are tracked in
_orchestrator/layrr.json and re-judged by port probe. No test here touches a
real layrr install, spawns a real process, or opens a real socket — the
seams (_spawn, _kill_tree, port_open, resolve_layrr) are stubbed.
"""

import json
import os
import threading
import http.client

import pytest

import kanban_server as ks
import layrr_launcher as ll


@pytest.fixture(autouse=True)
def clean_module_state():
    """The launcher caches Popen handles and the global npm root at module
    level; leaking either across tests makes ordering matter."""
    ll._PROCS.clear()
    ll._GLOBAL_NPM_ROOT = None
    yield
    ll._PROCS.clear()
    ll._GLOBAL_NPM_ROOT = None


# ── fake layrr install ───────────────────────────────────────────────────────

UPSTREAM_AGENT = "export class ClaudeAgent {}\nexport async function checkClaude() {}\n"
PROXY_JS = (
    "const overlayScript = `\n"
    "          <script>window.__LAYRR_WS_PORT__ = ${proxyPort};</script>\n"
    '          <script src="/__layrr__/overlay.js"></script>\n'
    "        `;\n"
    "html = html.replace('</body>', `${overlayScript}</body>`);\n"
)


@pytest.fixture
def fake_layrr(tmp_path):
    """A minimal layrr dist tree with every file the patchers touch."""
    root = tmp_path / "layrr"
    (root / "dist" / "agents").mkdir(parents=True)
    (root / "dist" / "server").mkdir(parents=True)
    (root / "dist" / "cli.js").write_text(
        "import { execSync } from 'child_process';\n"
        "console.log('Done (committed)');\n",
        encoding="utf-8",
    )
    (root / "dist" / "agents" / "claude.js").write_text(UPSTREAM_AGENT, encoding="utf-8")
    (root / "dist" / "server" / "proxy.js").write_text(PROXY_JS, encoding="utf-8")
    return str(root)


# ── git-write guard ──────────────────────────────────────────────────────────

def test_guard_patches_files_with_anchor(fake_layrr):
    result = ll.ensure_no_git_writes(fake_layrr)
    assert result["patched"] >= 1
    src = open(os.path.join(fake_layrr, "dist", "cli.js"), encoding="utf-8").read()
    assert ll.GIT_GUARD_MARKER in src
    assert ll.GIT_GUARD_ANCHOR not in src  # anchor replaced, not duplicated
    assert "__layrrIsGitWrite" in src


def test_guard_is_idempotent(fake_layrr):
    ll.ensure_no_git_writes(fake_layrr)
    before = open(os.path.join(fake_layrr, "dist", "cli.js"), encoding="utf-8").read()
    result = ll.ensure_no_git_writes(fake_layrr)
    after = open(os.path.join(fake_layrr, "dist", "cli.js"), encoding="utf-8").read()
    assert result["patched"] == 0 and result["already"] >= 1
    assert before == after


def test_guard_repatches_a_stale_guard(fake_layrr):
    """A file carrying the marker but an older guard body gets the current one."""
    ll.ensure_no_git_writes(fake_layrr)
    path = os.path.join(fake_layrr, "dist", "cli.js")
    src = open(path, encoding="utf-8").read()
    open(path, "w", encoding="utf-8").write(
        src.replace("windowsHide: true", "windowsHide: false"))
    result = ll.ensure_no_git_writes(fake_layrr)
    assert result["patched"] == 1
    assert "windowsHide: true" in open(path, encoding="utf-8").read()


def test_guard_refuses_when_no_anchor(tmp_path):
    root = tmp_path / "layrr"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "cli.js").write_text("console.log('no git here');\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        ll.ensure_no_git_writes(str(root))


def test_guard_rewrites_commit_log_lines(fake_layrr):
    ll.ensure_no_git_writes(fake_layrr)
    src = open(os.path.join(fake_layrr, "dist", "cli.js"), encoding="utf-8").read()
    assert "Done (committed)" not in src
    assert "yours to commit" in src


# ── kanban sink ──────────────────────────────────────────────────────────────

def test_sink_replaces_agent_and_backs_up_upstream(fake_layrr):
    assert ll.ensure_kanban_sink(fake_layrr) is True
    agents = os.path.join(fake_layrr, "dist", "agents")
    replaced = open(os.path.join(agents, "claude.js"), encoding="utf-8").read()
    backup = open(os.path.join(agents, "claude.upstream.js"), encoding="utf-8").read()
    assert ll.KANBAN_SINK_MARKER in replaced
    assert "LAYRR_TICKET_MODEL" in replaced  # model default rides on the sink
    assert backup == UPSTREAM_AGENT


def test_sink_is_idempotent_and_preserves_backup(fake_layrr):
    ll.ensure_kanban_sink(fake_layrr)
    agents = os.path.join(fake_layrr, "dist", "agents")
    # A second pass must not clobber the upstream backup with our own copy.
    assert ll.ensure_kanban_sink(fake_layrr) is False
    backup = open(os.path.join(agents, "claude.upstream.js"), encoding="utf-8").read()
    assert backup == UPSTREAM_AGENT


def test_sink_missing_agent_raises(tmp_path):
    root = tmp_path / "layrr"
    (root / "dist" / "agents").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        ll.ensure_kanban_sink(str(root))


# ── widget injection ─────────────────────────────────────────────────────────

def test_widget_injected_after_overlay_anchor(fake_layrr):
    assert ll.ensure_widget_injection(fake_layrr) is True
    src = open(os.path.join(fake_layrr, "dist", "server", "proxy.js"), encoding="utf-8").read()
    assert ll.WIDGET_ANCHOR in src  # anchor kept, widget appended after it
    assert "process.env.LAYRR_WIDGET_URL" in src
    assert src.find(ll.WIDGET_ANCHOR) < src.find("LAYRR_WIDGET_URL")


def test_widget_injection_is_idempotent(fake_layrr):
    ll.ensure_widget_injection(fake_layrr)
    path = os.path.join(fake_layrr, "dist", "server", "proxy.js")
    before = open(path, encoding="utf-8").read()
    assert ll.ensure_widget_injection(fake_layrr) is False
    assert open(path, encoding="utf-8").read() == before


def test_widget_snippet_upgrades_in_place(fake_layrr, monkeypatch):
    ll.ensure_widget_injection(fake_layrr)
    monkeypatch.setattr(ll, "WIDGET_SNIPPET",
                        ll.WIDGET_SNIPPET.replace("defer", "async"))
    assert ll.ensure_widget_injection(fake_layrr) is True
    src = open(os.path.join(fake_layrr, "dist", "server", "proxy.js"), encoding="utf-8").read()
    assert "async></script>" in src
    assert src.count("LAYRR_WIDGET_URL") == 2  # one src=, one guard — not doubled


def test_widget_refuses_when_anchor_missing(tmp_path):
    root = tmp_path / "layrr"
    (root / "dist" / "server").mkdir(parents=True)
    (root / "dist" / "server" / "proxy.js").write_text("nothing here", encoding="utf-8")
    with pytest.raises(RuntimeError):
        ll.ensure_widget_injection(str(root))


# ── install resolution ───────────────────────────────────────────────────────

def test_resolve_prefers_project_local(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    local = proj / "node_modules" / "layrr" / "dist"
    local.mkdir(parents=True)
    (local / "cli.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(ll, "_global_npm_root", lambda: str(tmp_path / "global"))
    assert ll.resolve_layrr(str(proj)) == str(proj / "node_modules" / "layrr")


def test_resolve_falls_back_to_global(tmp_path, monkeypatch):
    g = tmp_path / "global"
    (g / "layrr" / "dist").mkdir(parents=True)
    (g / "layrr" / "dist" / "cli.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(ll, "_global_npm_root", lambda: str(g))
    assert ll.resolve_layrr(str(tmp_path / "proj")) == str(g / "layrr")


def test_resolve_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "_global_npm_root", lambda: "")
    assert ll.resolve_layrr(str(tmp_path)) is None


# ── board config sanitizing ──────────────────────────────────────────────────

def test_sanitize_cfg_types_and_junk():
    clean = ll.sanitize_cfg({
        "targetPort": "5273", "projectRoot": "  C:\\x  ", "baseBranch": "working",
        "model": "claude-opus-4-8", "column": "ready", "junk": "dropped",
    })
    assert clean == {"targetPort": 5273, "projectRoot": "C:\\x",
                     "baseBranch": "working", "model": "claude-opus-4-8",
                     "column": "ready"}


@pytest.mark.parametrize("value", [None, "", "x", 5, [], {}, {"targetPort": "nope"},
                                   {"targetPort": 0}, {"targetPort": 99999}])
def test_sanitize_cfg_rejects_empty_and_invalid(value):
    assert ll.sanitize_cfg(value) is None


def test_update_board_meta_stores_sanitized_layrr(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    result, status = ks.update_board_meta(
        "demo", {"layrr": {"targetPort": "5273", "projectRoot": "C:\\site",
                           "model": "claude-opus-4-8", "junk": True}})
    assert status == 200 and result["ok"] is True
    meta = json.load(open(os.path.join(kanban, "boards", "demo", "_meta.json"), encoding="utf-8"))
    assert meta["layrr"] == {"targetPort": 5273, "projectRoot": "C:\\site",
                             "model": "claude-opus-4-8"}


def test_update_board_meta_empty_layrr_clears(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks.update_board_meta("demo", {"layrr": {"targetPort": 5273}})
    ks.update_board_meta("demo", {"layrr": {}})
    meta = json.load(open(os.path.join(kanban, "boards", "demo", "_meta.json"), encoding="utf-8"))
    assert "layrr" not in meta


def test_load_board_passes_layrr_through(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks.update_board_meta("demo", {"layrr": {"targetPort": 5273}})
    data, status = ks.load_board("demo")
    assert status == 200
    assert data["layrr"] == {"targetPort": 5273}


# ── start() ──────────────────────────────────────────────────────────────────

class FakeProc:
    def __init__(self, pid=4242, exit_code=None):
        self.pid = pid
        self._exit = exit_code

    def poll(self):
        return self._exit


@pytest.fixture
def startable(tmp_path, monkeypatch):
    """Everything start() needs stubbed for a happy path: a project dir, an
    open target port, a free 4567, patchers as no-ops, and a spawn recorder."""
    proj = tmp_path / "site"
    proj.mkdir()
    spawned = {}

    monkeypatch.setattr(ll, "port_open", lambda p: p == 5273)
    monkeypatch.setattr(ll.shutil, "which", lambda name: "C:\\node\\node.exe")
    monkeypatch.setattr(ll, "resolve_layrr", lambda root: str(tmp_path / "layrr"))
    monkeypatch.setattr(ll, "apply_patches", lambda root: None)
    monkeypatch.setattr(ll, "_repo_root_for", lambda root: "")
    monkeypatch.setattr(ll, "_kick_prepare", lambda script, node: None)

    def fake_spawn(cmd, cwd, env, log_path):
        spawned.update(cmd=cmd, cwd=cwd, env=env, log_path=log_path)
        return FakeProc()

    monkeypatch.setattr(ll, "_spawn", fake_spawn)
    return {"project": str(proj), "spawned": spawned}


def _meta(project, **layrr):
    cfg = {"targetPort": 5273, "projectRoot": project}
    cfg.update(layrr)
    return {"layrr": cfg}


def test_start_requires_config(kanban):
    payload, status = ll.start(kanban, "demo", {}, "http://127.0.0.1:8745")
    assert status == 400
    assert "port" in payload["error"]


def test_start_requires_existing_project_root(kanban, tmp_path):
    meta = _meta(str(tmp_path / "missing"))
    payload, status = ll.start(kanban, "demo", meta, "http://127.0.0.1:8745")
    assert status == 400
    assert "project root" in payload["error"]


def test_start_409_when_nothing_listening(kanban, startable, monkeypatch):
    monkeypatch.setattr(ll, "port_open", lambda p: False)
    payload, status = ll.start(kanban, "demo", _meta(startable["project"]),
                               "http://127.0.0.1:8745")
    assert status == 409
    assert "nothing is listening on port 5273" in payload["error"]


def test_start_happy_path(kanban, startable):
    payload, status = ll.start(kanban, "demo",
                               _meta(startable["project"], model="claude-opus-4-8"),
                               "http://127.0.0.1:8745")
    assert status == 200 and payload["ok"] is True
    inst = payload["instance"]
    assert inst["id"] == "demo-4567"
    assert inst["state"] == "starting"
    assert inst["url"] == "http://localhost:4567"
    assert inst["pid"] == 4242

    cmd = startable["spawned"]["cmd"]
    assert "--port" in cmd and "5273" in cmd
    assert "--proxy-port" in cmd and "4567" in cmd
    assert cmd[-1] == startable["project"]

    env = startable["spawned"]["env"]
    assert env["LAYRR_KANBAN_URL"] == "http://127.0.0.1:8745"
    assert env["LAYRR_KANBAN_BOARD"] == "demo"
    assert env["LAYRR_TICKET_MODEL"] == "claude-opus-4-8"
    assert env["LAYRR_BASE_BRANCH"] == "working"
    assert env["LAYRR_WIDGET_URL"] == "http://127.0.0.1:8745/layrr-widget.js"

    registry = ll.read_registry(kanban)
    assert [i["id"] for i in registry["instances"]] == ["demo-4567"]


def test_start_skips_taken_proxy_ports(kanban, startable):
    ll.write_registry(kanban, {"instances": [
        {"id": "other-4567", "board": "other", "targetPort": 9999, "proxyPort": 4567}]})
    payload, _ = ll.start(kanban, "demo", _meta(startable["project"]),
                          "http://127.0.0.1:8745")
    assert payload["instance"]["proxyPort"] == 4568


def test_start_returns_existing_running_instance(kanban, startable, monkeypatch):
    existing = {"id": "demo-4567", "board": "demo", "targetPort": 5273,
                "proxyPort": 4567, "url": "http://localhost:4567", "state": "running"}
    ll.write_registry(kanban, {"instances": [existing]})
    monkeypatch.setattr(ll, "port_open", lambda p: p in (5273, 4567))
    payload, status = ll.start(kanban, "demo", _meta(startable["project"]),
                               "http://127.0.0.1:8745")
    assert status == 200
    assert payload["alreadyRunning"] is True
    assert payload["instance"]["id"] == "demo-4567"
    assert startable["spawned"] == {}  # nothing new spawned


# ── status() lifecycle ───────────────────────────────────────────────────────

def _seed(kanban, monkeypatch, *, state="starting", started_at=None, port_answers=False):
    inst = {"id": "demo-4567", "board": "demo", "targetPort": 5273,
            "proxyPort": 4567, "url": "http://localhost:4567", "pid": 4242,
            "state": state, "startedAt": started_at or ll.now_iso(),
            "logFile": os.path.join(kanban, "_orchestrator", "layrr-logs", "demo-4567.log")}
    ll.write_registry(kanban, {"instances": [inst]})
    monkeypatch.setattr(ll, "port_open", lambda p: port_answers)
    return inst


def test_status_running_when_port_answers(kanban, monkeypatch):
    _seed(kanban, monkeypatch, port_answers=True)
    payload, status = ll.status(kanban)
    assert status == 200
    assert payload["instances"][0]["state"] == "running"


def test_status_keeps_fresh_starting_instance(kanban, monkeypatch):
    _seed(kanban, monkeypatch)
    payload, _ = ll.status(kanban)
    assert payload["instances"][0]["state"] == "starting"


def test_status_evicts_stale_instance(kanban, monkeypatch):
    _seed(kanban, monkeypatch, state="running",
          started_at="2020-01-01T00:00:00+00:00")
    payload, _ = ll.status(kanban)
    assert payload["instances"] == []
    assert ll.read_registry(kanban)["instances"] == []


def test_status_marks_dead_child_failed_with_log_tail(kanban, monkeypatch):
    inst = _seed(kanban, monkeypatch)
    os.makedirs(os.path.dirname(inst["logFile"]), exist_ok=True)
    with open(inst["logFile"], "w", encoding="utf-8") as f:
        f.write("boot\nport 5273 (dev server) is already in use.\n")
    ll._PROCS["demo-4567"] = FakeProc(exit_code=1)
    payload, _ = ll.status(kanban)
    got = payload["instances"][0]
    assert got["state"] == "failed"
    assert "exited with code 1" in got["lastError"]
    assert "already in use" in got["lastError"]


# ── stop() ───────────────────────────────────────────────────────────────────

def test_stop_kills_and_deregisters(kanban, monkeypatch):
    _seed(kanban, monkeypatch)
    killed = []
    monkeypatch.setattr(ll, "_kill_tree", killed.append)
    payload, status = ll.stop(kanban, "demo-4567")
    assert status == 200 and payload["ok"] is True
    assert killed == [4242]
    assert ll.read_registry(kanban)["instances"] == []


def test_stop_unknown_is_404(kanban):
    payload, status = ll.stop(kanban, "nope-1234")
    assert status == 404


# ── HTTP routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def server(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    httpd = ks.HTTPServer(("127.0.0.1", 0), ks.KanbanHandler)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield port
    httpd.shutdown()


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = conn.getresponse()
    raw = r.read().decode("utf-8")
    content_type = r.getheader("Content-Type", "")
    conn.close()
    return r.status, raw, content_type


def test_route_status_empty(server):
    status, raw, _ = _req(server, "GET", "/api/layrr/status")
    assert status == 200
    assert json.loads(raw) == {"instances": []}


def test_route_widget_served_as_js(server):
    status, raw, ctype = _req(server, "GET", "/layrr-widget.js")
    assert status == 200
    assert "javascript" in ctype
    assert "kanban-layrr-widget" in raw


def test_route_start_unknown_board_404(server):
    status, raw, _ = _req(server, "POST", "/api/layrr/start/ghost")
    assert status == 404


def test_route_start_unconfigured_board_400(server):
    status, raw, _ = _req(server, "POST", "/api/layrr/start/demo")
    assert status == 400
    assert "port" in json.loads(raw)["error"]


def test_route_stop_unknown_404(server):
    status, _, _ = _req(server, "POST", "/api/layrr/stop/nope-1")
    assert status == 404
