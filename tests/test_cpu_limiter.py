import json
import os
import subprocess
import sys

import pytest

import cpu_limiter

# cpu_limiter.py lives in app/ (conftest puts app/ on sys.path; subprocesses
# below run with app/ as cwd so their bare `import cpu_limiter` resolves too).
APP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"
)


def _write_cfg(tmp_path, data):
    path = os.path.join(str(tmp_path), "server.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KANBAN_CPU_LIMIT", raising=False)


def test_default_when_no_config():
    assert cpu_limiter.resolve_limit_percent(None) == cpu_limiter.DEFAULT_LIMIT_PERCENT


def test_config_value_used(tmp_path):
    path = _write_cfg(tmp_path, {"cpuLimitPercent": 30})
    assert cpu_limiter.resolve_limit_percent(path) == 30


def test_env_overrides_config(tmp_path, monkeypatch):
    path = _write_cfg(tmp_path, {"cpuLimitPercent": 30})
    monkeypatch.setenv("KANBAN_CPU_LIMIT", "70")
    assert cpu_limiter.resolve_limit_percent(path) == 70


def test_zero_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAN_CPU_LIMIT", "0")
    assert cpu_limiter.resolve_limit_percent(None) is None
    path = _write_cfg(tmp_path, {"cpuLimitPercent": 0})
    assert cpu_limiter.resolve_limit_percent(path) is None


def test_clamped_to_100(monkeypatch):
    monkeypatch.setenv("KANBAN_CPU_LIMIT", "250")
    assert cpu_limiter.resolve_limit_percent(None) == 100


def test_malformed_values_fall_through(tmp_path, monkeypatch):
    # Bad env falls through to config; bad config falls through to default.
    path = _write_cfg(tmp_path, {"cpuLimitPercent": 30})
    monkeypatch.setenv("KANBAN_CPU_LIMIT", "not-a-number")
    assert cpu_limiter.resolve_limit_percent(path) == 30
    path = _write_cfg(tmp_path, {"cpuLimitPercent": "nope"})
    assert cpu_limiter.resolve_limit_percent(path) == cpu_limiter.DEFAULT_LIMIT_PERCENT


def test_disabled_percent_is_noop():
    assert cpu_limiter.apply_cpu_limit(None) is False
    assert cpu_limiter.apply_cpu_limit(0) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects only")
def test_apply_in_child_process_and_children_break_away():
    # Apply the cap in a throwaway child so the test runner itself is never
    # assigned to a job object. The child then spawns a grandchild and checks
    # job membership via IsProcessInJob: the capped process must be in the
    # job; its children must NOT be (silent breakaway → agents run uncapped).
    code = """
import ctypes, subprocess, sys
from ctypes import wintypes
import cpu_limiter

assert cpu_limiter.apply_cpu_limit(50)
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.IsProcessInJob.restype = wintypes.BOOL
k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
k.GetCurrentProcess.restype = wintypes.HANDLE

def in_job(handle):
    flag = wintypes.BOOL()
    assert k.IsProcessInJob(handle, cpu_limiter._job_handle, ctypes.byref(flag))
    return bool(flag.value)

grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
try:
    print("self_in_job:", in_job(k.GetCurrentProcess()))
    print("child_in_job:", in_job(int(grandchild._handle)))
finally:
    grandchild.kill()
"""
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "self_in_job: True" in out.stdout, out.stderr
    assert "child_in_job: False" in out.stdout, out.stderr


def test_set_cpu_limit_noop_off_windows(monkeypatch):
    # No job handle + non-Windows -> nothing to do, reports False.
    monkeypatch.setattr(cpu_limiter, "_job_handle", None)
    monkeypatch.setattr(cpu_limiter.sys, "platform", "linux")
    assert cpu_limiter.set_cpu_limit(10) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects only")
def test_set_cpu_limit_live_updates_the_rate():
    # In a throwaway child (so the runner is never capped): create the job via
    # set_cpu_limit, then change the rate and disable it, reading the live
    # CpuRate/ControlFlags back off the job each time to prove the kernel state
    # actually changed without recreating the job.
    code = """
import ctypes, sys
from ctypes import wintypes
import cpu_limiter

class RATE(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.QueryInformationJobObject.restype = wintypes.BOOL
k.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]

def rate():
    info = RATE(); ret = wintypes.DWORD()
    assert k.QueryInformationJobObject(
        cpu_limiter._job_handle, 15, ctypes.byref(info), ctypes.sizeof(info),
        ctypes.byref(ret))
    return info.ControlFlags, info.CpuRate

assert cpu_limiter._job_handle is None
assert cpu_limiter.set_cpu_limit(50)   # creates the job (no handle yet)
print("create:", rate())               # (5, 5000)  enable|hardcap, 50%
assert cpu_limiter.set_cpu_limit(20)   # live update on the existing job
print("update:", rate())               # (5, 2000)
assert cpu_limiter.set_cpu_limit(0)    # disable rate control, stay in job
print("disable:", rate())              # (0, 0)
"""
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "create: (5, 5000)" in out.stdout, out.stderr
    assert "update: (5, 2000)" in out.stdout, out.stderr
    assert "disable: (0, 0)" in out.stdout, out.stderr
