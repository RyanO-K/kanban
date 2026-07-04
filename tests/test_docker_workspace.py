"""Ticket #16: Docker dev-workspace per repo (Option A).

The orchestrator can build one Docker image per board repo and run the dispatched
`claude -p` agent INSIDE a container, with per-board editable env vars supplied
via `--env-file`. This covers:
  1. The pure core logic (flags, env sanitizing/rendering, Docker naming, path
     translation, argv builders) in orchestrator_core.
  2. The spawn_agent wiring + container teardown in orchestrator.
  3. The server exposing/sanitizing the `useDocker` + `envVars` board-meta fields.
"""

import io
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


def test_docker_run_argv_interactive_adds_stdin_flag():
    # Agent chat needs the container's stdin attached to the docker run client.
    argv = oc.docker_run_argv("img", "c", "/r", None, ["claude"], interactive=True)
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    # Default stays non-interactive (legacy shape untouched).
    argv2 = oc.docker_run_argv("img", "c", "/r", None, ["claude"])
    assert "-i" not in argv2


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
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    builds = []

    class FakeBuild:
        returncode = 0

    monkeypatch.setattr(orch.subprocess, "run",
                        lambda argv, **k: builds.append(argv) or FakeBuild())

    captured = {}

    class FakeProc:
        pid = 7777

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, **kw):
        captured["cmd"] = cmd
        captured["proc"] = FakeProc()
        return captured["proc"]

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
    # The inner claude invocation runs after the image tag; in chat mode the
    # prompt travels via stdin (host paths translated onto the /workspace
    # mount) rather than argv.
    tag = oc.docker_image_tag("demo")
    inner = cmd[cmd.index(tag) + 1:]
    assert inner[0] == "claude" and inner[1] == "-p"
    text = json.loads(captured["proc"].stdin.getvalue().decode("utf-8")
                      .splitlines()[0])["message"]["content"][0]["text"]
    assert "/workspace" in text
    assert kanban not in text


def test_spawn_agent_docker_forwards_api_key(kanban, monkeypatch):
    _docker_board(kanban)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))
    captured = {}

    class FakeProc:
        pid = 7778
        stdin = io.BytesIO()

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
        stdin = io.BytesIO()

        def poll(self):
            return None

    captured = {}

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))
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


# --- passthroughEnv: name-only secret forwarding (ticket #7) -----------------
#
# A board forwards non-Anthropic secrets (GitHub token, DATABASE_URL, etc.) into
# its container by NAME only: names live on disk in `passthroughEnv`, values only
# in the orchestrator's own environment (forwarded with bare `docker run -e NAME`).

def test_board_passthrough_env_keeps_valid_names_deduped():
    meta = {"passthroughEnv": ["GITHUB_TOKEN", "DATABASE_URL", "GITHUB_TOKEN",
                               "1bad", "bad-key", "has space", "", None, 5,
                               "_OK1"]}
    # Valid names only, order preserved, duplicates removed.
    assert oc.board_passthrough_env(meta) == [
        "GITHUB_TOKEN", "DATABASE_URL", "_OK1"]


def test_board_passthrough_env_non_list_is_empty():
    assert oc.board_passthrough_env({"passthroughEnv": "GITHUB_TOKEN"}) == []
    assert oc.board_passthrough_env({"passthroughEnv": {"A": 1}}) == []
    assert oc.board_passthrough_env({}) == []
    assert oc.board_passthrough_env(None) == []


