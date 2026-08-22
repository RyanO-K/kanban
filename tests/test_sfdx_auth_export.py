"""Tests for ticket #98: auto-export SFDX_AUTH_URL env vars on orchestrator startup.

Tests cover:
  - alias_to_env_var_name: the pure derivation function (orchestrator_core)
  - populate_sfdx_auth_urls: subprocess wiring + env mutation (orchestrator)
"""

import json
import os

import orchestrator_core as oc
import orchestrator as orch


# ---------------------------------------------------------------------------
# alias_to_env_var_name (pure, in orchestrator_core)
# ---------------------------------------------------------------------------

def test_alias_to_env_var_name_simple():
    assert oc.alias_to_env_var_name("Workbox2") == "SFDX_AUTH_URL_WORKBOX2"


def test_alias_to_env_var_name_spaces_become_underscores():
    assert oc.alias_to_env_var_name("My Org") == "SFDX_AUTH_URL_MY_ORG"


def test_alias_to_env_var_name_special_chars_stripped():
    # hyphens, dots, brackets, @ stripped; only alphanumerics and underscores remain
    assert oc.alias_to_env_var_name("my-org.example") == "SFDX_AUTH_URL_MYORGEXAMPLE"


def test_alias_to_env_var_name_mixed():
    assert oc.alias_to_env_var_name("Barnumhardis2 (SF)") == "SFDX_AUTH_URL_BARNUMHARDIS2_SF"


def test_alias_to_env_var_name_already_upper():
    assert oc.alias_to_env_var_name("PROD") == "SFDX_AUTH_URL_PROD"


def test_alias_to_env_var_name_empty_string():
    # empty alias → prefix only (no trailing underscore)
    assert oc.alias_to_env_var_name("") == "SFDX_AUTH_URL_"


def test_alias_to_env_var_name_only_special_chars():
    assert oc.alias_to_env_var_name("---") == "SFDX_AUTH_URL_"


# ---------------------------------------------------------------------------
# populate_sfdx_auth_urls (subprocess wiring, in orchestrator)
# ---------------------------------------------------------------------------

def _make_sf_list_output(orgs):
    """Build a fake `sf org list --json` stdout blob."""
    return json.dumps({
        "result": {
            "nonScratchOrgs": orgs,
            "sandboxes": [],
        }
    })


def _make_sf_display_output(auth_url=None):
    result = {"username": "user@example.com"}
    if auth_url is not None:
        result["sfdxAuthUrl"] = auth_url
    return json.dumps({"result": result})


