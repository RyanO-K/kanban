"""Kernel-level CPU cap for the kanban server (Windows Job Object hard cap).

The server assigns *itself* to a Windows Job Object configured with
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP, so the NT scheduler — not any polling
loop in userspace — throttles the process. The cap covers the server process
only: the orchestrator tick loop and perf sampler run as threads inside it
and are therefore capped, but the job also sets
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK so every child process (notably the
`claude` agents the orchestrator dispatches) automatically breaks away from
the job at spawn and runs uncapped.

`CpuRate` is expressed in 1/100ths of a percent of TOTAL system CPU across
all logical processors: a 50% cap on an 8-core box allows the whole tree the
equivalent of 4 cores.

Resolution of the cap (most explicit wins):
  KANBAN_CPU_LIMIT env var  >  server.json `cpuLimitPercent`  >  default (5)
A value of 0 disables the cap entirely; anything unparsable falls back to the
default so a bad config can never leave the server uncapped by accident.

Non-Windows platforms are a no-op (returns False) — use cgroups/systemd
`CPUQuota=` there instead.
"""

import json
import os
import sys

DEFAULT_LIMIT_PERCENT = 5

# Keep the job handle alive for the life of the process. The job itself lives
# as long as any process is assigned to it, but holding the handle makes the
# ownership explicit and lets callers inspect it.
_job_handle = None


def resolve_limit_percent(config_path=None):
    """Return the effective CPU cap percent (int 1-100), or None if disabled.

    Precedence: KANBAN_CPU_LIMIT env > server.json cpuLimitPercent > default.
    0 (or negative) at either level means "disabled". Malformed values fall
    through to the next level rather than disabling the cap.
    """
    candidates = []
    env = os.environ.get("KANBAN_CPU_LIMIT")
    if env is not None:
        candidates.append(env)
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "cpuLimitPercent" in data:
                candidates.append(data["cpuLimitPercent"])
        except (OSError, ValueError):
            pass
    candidates.append(DEFAULT_LIMIT_PERCENT)

    for value in candidates:
        try:
            pct = int(value)
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            return None
        return min(pct, 100)
    return None


def apply_cpu_limit(percent):
    """Cap this process (and all children) at `percent` of total system CPU.

    Returns True if the kernel cap is in place, False otherwise (non-Windows,
    disabled, or an OS call failed — the server should run uncapped rather
    than refuse to start).
    """
    global _job_handle
    if not percent:
        return False
    if sys.platform != "win32":
        return False
    if _job_handle is not None:  # already applied (idempotent)
        return True

    import ctypes
    from ctypes import wintypes

    JobObjectExtendedLimitInformation = 9
    JobObjectCpuRateControlInformation = 15
    JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
    JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
    JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000

    ULONG_PTR = ctypes.c_size_t

    class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("ControlFlags", wintypes.DWORD),
            ("CpuRate", wintypes.DWORD),  # 1/100ths of a percent of total CPU
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Declare prototypes explicitly: ctypes' default c_int return type
    # truncates 64-bit HANDLEs, yielding an invalid handle on x64.
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False

    info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(
        ControlFlags=JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
        | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
        CpuRate=int(percent) * 100,
    )
    ok = kernel32.SetInformationJobObject(
        job,
        JobObjectCpuRateControlInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if ok:
        # Silent breakaway: children (dispatched agents, git subprocesses)
        # never join the job, so only the server process itself is capped.
        ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        ok = kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(ext),
            ctypes.sizeof(ext),
        )
    # Requires Windows 8 / Server 2012+ for nested jobs, in case the process
    # is already inside a job (e.g. launched from a CI runner or terminal
    # that uses one).
    if ok:
        ok = kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    if not ok:
        kernel32.CloseHandle(job)
        return False

    _job_handle = job
    return True