def test_docker_dispatch_passthrough_ordering_and_warning(kanban, monkeypatch):
    # Board lists three secret NAMES; ANTHROPIC_API_KEY is also an Anthropic
    # default (must dedupe), GITHUB_TOKEN is present, MISSING_SECRET is absent.
    import io
    meta = {"project": "Demo", "useDocker": True,
            "passthroughEnv": ["GITHUB_TOKEN", "ANTHROPIC_API_KEY",
                               "MISSING_SECRET"]}
    docker_dir = os.path.join(kanban, "_orchestrator", "docker")
    os.makedirs(docker_dir, exist_ok=True)
    open(os.path.join(docker_dir, "demo.Dockerfile"), "w").write("FROM python:3\n")

    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    for absent in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                   "CLAUDE_CODE_OAUTH_TOKEN", "MISSING_SECRET"):
        monkeypatch.delenv(absent, raising=False)

    log = io.StringIO()
    cmd, _cname, _inner = orch._docker_dispatch(kanban, "demo", {"id": "1"}, meta,
                                                "prompt", "sess", "m", None, log)
    forwarded = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    # Anthropic defaults first, then the board's present secret; the duplicate
    # ANTHROPIC_API_KEY appears once; the absent MISSING_SECRET is not forwarded.
    assert forwarded == ["ANTHROPIC_API_KEY", "GITHUB_TOKEN"]
    # A value is never echoed into the argv, only the name.
    assert "ghp-secret" not in cmd and "sk-test" not in cmd
    # The listed-but-absent secret is logged as a visible warning, not dropped
    # silently.
    assert "MISSING_SECRET" in log.getvalue()


def test_spawn_agent_docker_forwards_passthrough_env(kanban, monkeypatch):
    with open(os.path.join(kanban, "demo", "_meta.json"), "w",
              encoding="utf-8") as f:
        json.dump({"project": "Demo", "useDocker": True,
                   "passthroughEnv": ["GITHUB_TOKEN"]}, f)
    docker_dir = os.path.join(kanban, "_orchestrator", "docker")
    os.makedirs(docker_dir, exist_ok=True)
    open(os.path.join(docker_dir, "demo.Dockerfile"), "w").write("FROM python:3\n")

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))
    captured = {}

    class FakeProc:
        pid = 7779
        stdin = io.BytesIO()

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
    assert "GITHUB_TOKEN" in cmd       # forwarded by name
    assert "ghp-secret" not in cmd     # value never written into the argv


