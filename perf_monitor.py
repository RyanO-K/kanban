"""System-wide Claude session discovery + CPU/memory rollup.

Optional dependency on psutil. All public functions accept injectable
process iterators so tests never spawn real processes.
"""
import os
import re
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone

try:
    import psutil  # type: ignore
    PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via available flag
    psutil = None
    PSUTIL_AVAILABLE = False

_TICKET_RE = re.compile(r"[\\/]([^\\/]+)[\\/](\d+)\.json")


def classify_cmdline(cmdline):
    """Return (kind, board, ticket) for a claude process command line."""
    args = list(cmdline or [])
    kind = "headless" if "-p" in args else "interactive"
    board = ticket = None
    blob = "\n".join(args)
    m = _TICKET_RE.search(blob)
    if m:
        board, ticket = m.group(1), m.group(2)
    return kind, board, ticket


def _rollup(proc):
    """Sum cpu_percent and rss for proc + all descendants. Returns
    (cpu, mem_bytes, children_list)."""
    cpu = float(proc.cpu_percent())
    mem = float(proc.memory_info().rss)
    children = []
    try:
        kids = proc.children(recursive=True)
    except Exception:
        kids = []
    for k in kids:
        try:
            kcpu = float(k.cpu_percent())
            kmem = float(k.memory_info().rss)
        except Exception:
            continue
        cpu += kcpu
        mem += kmem
        children.append({
            "pid": k.pid,
            "name": k.name(),
            "cpuPercent": round(kcpu, 1),
            "memoryMB": round(kmem / (1024 * 1024), 1),
        })
    return cpu, mem, children


def discover_sessions(proc_iter=None, owned_pids=None):
    """Find every claude.exe process and roll up its subprocess tree."""
    if proc_iter is None:
        if not PSUTIL_AVAILABLE:
            return []
        proc_iter = psutil.process_iter
    owned = set(owned_pids or ())
    sessions = []
    for p in proc_iter():
        try:
            name = (p.name() or "").lower()
            if name != "claude.exe":
                continue
            kind, board, ticket = classify_cmdline(p.cmdline())
            cpu, mem, children = _rollup(p)
            sessions.append({
                "pid": p.pid,
                "kind": kind,
                "owned": p.pid in owned,
                "board": board,
                "ticket": ticket,
                "cpuPercent": round(cpu, 1),
                "memoryMB": round(mem / (1024 * 1024), 1),
                "childCount": len(children),
                "children": children,
                "_createTime": float(p.create_time()),
            })
        except Exception:
            # NoSuchProcess / AccessDenied / partial-death — skip.
            continue
    return sessions


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PerfSampler:
    """Background sampler keeping a rolling per-session CPU/mem history.

    History is keyed by ``(pid, create_time)`` so OS pid reuse never inherits a
    dead session's series. Each ``sample_once`` runs one discovery, appends a
    point per live session, prunes vanished sessions, and refreshes the cached
    snapshot returned by ``snapshot()``.
    """

    def __init__(self, interval=3.0, cap=100, owned_pids_fn=None):
        self.interval = interval
        self.cap = cap
        self._owned_pids_fn = owned_pids_fn or (lambda: set())
        self._proc_iter = None  # None => use psutil via discover_sessions
        self._history = {}       # (pid, create_time) -> deque[{"t","cpu","mem"}]
        self._cache = {"available": PSUTIL_AVAILABLE, "sampledAt": None,
                       "totals": {"cpuPercent": 0.0, "memoryMB": 0.0,
                                  "sessionCount": 0},
                       "sessions": []}
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    def sample_once(self):
        sessions = discover_sessions(proc_iter=self._proc_iter,
                                     owned_pids=self._owned_pids_fn())
        ts = _now_iso()
        live_keys = set()
        tot_cpu = tot_mem = 0.0
        for s in sessions:
            key = (s["pid"], s.pop("_createTime"))
            live_keys.add(key)
            buf = self._history.setdefault(key, deque(maxlen=self.cap))
            buf.append({"t": ts, "cpu": s["cpuPercent"], "mem": s["memoryMB"]})
            s["history"] = list(buf)
            tot_cpu += s["cpuPercent"]
            tot_mem += s["memoryMB"]
        # prune history for sessions no longer present
        for dead in [k for k in self._history if k not in live_keys]:
            del self._history[dead]
        snap = {
            "available": PSUTIL_AVAILABLE,
            "sampledAt": ts,
            "totals": {"cpuPercent": round(tot_cpu, 1),
                       "memoryMB": round(tot_mem, 1),
                       "sessionCount": len(sessions)},
            "sessions": sessions,
        }
        with self._lock:
            self._cache = snap
        return snap

    def snapshot(self):
        with self._lock:
            return self._cache

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:
                pass  # never let the sampler thread die
            self._stop.wait(self.interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="perf-sampler",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def _find_proc(pid):
    if not PSUTIL_AVAILABLE:
        return None
    try:
        return psutil.Process(pid)
    except Exception:
        return None


def _default_killer(pid):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def kill_session(pid, killer=None):
    """Terminate a session's whole tree, children first. A missing pid is a
    no-op success. Returns ``{"killed": [pids], "ok": bool}``."""
    killer = killer or _default_killer
    proc = _find_proc(pid)
    killed = []
    if proc is None:
        return {"killed": killed, "ok": True}
    try:
        kids = proc.children(recursive=True)
    except Exception:
        kids = []
    for k in kids:
        if killer(k.pid):
            killed.append(k.pid)
    if killer(pid):
        killed.append(pid)
    return {"killed": killed, "ok": True}
