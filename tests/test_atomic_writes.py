"""Ticket #41: write_ticket and touch_meta must write atomically.

A concurrent reader must never observe a half-written ticket or _meta.json.
We prove atomicity by interrupting the write mid-stream (json.dump raises) and
asserting the original file is left fully intact and parseable — the behaviour
os.replace gives us but in-place mode-'w' truncation does not.
"""
import json
import os

import pytest

import kanban_server as ks


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_write_ticket_is_atomic_on_failure(kanban):
    path = os.path.join(kanban, "boards", "demo", "1.json")
    original = _read(path)

    def boom(*a, **k):
        raise RuntimeError("interrupted mid-write")

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(ks.json, "dump", boom)
        with pytest.raises(RuntimeError):
            ks.write_ticket(path, {"id": "1", "title": "New", "status": "ready"})

    # The reader never sees a torn file: original survives intact and parses.
    assert _read(path) == original
    assert not os.path.exists(path + ".tmp")


def test_touch_meta_is_atomic_on_failure(kanban):
    path = os.path.join(kanban, "boards", "demo", "_meta.json")
    original = _read(path)

    real_dump = ks.json.dump

    def boom(*a, **k):
        raise RuntimeError("interrupted mid-write")

    with pytest.MonkeyPatch().context() as mp:
        # Only the write half should blow up; the read half uses json.load.
        mp.setattr(ks.json, "dump", boom)
        with pytest.raises(RuntimeError):
            ks.touch_meta(os.path.join(kanban, "boards", "demo"))

    assert _read(path) == original
    assert not os.path.exists(path + ".tmp")


def test_write_ticket_persists_via_replace(kanban):
    path = os.path.join(kanban, "boards", "demo", "1.json")
    ks.write_ticket(path, {"id": "1", "title": "Done", "status": "completed"})
    assert _read(path)["status"] == "completed"
    assert not os.path.exists(path + ".tmp")