def test_server_persists_passthrough_env(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks.update_board_meta("demo", {"passthroughEnv": [
        "GITHUB_TOKEN", "DATABASE_URL", "bad-key", "GITHUB_TOKEN"]})
    meta = json.load(open(os.path.join(kanban, "demo", "_meta.json"),
                          encoding="utf-8"))
    assert meta["passthroughEnv"] == ["GITHUB_TOKEN", "DATABASE_URL"]

    data, status = ks.load_board("demo")
    assert status == 200
    assert data["passthroughEnv"] == ["GITHUB_TOKEN", "DATABASE_URL"]


def test_server_passthrough_env_accepts_newline_string(kanban, monkeypatch):
    # The Project Settings textarea submits one name per line.
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks.update_board_meta("demo", {
        "passthroughEnv": "GITHUB_TOKEN\nDATABASE_URL\n\nbad-key\n"})
    meta = json.load(open(os.path.join(kanban, "demo", "_meta.json"),
                          encoding="utf-8"))
    assert meta["passthroughEnv"] == ["GITHUB_TOKEN", "DATABASE_URL"]


def test_server_empty_passthrough_env_removes_field(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    ks.update_board_meta("demo", {"passthroughEnv": ["GITHUB_TOKEN"]})
    ks.update_board_meta("demo", {"passthroughEnv": []})
    meta = json.load(open(os.path.join(kanban, "demo", "_meta.json"),
                          encoding="utf-8"))
    assert "passthroughEnv" not in meta


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


# --- git works inside the container (ticket #8) ------------------------------
#
# A bind-mounted repo is owned by the host user, so git inside the container trips
# its dubious-ownership guard ("detected dubious ownership"); the fresh container
# also carries no commit identity. Both break in-container `git commit` / worktree
# creation on the shared volume. The Dockerfile template must clear both. Push
# stays host-side (the orchestrator auto-commits/pushes after reap), so the
# container only needs to make local commits.

def _dockerfile_template_text():
    # orchestrator_core.py lives at the repo root; the template sits beside it
    # under _orchestrator/docker/Dockerfile.
    root = os.path.dirname(os.path.abspath(oc.__file__))
    path = os.path.join(root, "_orchestrator", "docker", "Dockerfile")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_dockerfile_template_trusts_bind_mounted_repos():
    # Without `safe.directory '*'`, git refuses to operate on the host-owned,
    # bind-mounted repo and in-container commits fail with 'dubious ownership'.
    text = _dockerfile_template_text()
    assert "safe.directory" in text
    assert "'*'" in text or '"*"' in text


def test_dockerfile_template_sets_fallback_git_identity():
    # A fallback user.name / user.email so `git commit` never aborts for lack of
    # an identity in the fresh container (boards may override via passthroughEnv).
    text = _dockerfile_template_text()
    assert "user.name" in text
    assert "user.email" in text


# --- Agent chat in docker mode (spec 2026-07-03) -----------------------------


def test_spawn_agent_docker_chat_streams_translated_prompt(kanban, monkeypatch):
    """Docker chat dispatch: `docker run -i`, inner claude gets
    --input-format stream-json with NO prompt argv, and the first stdin
    message carries the host-path-TRANSLATED prompt."""
    _docker_board(kanban)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(orch, "_start_chat_pump", lambda *a, **k: None)
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    captured = {}

    class FakeProc:
        pid = 7779

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        captured["stdin"] = k.get("stdin")
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")

    cmd = captured["cmd"]
    assert cmd[:4] == ["docker", "run", "--rm", "-i"], \
        "docker chat mode must keep stdin attached with -i"
    # Inner claude reads streaming input; the prompt is NOT in argv.
    tag = oc.docker_image_tag("demo")
    inner = cmd[cmd.index(tag) + 1:]
    assert inner[0] == "claude" and inner[1] == "-p"
    assert inner[2:4] == ["--input-format", "stream-json"]
    assert not any(kanban in str(a) for a in inner), \
        "no host path (i.e. no prompt) may remain in the inner argv"
    # The first stdin message is the TRANSLATED prompt (host paths -> /workspace).
    assert captured["stdin"] is orch.subprocess.PIPE
    raw = captured["proc"].stdin.getvalue().decode("utf-8")
    text = json.loads(raw.splitlines()[0])["message"]["content"][0]["text"]
    assert "/workspace" in text
    assert kanban not in text


def test_spawn_agent_docker_chat_disabled_keeps_legacy_inner_cmd(kanban, monkeypatch):
    """CHAT_ENABLED=False: docker dispatch is byte-for-byte the legacy form —
    prompt in the inner argv, no -i, no stdin pipe, no pump."""
    _docker_board(kanban)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(orch.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(oc, "CHAT_ENABLED", False)
    pumps = []
    monkeypatch.setattr(orch, "_start_chat_pump",
                        lambda *a, **k: pumps.append(a))
    monkeypatch.setattr(oc, "CHAT_DIR", os.path.join(kanban, "_orchestrator", "chat"))

    captured = {}

    class FakeProc:
        pid = 7780

        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        captured["stdin"] = k.get("stdin")
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(orch.subprocess, "Popen", fake_popen)
    task = {"id": "1", "title": "x", "detail": "", "_board": "demo",
            "_path": os.path.join(kanban, "demo", "1.json")}
    orch.spawn_agent(kanban, "demo", task, {"name": "g", "systemPrompt": "p"}, "m")

    cmd = captured["cmd"]
    assert "-i" not in cmd
    tag = oc.docker_image_tag("demo")
    inner = cmd[cmd.index(tag) + 1:]
    assert inner[0] == "claude" and inner[1] == "-p"
    assert "/workspace" in inner[2], "legacy form keeps the translated prompt in argv"
    assert "--input-format" not in inner
    assert captured["stdin"] is None
    assert captured["proc"].stdin.getvalue() == b""
    assert pumps == []
