"""Ticket #16: Docker dev-workspace per repo (Option A).

The orchestrator can build one Docker image per board repo and run the dispatched
`claude -p` agent INSIDE a container, with per-board editable env vars supplied
via `--env-file`. This covers:
  1. The pure core logic (flags, env sanitizing/rendering, Docker naming, path
     translation, argv builders) in orchestrator_core.
  2. The spawn_agent wiring + container teardown in orchestrator.
  3. The server exposing/sanitizing the `useDocker` + `envVars` board-meta fields.
"""

import json
import os

import orchestrator_core as oc
import orchestrator as orch
import kanban_server as ks


# --- use_docker flag --------------------------------------------------------

def test_use_docker_defaults_false():
    assert oc.use_docker({}) is False
    assert oc.use_docker(None) is False


def test_use_docker_true_when_set():
    assert oc.use_docker({"useDocker": True}) is True


def test_use_docker_tolerates_stringified_booleans():
    assert oc.use_docker({"useDocker": "true"}) is True
    assert oc.use_docker({"useDocker": "false"}) is False


# --- env var key validation + sanitizing ------------------------------------

def test_valid_env_key():
    assert oc.valid_env_key("FOO_BAR")
    assert oc.valid_env_key("_x1")
    assert not oc.valid_env_key("1abc")
    assert not oc.valid_env_key("a-b")
    assert not oc.valid_env_key("has space")
    assert not oc.valid_env_key("")
    assert not oc.valid_env_key(None)


def test_board_env_vars_keeps_valid_and_coerces_scalars():
    meta = {"envVars": {"OK": "v", "also_ok": "2", "NUM": 5, "FLAG": True,
                        "1bad": "x", "bad-key": "y", "NADA": None,
                        "NESTED": {"a": 1}}}
    assert oc.board_env_vars(meta) == {
        "OK": "v", "also_ok": "2", "NUM": "5", "FLAG": "true"}


def test_board_env_vars_non_dict_is_empty():
    assert oc.board_env_vars({"envVars": "nope"}) == {}
    assert oc.board_env_vars({}) == {}
    assert oc.board_env_vars(None) == {}


# --- env-file rendering -----------------------------------------------------

def test_render_env_file_basic():
    assert oc.render_env_file({"A": "1", "B": "two"}) == "A=1\nB=two\n"


def test_render_env_file_empty():
    assert oc.render_env_file({}) == ""


def test_render_env_file_flattens_newlines():
    # Newlines would corrupt the line-based --env-file format.
    assert oc.render_env_file({"X": "a\nb\r"}) == "X=a b\n"


# --- Docker naming ----------------------------------------------------------

def test_docker_safe_token():
    assert oc._docker_safe("") == "workspace"
    assert oc._docker_safe("Discord-Bot") == "discord-bot"
    assert oc._docker_safe("Weird/Name!!") == "weird-name"


def test_docker_image_tag_and_container_name():
    assert oc.docker_image_tag("Discord-Bot") == "ai-kanban-workspace:discord-bot"
    assert oc.docker_container_name("demo", {"id": "16"}) == \
        "ai-kanban-workspace-demo-16"
    # No ticket id -> board-only container name.
    assert oc.docker_container_name("demo", {}) == "ai-kanban-workspace-demo"


# --- host -> container path translation -------------------------------------

def test_translate_host_paths(tmp_path):
    host = str(tmp_path)
    ticket = os.path.join(host, ".AI-kanban", "demo", "1.json")
    out = oc.translate_host_paths(f"edit {ticket} now", host)
    assert "/workspace" in out
    assert host not in out
    assert out.endswith(" now")
    # The backslashes in the matched tail are flipped to forward slashes.
    assert "\\" not in out.split("/workspace", 1)[1].split(" ", 1)[0]


def test_translate_host_paths_noop_when_absent_or_empty():
    assert oc.translate_host_paths("nothing here", os.path.join("Z:", "x")) \
        == "nothing here"
    assert oc.translate_host_paths("x", "") == "x"
    assert oc.translate_host_paths("", "/root") == ""


# --- argv builders ----------------------------------------------------------

