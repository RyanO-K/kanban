"""Ticket #45: _run_git must have a timeout and suppress credential prompts.

A headless git push to an unreachable host (or a host with no cached creds) can
block forever on a credential prompt, wedging the orchestrator loop. The fix:
  1. Pass timeout= to subprocess.run so TimeoutExpired surfaces as a failure.
  2. Set GIT_TERMINAL_PROMPT=0 (and GIT_ASKPASS=echo) in the subprocess env so
     git never opens an interactive prompt.
"""

import os
import subprocess

import pytest

import orchestrator as orch


# ---------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------

def test_run_git_passes_timeout(monkeypatch):
    """_run_git must forward a timeout to subprocess.run."""
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    orch._run_git(["status"], cwd="/tmp", timeout=30)
    assert captured.get("timeout") == 30


def test_run_git_default_timeout(monkeypatch):
    """_run_git must apply a default timeout even when none is passed."""
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    orch._run_git(["status"], cwd="/tmp")
    assert captured.get("timeout") is not None, "_run_git must set a default timeout"


def test_run_git_timeout_raises_on_expiry(monkeypatch):
    """TimeoutExpired from subprocess.run must propagate (not be swallowed)."""
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        orch._run_git(["push", "origin", "main"], cwd="/tmp", timeout=1)


# ---------------------------------------------------------------------------
# non-interactive env
# ---------------------------------------------------------------------------

def test_run_git_sets_no_terminal_prompt(monkeypatch):
    """GIT_TERMINAL_PROMPT=0 must be injected so git never waits for creds."""
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    orch._run_git(["push", "origin", "main"], cwd="/tmp")
    env = captured.get("env") or {}
    assert env.get("GIT_TERMINAL_PROMPT") == "0", \
        "GIT_TERMINAL_PROMPT must be '0' to prevent interactive credential prompts"


def test_run_git_sets_askpass(monkeypatch):
    """GIT_ASKPASS must be set to a no-op so git can't open an askpass dialog."""
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    orch._run_git(["push", "origin", "main"], cwd="/tmp")
    env = captured.get("env") or {}
    assert "GIT_ASKPASS" in env, "GIT_ASKPASS must be set"


def test_run_git_env_inherits_parent(monkeypatch):
    """The injected env must include parent-process env vars (PATH etc.)."""
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    orch._run_git(["status"], cwd="/tmp")
    env = captured.get("env") or {}
    # PATH should be inherited from os.environ
    assert "PATH" in env or "Path" in env, \
        "injected env must inherit os.environ so git itself can be found"
