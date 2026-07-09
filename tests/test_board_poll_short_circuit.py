"""Tests for the GET /api/board/<slug>?since=<mtime> short-circuit.

Polling clients send the mtime they last saw; when nothing changed the server
answers {"unchanged": true} from file stats alone — no ticket JSON opens, no
spec-index rebuild — so idle polls stay invisible to on-access file scanning.
"""
import json
import os
import time

import kanban_server as ks


def test_board_get_unchanged_short_circuit(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    data, status = ks.board_get("demo")
    assert status == 200
    assert len(data["tasks"]) == 2
    m = data["mtime"]

    data2, status2 = ks.board_get("demo", str(m))
    assert status2 == 200
    assert data2.get("unchanged") is True
    assert data2["mtime"] == m
    assert "tasks" not in data2


def test_board_get_stale_since_returns_full_payload(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    data, _ = ks.board_get("demo")
    m = data["mtime"]

    ticket = os.path.join(kanban, "boards", "demo", "1.json")
    future = time.time() + 10
    os.utime(ticket, (future, future))

    data2, status2 = ks.board_get("demo", str(m))
    assert status2 == 200
    assert not data2.get("unchanged")
    assert len(data2["tasks"]) == 2
    assert data2["mtime"] != m


def test_board_get_all_slug_short_circuit(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    data, _ = ks.board_get(ks.ALL_SLUG)
    m = data["mtime"]
    data2, status2 = ks.board_get(ks.ALL_SLUG, str(m))
    assert status2 == 200
    assert data2.get("unchanged") is True


def test_board_get_doc_edit_invalidates(kanban, monkeypatch):
    """Spec/plan markdown edits are folded into the payload mtime, so a doc
    change breaks the short-circuit (and triggers a client re-render)."""
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    docs = os.path.join(kanban, "docs")
    os.makedirs(docs)
    doc = os.path.join(docs, "plan.md")
    with open(doc, "w", encoding="utf-8") as f:
        f.write("# Plan\n")

    data, _ = ks.board_get("demo")
    m = data["mtime"]
    assert ks.board_get("demo", str(m))[0].get("unchanged") is True

    future = time.time() + 10
    os.utime(doc, (future, future))
    data2, _ = ks.board_get("demo", str(m))
    assert not data2.get("unchanged")
    assert data2["mtime"] != m


def test_board_get_bad_since_falls_back_to_full_load(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    data, status = ks.board_get("demo", "not-a-float")
    assert status == 200
    assert len(data["tasks"]) == 2


def test_board_get_unknown_board_still_404s_with_since(kanban, monkeypatch):
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    data, status = ks.board_get("nope", "123.0")
    assert status == 404


def test_snapshot_mtime_matches_payload_mtime(kanban, monkeypatch):
    """board_snapshot_mtime must stay in lockstep with load_board's mtime,
    else the short-circuit would never (or always) fire."""
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    for slug in ("demo", ks.ALL_SLUG):
        payload, _ = ks.load_board(slug)
        assert ks.board_snapshot_mtime(slug) == payload["mtime"]