def test_docker_build_argv():
    assert oc.docker_build_argv("img:tag", "/d/Dockerfile", "/d") == \
        ["docker", "build", "-t", "img:tag", "-f", "/d/Dockerfile", "/d"]


def test_docker_run_argv_shape():
    argv = oc.docker_run_argv("img:tag", "cname", "/host/root", "/ef.env",
                              ["claude", "-p", "hi"],
                              passthrough_env=["ANTHROPIC_API_KEY"])
    assert argv[:5] == ["docker", "run", "--rm", "--name", "cname"]
    assert "/host/root:/workspace" in argv
    assert argv[argv.index("--env-file") + 1] == "/ef.env"
    assert argv[argv.index("-e") + 1] == "ANTHROPIC_API_KEY"
    # The image tag is immediately followed by the inner command.
    i = argv.index("img:tag")
    assert argv[i + 1:] == ["claude", "-p", "hi"]


def test_docker_run_argv_without_envfile():
    argv = oc.docker_run_argv("img", "c", "/r", None, ["claude"])
    assert "--env-file" not in argv
    assert argv[-1] == "claude"


# --- spawn_agent wiring -----------------------------------------------------

def _docker_board(kanban, env=None):
    meta = {"project": "Demo", "useDocker": True}
    if env is not None:
        meta["envVars"] = env
    with open(os.path.join(kanban, "demo", "_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    dockerdir = os.path.join(kanban, "_orchestrator", "docker")
    os.makedirs(dockerdir, exist_ok=True)
    with open(os.path.join(dockerdir, "Dockerfile"), "w", encoding="utf-8") as f:
        f.write("FROM node:20-slim\n")
    # A useDocker board now builds from its own per-board Dockerfile (ticket #6);
    # without it the preflight would block and _build_docker_image would skip.
    with open(os.path.join(dockerdir, "demo.Dockerfile"), "w", encoding="utf-8") as f:
        f.write("FROM node:20-slim\n")


def test_spawn_agent_docker_runs_in_container(kanban, monkeypatch):
    _docker_board(kanban, env={"FOO": "bar"})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    builds = []

    class FakeBuild:
        returncode = 0

    monkeypatch.setattr(orch.subprocess, "run",
                        lambda argv, **k: builds.append(argv) or FakeBuild())

    captured = {}

    class FakeProc:
        pid = 7777

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)

    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    marker = orch.spawn_agent(kanban, "demo", task,
                              {"name": "g", "systemPrompt": "p"}, "m")

    cmd = captured["cmd"]
    assert cmd[:3] == ["docker", "run", "--rm"]
    # The image was built first.
    assert any(a[:2] == ["docker", "build"] for a in builds)
    # Workspace root is mounted at /workspace and an env-file is supplied.
    assert any(str(a).endswith(":/workspace") for a in cmd)
    assert "--env-file" in cmd
    # The container name is recorded on the marker AND used in the run command.
    cname = oc.docker_container_name("demo", task)
    assert marker["containerName"] == cname
    assert cname in cmd
    # The inner claude invocation runs after the image tag, with host paths in
    # the prompt translated onto the /workspace mount.
    tag = oc.docker_image_tag("demo")
    inner = cmd[cmd.index(tag) + 1:]
    assert inner[0] == "claude" and inner[1] == "-p"
    assert "/workspace" in inner[2]
    assert kanban not in inner[2]


def test_spawn_agent_docker_forwards_api_key(kanban, monkeypatch):
    _docker_board(kanban)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    captured = {}

    class FakeProc:
        pid = 7778

        def poll(self):
            return None

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")
    cmd = captured["cmd"]
    # The host's credential is forwarded by name (value inherited), never echoed.
    assert "ANTHROPIC_API_KEY" in cmd
    assert "sk-test" not in cmd


def test_spawn_agent_non_docker_has_no_container(kanban, monkeypatch):
    # Regression: default board (no useDocker) still runs a plain claude subprocess.
    class FakeProc:
        pid = 8888

        def poll(self):
            return None

    captured = {}

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    marker = orch.spawn_agent(kanban, "demo", task,
                              {"name": "g", "systemPrompt": "p"}, "m")
    assert "containerName" not in marker
    assert captured["cmd"][0] != "docker"
    assert captured["cmd"][1] == "-p"


def test_write_board_env_file(kanban):
    path = orch._write_board_env_file(
        kanban, "demo", {"envVars": {"A": "1", "bad key": "x", "B": "two"}})
    text = open(path, encoding="utf-8").read()
    assert "A=1" in text and "B=two" in text
    assert "bad key" not in text


# --- container teardown -----------------------------------------------------

def test_kill_container_calls_docker(monkeypatch):
    calls = []
    monkeypatch.setattr(orch.subprocess, "run", lambda argv, **k: calls.append(argv))
    assert orch._kill_container("c1") is True
    assert calls == [["docker", "kill", "c1"]]


def test_kill_container_noop_for_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(orch.subprocess, "run", lambda argv, **k: calls.append(argv))
    assert orch._kill_container("") is False
    assert orch._kill_container(None) is False
    assert calls == []


# --- server meta fields -----------------------------------------------------

def test_server_persists_docker_meta(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks.update_board_meta("demo", {"useDocker": True,
                                  "envVars": {"FOO": "bar", "bad key": "x"}})
    meta = json.load(open(os.path.join(kanban, "demo", "_meta.json"), encoding="utf-8"))
    assert meta["useDocker"] is True
    assert meta["envVars"] == {"FOO": "bar"}  # invalid key sanitized out

    data, status = ks.load_board("demo")
    assert status == 200
    assert data["useDocker"] is True
    assert data["envVars"] == {"FOO": "bar"}


def test_server_empty_env_vars_removes_field(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks.update_board_meta("demo", {"envVars": {"FOO": "bar"}})
    ks.update_board_meta("demo", {"envVars": {}})
    meta = json.load(open(os.path.join(kanban, "demo", "_meta.json"), encoding="utf-8"))
    assert "envVars" not in meta


# --- per-board Dockerfile requirement + preflight (ticket #6) ----------------

def test_board_dockerfile_name():
    assert oc.board_dockerfile_name("demo") == "demo.Dockerfile"
    assert oc.board_dockerfile_name("Discord-Bot") == "discord-bot.Dockerfile"
    # Empty/weird board names still yield a valid, non-empty filename.
    assert oc.board_dockerfile_name("") == "workspace.Dockerfile"


def test_resolve_board_dockerfile_prefers_per_board(tmp_path):
    docker_dir = str(tmp_path)
    # Only the generic template exists -> no per-board resolution.
    open(os.path.join(docker_dir, "Dockerfile"), "w").write("FROM node:20-slim\n")
    assert oc.resolve_board_dockerfile(docker_dir, "demo") is None
    # Add the per-board file -> it resolves to that exact path.
    per_board = os.path.join(docker_dir, "demo.Dockerfile")
    open(per_board, "w").write("FROM python:3\n")
    assert oc.resolve_board_dockerfile(docker_dir, "demo") == per_board


def test_resolve_board_dockerfile_missing_dir():
    assert oc.resolve_board_dockerfile("", "demo") is None
    assert oc.resolve_board_dockerfile("/no/such/dir", "demo") is None


def test_docker_preflight_ok_when_disabled(tmp_path):
    # A non-docker board never needs a Dockerfile -> trivially ok.
    ok, reason = oc.docker_preflight(str(tmp_path), {"useDocker": False}, "demo")
    assert ok is True


def test_docker_preflight_ok_when_per_board_present(tmp_path):
    docker_dir = str(tmp_path)
    open(os.path.join(docker_dir, "demo.Dockerfile"), "w").write("FROM python:3\n")
    ok, reason = oc.docker_preflight(docker_dir, {"useDocker": True}, "demo")
    assert ok is True


def test_docker_preflight_fails_when_missing(tmp_path):
    # useDocker on but no per-board Dockerfile (even a generic one) -> blocked,
    # with an actionable reason naming the file to create.
    open(os.path.join(str(tmp_path), "Dockerfile"), "w").write("FROM node:20-slim\n")
    ok, reason = oc.docker_preflight(str(tmp_path), {"useDocker": True}, "demo")
    assert ok is False
    assert "demo.Dockerfile" in reason


# --- image build resolves the per-board Dockerfile --------------------------

def test_build_docker_image_uses_per_board_dockerfile(kanban, monkeypatch):
    docker_dir = os.path.join(kanban, "_orchestrator", "docker")
    os.makedirs(docker_dir, exist_ok=True)
    # Generic template present but should NOT be the one built from.
    open(os.path.join(docker_dir, "Dockerfile"), "w").write("FROM node:20-slim\n")
    per_board = os.path.join(docker_dir, "demo.Dockerfile")
    open(per_board, "w").write("FROM python:3\n")

    argvs = []
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda argv, **k: argvs.append(argv)
                        or type("R", (), {"returncode": 0})())
    import io
    assert orch._build_docker_image(kanban, "demo", io.StringIO()) is True
    argv = argvs[0]
    assert per_board in argv
    assert argv[argv.index("-f") + 1] == per_board


def test_build_docker_image_skips_when_no_per_board(kanban):
    docker_dir = os.path.join(kanban, "_orchestrator", "docker")
    os.makedirs(docker_dir, exist_ok=True)
    # Only the generic template -> no per-board Dockerfile -> no build.
    open(os.path.join(docker_dir, "Dockerfile"), "w").write("FROM node:20-slim\n")
    import io
    assert orch._build_docker_image(kanban, "demo", io.StringIO()) is False


# --- dispatch is blocked (not spawned) when per-board Dockerfile missing -----

def _docker_meta(kanban, use_docker=True):
    with open(os.path.join(kanban, "demo", "_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"project": "Demo", "useDocker": use_docker}, f)
    os.makedirs(os.path.join(kanban, "_orchestrator", "docker"), exist_ok=True)


def test_dispatch_one_blocks_when_dockerfile_missing(kanban, monkeypatch):
    _docker_meta(kanban)  # useDocker on, no per-board Dockerfile

    spawned = []
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **k: spawned.append(a) or {})

    task = {"id": "1", "title": "x", "detail": "", "status": "ready",
            "_board": "demo", "_path": os.path.join(kanban, "demo", "1.json")}
    dispatched = orch._dispatch_one(kanban, task,
                                    {"name": "g", "systemPrompt": "p"}, "m")

    assert dispatched is False
    assert spawned == []  # never spawned
    assert task["status"] == "blocked"
    q = oc.get_marker(task).get("question")
    assert q and q["type"] == "input"
    assert "demo.Dockerfile" in q["prompt"]
    assert q["answer"] is None
    # Persisted to disk.
    saved = json.load(open(task["_path"], encoding="utf-8"))
    assert saved["status"] == "blocked"
    assert saved["orchestrator"]["question"]["prompt"]


def test_dispatch_one_spawns_when_dockerfile_present(kanban, monkeypatch):
    _docker_meta(kanban)
    open(os.path.join(kanban, "_orchestrator", "docker", "demo.Dockerfile"),
         "w").write("FROM python:3\n")

    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **k: {"state": "dispatched", "pid": 1,
                                         "sessionId": "s", "cwd": "c",
                                         "logFile": "l"})
    task = {"id": "1", "title": "x", "detail": "", "status": "ready",
            "_board": "demo", "_path": os.path.join(kanban, "demo", "1.json")}
    dispatched = orch._dispatch_one(kanban, task,
                                    {"name": "g", "systemPrompt": "p"}, "m")
    assert dispatched is True
    assert task["status"] == "in_progress"


def test_dispatch_one_non_docker_spawns(kanban, monkeypatch):
    # A non-docker board is never subject to the preflight.
    monkeypatch.setattr(orch, "spawn_agent",
                        lambda *a, **k: {"state": "dispatched", "pid": 1})
    task = {"id": "1", "title": "x", "detail": "", "status": "ready",
            "_board": "demo", "_path": os.path.join(kanban, "demo", "1.json")}
    assert orch._dispatch_one(kanban, task,
                              {"name": "g", "systemPrompt": "p"}, "m") is True
    assert task["status"] == "in_progress"
