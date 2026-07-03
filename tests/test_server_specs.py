import os

import pytest

import kanban_server as ks


@pytest.fixture
def docs(kanban, monkeypatch):
    """Point the server at the temp kanban tree and seed docs/ with a spec.

    DOCS_DIR is derived from KANBAN_DIR at import time, so both must be patched.
    """
    monkeypatch.setattr(ks, "KANBAN_DIR", kanban)
    docs_dir = os.path.join(kanban, "docs")
    monkeypatch.setattr(ks, "DOCS_DIR", docs_dir)
    specs = os.path.join(docs_dir, "specs")
    plans = os.path.join(docs_dir, "plans")
    os.makedirs(specs)
    os.makedirs(plans)
    # A spec whose header points at board "demo", ticket 1.
    with open(os.path.join(specs, "feature.md"), "w", encoding="utf-8") as f:
        f.write("# Feature Design\n\n**Ticket:** `.kanban/demo/1.json`\n\nBody.\n")
    # A plan with no ticket ref — must NOT be auto-attached to anything.
    with open(os.path.join(plans, "loose.md"), "w", encoding="utf-8") as f:
        f.write("# Loose Plan\n\nNo ticket reference here.\n")
    return kanban


def test_index_discovers_spec_by_ticket_ref(docs):
    idx = ks.build_spec_index()
    assert "demo/1" in idx
    entry = idx["demo/1"][0]
    assert entry["kind"] == "spec"
    assert entry["title"] == "Feature Design"
    assert entry["path"] == "docs/specs/feature.md"


def test_docless_ticket_gets_nothing(docs):
    idx = ks.build_spec_index()
    task = {"id": "2"}
    ks.attach_specs(task, "demo", idx)
    assert "_specs" not in task


def test_attach_specs_auto(docs):
    idx = ks.build_spec_index()
    task = {"id": "1"}
    ks.attach_specs(task, "demo", idx)
    assert [s["path"] for s in task["_specs"]] == ["docs/specs/feature.md"]


def test_attach_specs_explicit_ref_merges_and_dedupes(docs):
    idx = ks.build_spec_index()
    # Explicit ref to the same doc (with .kanban/ prefix) must not duplicate.
    task = {"id": "1", "spec": ".kanban/docs/specs/feature.md"}
    ks.attach_specs(task, "demo", idx)
    assert len(task["_specs"]) == 1


def test_read_doc_serves_markdown(docs):
    text, status = ks.read_doc("docs/specs/feature.md")
    assert status == 200
    assert "Feature Design" in text


def test_read_doc_rejects_traversal(docs):
    # Escaping the docs/ tree is forbidden.
    assert ks.read_doc("../demo/1.json")[1] == 403
    assert ks.read_doc("../../etc/passwd")[1] == 403


def test_read_doc_missing(docs):
    assert ks.read_doc("docs/specs/nope.md")[1] == 404