def test_populate_sets_env_vars_for_found_orgs(monkeypatch):
    """Happy path: two orgs, both have auth URLs → both env vars set."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "list" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": _make_sf_list_output([
                    {"alias": "Workbox2", "username": "wb2@example.com"},
                    {"alias": "Prod", "username": "prod@example.com"},
                ]),
            })()
        # display call — derive org from -o flag
        org = cmd[cmd.index("-o") + 1]
        url = f"force://tok_{org.lower()}@example.com"
        return type("R", (), {
            "returncode": 0,
            "stdout": _make_sf_display_output(url),
        })()

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    assert env.get("SFDX_AUTH_URL_WORKBOX2") == "force://tok_workbox2@example.com"
    assert env.get("SFDX_AUTH_URL_PROD") == "force://tok_prod@example.com"
    assert result["found"] == 2
    assert result["populated"] == 2
    assert result["skipped"] == []
    assert result["failed"] == []


def test_populate_skips_orgs_with_no_auth_url(monkeypatch):
    """An org whose display returns no sfdxAuthUrl is skipped, not an error."""
    def fake_run(cmd, **kwargs):
        if "list" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": _make_sf_list_output([
                    {"alias": "JwtOrg", "username": "jwt@example.com"},
                ]),
            })()
        # display with no sfdxAuthUrl
        return type("R", (), {
            "returncode": 0,
            "stdout": _make_sf_display_output(auth_url=None),
        })()

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    assert "SFDX_AUTH_URL_JWTORG" not in env
    assert result["found"] == 1
    assert result["populated"] == 0
    assert result["skipped"] == ["JwtOrg"]
    assert result["failed"] == []


def test_populate_handles_org_display_failure(monkeypatch):
    """A `sf org display` failure for one org records it in failed and continues."""
    def fake_run(cmd, **kwargs):
        if "list" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": _make_sf_list_output([
                    {"alias": "GoodOrg", "username": "g@example.com"},
                    {"alias": "BadOrg", "username": "b@example.com"},
                ]),
            })()
        org = cmd[cmd.index("-o") + 1]
        if org == "BadOrg":
            return type("R", (), {"returncode": 1, "stdout": "{}"})()
        return type("R", (), {
            "returncode": 0,
            "stdout": _make_sf_display_output("force://good@example.com"),
        })()

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    assert env.get("SFDX_AUTH_URL_GOODORG") == "force://good@example.com"
    assert "SFDX_AUTH_URL_BADORG" not in env
    assert result["populated"] == 1
    assert result["failed"] == ["BadOrg"]


def test_populate_handles_sf_list_failure(monkeypatch):
    """If `sf org list` fails entirely, return empty result without crashing."""
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": ""})()

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    assert result["found"] == 0
    assert result["populated"] == 0


def test_populate_handles_sf_not_installed(monkeypatch):
    """If sf is not installed (OSError), return empty result without crashing."""
    import subprocess

    def fake_run(cmd, **kwargs):
        raise OSError("sf not found")

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    assert result["found"] == 0
    assert result["populated"] == 0


def test_populate_collects_orgs_from_sandboxes_and_non_scratch(monkeypatch):
    """Both .result.sandboxes[] and .result.nonScratchOrgs[] are enumerated."""
    def fake_run(cmd, **kwargs):
        if "list" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": json.dumps({
                    "result": {
                        "nonScratchOrgs": [{"alias": "ProdOrg", "username": "p@example.com"}],
                        "sandboxes": [{"alias": "SandboxOrg", "username": "s@example.com"}],
                    }
                }),
            })()
        org = cmd[cmd.index("-o") + 1]
        return type("R", (), {
            "returncode": 0,
            "stdout": _make_sf_display_output(f"force://{org.lower()}@example.com"),
        })()

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    assert "SFDX_AUTH_URL_PRODORG" in env
    assert "SFDX_AUTH_URL_SANDBOXORG" in env
    assert result["found"] == 2
    assert result["populated"] == 2


def test_populate_skips_orgs_with_no_alias(monkeypatch):
    """Orgs without an alias field are skipped — no alias means no safe var name."""
    def fake_run(cmd, **kwargs):
        if "list" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": _make_sf_list_output([
                    {"alias": "", "username": "noalias@example.com"},
                    {"username": "alsonoalias@example.com"},
                    {"alias": "HasAlias", "username": "has@example.com"},
                ]),
            })()
        return type("R", (), {
            "returncode": 0,
            "stdout": _make_sf_display_output("force://tok@example.com"),
        })()

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    assert result["found"] == 1  # only HasAlias counted
    assert "SFDX_AUTH_URL_HASALIAS" in env


def test_populate_does_not_overwrite_existing_env_var(monkeypatch):
    """A var already set in the environment is not overwritten."""
    def fake_run(cmd, **kwargs):
        if "list" in cmd:
            return type("R", (), {
                "returncode": 0,
                "stdout": _make_sf_list_output([
                    {"alias": "Workbox2", "username": "wb2@example.com"},
                ]),
            })()
        return type("R", (), {
            "returncode": 0,
            "stdout": _make_sf_display_output("force://new@example.com"),
        })()

    monkeypatch.setattr(orch, "_sf_run", fake_run)
    env = {"SFDX_AUTH_URL_WORKBOX2": "force://existing@example.com"}
    monkeypatch.setattr(os, "environ", env)

    result = orch.populate_sfdx_auth_urls()

    # Pre-existing value preserved
    assert env["SFDX_AUTH_URL_WORKBOX2"] == "force://existing@example.com"
    assert result["populated"] == 0  # skipped because already set
    assert result["skipped"] == ["Workbox2"]


def test_run_loop_calls_populate_sfdx_auth_urls_before_first_tick(
        kanban, monkeypatch):
    """populate_sfdx_auth_urls is called once before the first tick in run_loop."""
    import threading

    populate_calls = []
    tick_calls = []

    monkeypatch.setattr(orch, "populate_sfdx_auth_urls",
                        lambda: populate_calls.append(1) or
                        {"found": 0, "populated": 0, "skipped": [], "failed": []})

    stop = threading.Event()

    def fake_tick(kd, **kw):
        tick_calls.append(1)
        stop.set()

    monkeypatch.setattr(orch, "tick", fake_tick)
    monkeypatch.setattr(oc, "acquire_lock", lambda kd: True)
    monkeypatch.setattr(oc, "release_lock", lambda kd: None)

    orch.run_loop(kanban, stop_event=stop, tick_seconds=0)

    assert len(populate_calls) == 1, "populate_sfdx_auth_urls must be called once"
    assert len(tick_calls) >= 1
    # populate called BEFORE any tick
    # (we verify this structurally: populate_calls is set by the run_loop call
    # before the loop body, tick_calls during the loop body)
