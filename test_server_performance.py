import kanban_server as ks


def test_perf_snapshot_returns_sampler_cache(monkeypatch):
    fake = {"available": True, "sampledAt": "t", "totals": {}, "sessions": []}

    class FakeSampler:
        def snapshot(self):
            return fake

    monkeypatch.setattr(ks, "_PERF_SAMPLER", FakeSampler())
    data, status = ks.perf_snapshot()
    assert status == 200
    assert data is fake


def test_perf_kill_validates_pid(monkeypatch):
    monkeypatch.setattr(ks.perf_monitor, "kill_session",
                        lambda pid: {"killed": [pid], "ok": True})
    data, status = ks.perf_kill("10")
    assert status == 200 and data["killed"] == [10]
    data, status = ks.perf_kill("notanint")
    assert status == 400
