"""Superpowers plugin gating for spawned claude sessions.

The superpowers plugin's SessionStart hook costs a process spawn (plus its
context-injection tokens) on every claude session. It is only worth that for
sonnet- and opus-tier ticket agents: haiku-tier agents and the orchestrator's
utility calls (triage, summarizer) get the plugin disabled via a
per-invocation `--settings` override.
"""
import json

import orchestrator_core as oc


def _is_disable_args(args):
    assert args[0] == "--settings"
    settings = json.loads(args[1])
    return settings == {"enabledPlugins": {oc.SUPERPOWERS_PLUGIN: False}}


def test_utility_calls_disable_superpowers():
    assert _is_disable_args(oc.superpowers_args("claude-opus-4-8", utility=True))
    assert _is_disable_args(oc.superpowers_args("claude-sonnet-4-6", utility=True))


def test_haiku_tier_tickets_disable_superpowers():
    assert _is_disable_args(oc.superpowers_args("claude-haiku-4-5-20251001"))
    assert _is_disable_args(oc.superpowers_args("haiku"))


def test_sonnet_and_opus_tier_tickets_keep_superpowers():
    assert oc.superpowers_args("claude-sonnet-4-6") == []
    assert oc.superpowers_args("claude-opus-4-8") == []
    assert oc.superpowers_args("claude-fable-5") == []


def test_unknown_or_missing_model_keeps_superpowers():
    # No model flag means the CLI default (sonnet tier or higher) applies.
    assert oc.superpowers_args(None) == []
    assert oc.superpowers_args("") == []
