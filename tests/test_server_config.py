import json
import os

import pytest

import kanban_server as ks


def _write_cfg(root, data):
    orch = os.path.join(root, "_orchestrator")
    os.makedirs(orch, exist_ok=True)
    path = os.path.join(orch, "server.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def test_defaults_when_no_file(tmp_path):
    cfg = ks.load_server_config(os.path.join(str(tmp_path), "missing.json"))
    assert cfg == {"host": "127.0.0.1", "port": ks.PORT}


def test_reads_host_and_port(tmp_path):
    path = _write_cfg(str(tmp_path), {"host": "0.0.0.0", "port": 9000})
    cfg = ks.load_server_config(path)
    assert cfg == {"host": "0.0.0.0", "port": 9000}


def test_partial_file_falls_back_per_field(tmp_path):
    path = _write_cfg(str(tmp_path), {"port": 9100})
    cfg = ks.load_server_config(path)
    assert cfg == {"host": "127.0.0.1", "port": 9100}


def test_malformed_json_falls_back_to_defaults(tmp_path):
    path = os.path.join(str(tmp_path), "server.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not valid json")
    cfg = ks.load_server_config(path)
    assert cfg == {"host": "127.0.0.1", "port": ks.PORT}


def test_invalid_types_fall_back_per_field(tmp_path):
    path = _write_cfg(str(tmp_path), {"host": "   ", "port": "not-a-number"})
    cfg = ks.load_server_config(path)
    assert cfg == {"host": "127.0.0.1", "port": ks.PORT}


# --- CPU cap endpoints (Setup-tab adjustable, GET/PUT /api/server/config) ----


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KANBAN_CPU_LIMIT", raising=False)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point SERVER_CONFIG_PATH at a temp file and stub the live kernel cap so
    the test runner is never assigned to a real Job Object."""
    path = os.path.join(str(tmp_path), "server.json")
    monkeypatch.setattr(ks, "SERVER_CONFIG_PATH", path)
    import cpu_limiter
    monkeypatch.setattr(cpu_limiter, "set_cpu_limit", lambda p: True)
    return path


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_config_get_reports_raw_and_effective(cfg):
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"host": "127.0.0.1", "port": 8745, "cpuLimitPercent": 10}, f)
    body, status = ks.server_config_get()
    assert status == 200
    assert body == {"cpuLimitPercent": 10, "effectivePercent": 10, "envOverride": False}


def test_config_get_default_when_key_absent(cfg):
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"host": "127.0.0.1", "port": 8745}, f)
    body, _ = ks.server_config_get()
    assert body["cpuLimitPercent"] is None   # not set in the file
    assert body["effectivePercent"] == 5     # resolves to the built-in default


def test_config_put_persists_and_preserves_other_keys(cfg):
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"host": "127.0.0.1", "port": 9000, "cpuLimitPercent": 5}, f)
    body, status = ks.server_config_put({"cpuLimitPercent": 25})
    assert status == 200
    assert body["cpuLimitPercent"] == 25 and body["applied"] is True
    assert _read(cfg) == {"host": "127.0.0.1", "port": 9000, "cpuLimitPercent": 25}


def test_config_put_clamps_range(cfg):
    body, status = ks.server_config_put({"cpuLimitPercent": 250})
    assert status == 200 and body["cpuLimitPercent"] == 100
    body, status = ks.server_config_put({"cpuLimitPercent": -5})
    assert status == 200 and body["cpuLimitPercent"] == 0


def test_config_put_rejects_bad_payloads(cfg):
    _, status = ks.server_config_put({})
    assert status == 400
    _, status = ks.server_config_put({"cpuLimitPercent": "fast"})
    assert status == 400


def test_config_put_flags_env_override(cfg, monkeypatch):
    monkeypatch.setenv("KANBAN_CPU_LIMIT", "70")
    body, status = ks.server_config_put({"cpuLimitPercent": 25})
    assert status == 200
    assert _read(cfg)["cpuLimitPercent"] == 25   # edit is still persisted
    assert body["effectivePercent"] == 70        # ...but the env var wins
    assert body["envOverride"] is True
