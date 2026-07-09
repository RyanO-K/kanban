"""Ticket #101: a board poll must not wipe an in-progress ticket edit.

When any board change (a card moving, a comment posted, another ticket's
status flipping) bumps the board mtime, the UI's `poll()` calls
`refreshPanel()`, which re-runs `renderPanel()` and does
`body.innerHTML = html`. That destroys any open inline editor — the
description `<textarea>` opened by `startDetailEdit` or the title `<input>`
opened by `startTitleEdit` — silently discarding whatever the user was
typing. The reporter hit this while typing a long description: it was
wiped three times by unrelated board activity.

The fix: `refreshPanel()` must detect an in-progress inline edit and skip
the re-render (leaving the editor and its unsaved text untouched) rather
than tearing the panel down under the user. These tests assert the guard
exists in the client source (the codebase's stdlib-only convention for
verifying kanban.js behaviour — see test_nudge.py).
"""
import os
import re

import sys
KANBAN_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, KANBAN_SRC)

import kanban_server as ks


def _js():
    with open(ks.JS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _refresh_panel_body():
    """Return the source of the refreshPanel() function body."""
    js = _js()
    m = re.search(r"function refreshPanel\(\)\{", js)
    assert m, "refreshPanel() not found in kanban.js"
    # Walk braces from the opening brace to find the matching close.
    start = m.end() - 1  # index of the '{'
    depth = 0
    for i in range(start, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start : i + 1]
    raise AssertionError("Could not find end of refreshPanel()")


def test_refresh_panel_skips_render_while_editing():
    """refreshPanel must bail out (no renderPanel) when an inline edit is open.

    Behaviour contract: before the normal-path renderPanel(t) call there must
    be a `return` that fires when an editor is open. The open-editor test may be
    inline or delegated to a helper (e.g. panelHasOpenEdit()); either way the
    guard call + return must sit ahead of renderPanel.
    """
    body = _refresh_panel_body()

    render_idx = body.find("renderPanel(")
    assert render_idx != -1, "refreshPanel should still call renderPanel in the normal path"

    guard = body[:render_idx]
    # The guard either checks the DOM inline, or calls an editing-state helper.
    detects_editor = re.search(
        r"querySelector\([\"'][^\"']*(?:textarea|input|sp-detail-edit|sp-title-input)",
        guard,
        re.IGNORECASE,
    ) or re.search(r"panelHasOpenEdit|isPanelEditing|panelEditing|isEditingPanel", guard)
    assert detects_editor, (
        "refreshPanel must check for an in-progress inline edit BEFORE re-rendering; "
        "no editor-detection guard found ahead of renderPanel()"
    )
    # The check must short-circuit the re-render.
    assert re.search(r"if\(.*?(?:panelHasOpenEdit|Editing|querySelector).*?\)\s*return", guard), (
        "refreshPanel must return early (skip the re-render) when an edit is in progress"
    )


def test_edit_guard_covers_description_textarea():
    """The guard must reach the description editor (the reported case).

    startDetailEdit gives its textarea the class "sp-detail-edit". Whether the
    detection is inline in refreshPanel or in a helper it calls, the selector for
    that textarea (or a generic textarea match) must exist in the client source.
    """
    js = _js()
    # The helper (or inline guard) that refreshPanel relies on must query for the
    # open description textarea. sp-detail-edit is the class startDetailEdit sets.
    assert re.search(r"querySelector\([^)]*textarea\.sp-detail-edit", js) or re.search(
        r"querySelector\([^)]*sp-detail-edit", js
    ), "the panel edit-detection must select the open description textarea (sp-detail-edit)"
