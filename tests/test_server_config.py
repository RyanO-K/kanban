import json
import os

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
