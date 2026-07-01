import perf_monitor as pm


class FakeProc:
    def __init__(self, pid, name, cmdline, rss=10*1024*1024, cpu=1.0,
                 create_time=100.0, children=None):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline
        self._rss = rss
        self._cpu = cpu
        self._ct = create_time
        self._children = children or []

    def name(self): return self._name
    def cmdline(self): return self._cmdline
    def create_time(self): return self._ct
    def cpu_percent(self, interval=None): return self._cpu
    def memory_info(self):
        class M: pass
        m = M(); m.rss = self._rss; return m
    def children(self, recursive=False): return self._children


def test_classify_headless_extracts_board_and_ticket():
    cmd = ["claude.EXE", "-p", "Ticket #10 ...\nTicket file: C:\\x\\.AI-kanban\\kanban-dev\\10.json"]
    kind, board, ticket = pm.classify_cmdline(cmd)
    assert kind == "headless"
    assert board == "kanban-dev"
    assert ticket == "10"


def test_classify_interactive_has_no_ticket():
    kind, board, ticket = pm.classify_cmdline(["claude.exe"])
    assert kind == "interactive"
    assert board is None and ticket is None


def test_discover_finds_only_claude_and_rolls_up_children():
    child = FakeProc(22, "bash.exe", ["bash"], rss=8*1024*1024, cpu=2.0)
    claude = FakeProc(10, "claude.exe", ["claude.exe"], rss=100*1024*1024,
                      cpu=4.0, children=[child])
    other = FakeProc(99, "explorer.exe", ["explorer.exe"])
    sessions = pm.discover_sessions(proc_iter=lambda: [claude, other], owned_pids={10})
    assert len(sessions) == 1
    s = sessions[0]
    assert s["pid"] == 10 and s["owned"] is True
    assert s["childCount"] == 1
    # rollup: cpu 4.0 + 2.0, mem (100+8)MB
    assert s["cpuPercent"] == 6.0
    assert round(s["memoryMB"]) == 108
    assert s["children"][0]["name"] == "bash.exe"


# --- Task 2: PerfSampler ---

def _sampler_with(procs):
    s = pm.PerfSampler(interval=0, cap=3)
    s._proc_iter = lambda: procs
    return s


def test_snapshot_accumulates_history_and_totals():
    claude = FakeProc(10, "claude.exe", ["claude.exe"], rss=50*1024*1024, cpu=2.0)
    s = _sampler_with([claude])
    s.sample_once()
    s.sample_once()
    snap = s.snapshot()
    assert snap["available"] is True
    assert snap["totals"]["sessionCount"] == 1
    sess = snap["sessions"][0]
    assert len(sess["history"]) == 2
    assert sess["history"][-1]["cpu"] == 2.0


def test_history_evicts_by_cap():
    claude = FakeProc(10, "claude.exe", ["claude.exe"])
    s = _sampler_with([claude])  # cap=3
    for _ in range(5):
        s.sample_once()
    sess = s.snapshot()["sessions"][0]
    assert len(sess["history"]) == 3


def test_dead_session_history_dropped():
    claude = FakeProc(10, "claude.exe", ["claude.exe"])
    s = _sampler_with([claude])
    s.sample_once()
    s._proc_iter = lambda: []          # process gone
    s.sample_once()
    assert s.snapshot()["sessions"] == []
    assert s._history == {}            # series pruned


def test_reused_pid_new_createtime_starts_fresh_series():
    first = FakeProc(10, "claude.exe", ["claude.exe"], create_time=100.0, cpu=1.0)
    s = _sampler_with([first])
    s.sample_once()
    s.sample_once()
    # same pid, different create_time => OS pid reuse, fresh series
    second = FakeProc(10, "claude.exe", ["claude.exe"], create_time=999.0, cpu=5.0)
    s._proc_iter = lambda: [second]
    s.sample_once()
    sess = s.snapshot()["sessions"][0]
    assert len(sess["history"]) == 1
    assert sess["history"][0]["cpu"] == 5.0


# --- Task 3: kill_session ---

def test_kill_session_kills_children_then_parent(monkeypatch):
    child = FakeProc(22, "bash.exe", ["bash"])
    parent = FakeProc(10, "claude.exe", ["claude.exe"], children=[child])
    monkeypatch.setattr(pm, "_find_proc", lambda pid: parent if pid == 10 else None)
    order = []
    res = pm.kill_session(10, killer=lambda p: order.append(p) or True)
    assert order == [22, 10]          # children first, then parent
    assert res["ok"] is True
    assert res["killed"] == [22, 10]


def test_kill_missing_pid_is_noop_success(monkeypatch):
    monkeypatch.setattr(pm, "_find_proc", lambda pid: None)
    res = pm.kill_session(999, killer=lambda p: True)
    assert res["ok"] is True
    assert res["killed"] == []
