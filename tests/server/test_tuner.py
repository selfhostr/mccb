"""Auto-tuning: machine probe (cgroup parsing), static choices, AIMD controller on simulated curves."""
import pytest

from app.server import tuner
from app.server.tuner import AutoTuner, MachineProbe, Sample

MB = 1024 * 1024


# ------------------------------------------------------------------ probe
def _cg(tmp_path, files: dict):
    root = tmp_path / "cg"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(root)


def _meminfo(tmp_path, kb):
    p = tmp_path / "meminfo"
    p.write_text(f"MemTotal: 99999999 kB\nMemFree: 1 kB\nMemAvailable: {kb} kB\n")
    return str(p)


@pytest.mark.parametrize("text,expected", [("max 100000", None), ("200000 100000", 2.0), ("150000 100000", 1.5),
                                           ("50000", 0.5), ("", None), ("garbage", None)])
def test_parse_cpu_max(text, expected):
    assert tuner.parse_cpu_max(text) == expected


def test_probe_cgroup_v2(tmp_path):
    root = _cg(tmp_path, {"cpu.max": "200000 100000\n", "memory.max": str(1024 * MB), "memory.current": str(256 * MB)})
    p = tuner.probe_machine(root, _meminfo(tmp_path, 8 * 1024 * 1024), cpu_count=16, affinity=range(16))
    assert p.cpus == 2
    assert p.mem_available_bytes == 768 * MB  # cgroup (limit - usage) smaller than MemAvailable
    assert p.bench_rate is None


def test_probe_cgroup_v2_unlimited(tmp_path):
    root = _cg(tmp_path, {"cpu.max": "max 100000", "memory.max": "max"})
    p = tuner.probe_machine(root, _meminfo(tmp_path, 4096), cpu_count=8, affinity=range(8))
    assert p.cpus == 8
    assert p.mem_available_bytes == 4096 * 1024


def test_probe_fractional_quota_rounds_up_and_affinity(tmp_path):
    root = _cg(tmp_path, {"cpu.max": "250000 100000"})
    assert tuner.probe_machine(root, _meminfo(tmp_path, 1), cpu_count=16, affinity=range(16)).cpus == 3
    assert tuner.probe_machine(root, _meminfo(tmp_path, 1), cpu_count=16, affinity=[0, 1]).cpus == 2


def test_probe_cgroup_v1(tmp_path):
    root = _cg(tmp_path, {"cpu/cpu.cfs_quota_us": "300000", "cpu/cpu.cfs_period_us": "100000",
                          "memory/memory.limit_in_bytes": str(512 * MB),
                          "memory/memory.usage_in_bytes": str(112 * MB)})
    p = tuner.probe_machine(root, _meminfo(tmp_path, 8 * 1024 * 1024), cpu_count=32, affinity=range(32))
    assert (p.cpus, p.mem_available_bytes) == (3, 400 * MB)


def test_probe_cgroup_v1_unlimited(tmp_path):
    root = _cg(tmp_path, {"cpu/cpu.cfs_quota_us": "-1", "cpu/cpu.cfs_period_us": "100000",
                          "memory/memory.limit_in_bytes": str(9223372036854771712)})
    p = tuner.probe_machine(root, _meminfo(tmp_path, 2048), cpu_count=4, affinity=range(4))
    assert (p.cpus, p.mem_available_bytes) == (4, 2048 * 1024)


def test_probe_benchmark_hook(tmp_path):
    root = _cg(tmp_path, {})
    p = tuner.probe_machine(root, _meminfo(tmp_path, 1), cpu_count=2, affinity=[0, 1], benchmark=lambda: 123.0)
    assert p.bench_rate == 123.0

    def boom():
        raise RuntimeError("x")
    assert tuner.probe_machine(root, _meminfo(tmp_path, 1), cpu_count=2, affinity=[0], benchmark=boom).bench_rate is None


def test_default_benchmark_bounded_by_clock():
    t = [0.0]

    def clock():
        t[0] += 0.1
        return t[0]
    rate = tuner.default_benchmark(seconds=0.5, clock=clock)
    assert rate > 0
    assert t[0] < 1.0  # stops as soon as the (fake) time budget is spent


# ------------------------------------------------------------------ static choices
def probe(cpus=8, mem_mb=16000):
    return MachineProbe(cpus=cpus, mem_available_bytes=mem_mb * MB)


def test_choose_builders_leaves_one_core():
    assert tuner.choose_builders(probe(8)) == 7
    assert tuner.choose_builders(probe(1)) == 1
    assert tuner.choose_builders(probe(2)) == 1
    assert tuner.choose_builders(probe(32)) == 8  # cap


def test_choose_builders_memory_budget():
    assert tuner.choose_builders(probe(8), mem_budget_mb=600, per_builder_mb=200) == 3
    assert tuner.choose_builders(probe(8), mem_budget_mb=50, per_builder_mb=200) == 1  # min 1
    assert tuner.choose_builders(probe(8, mem_mb=1000), per_builder_mb=200) == 2  # default: half of available


def test_choose_builders_fair_split():
    assert tuner.choose_builders(probe(9), running_jobs=2) == 4
    assert tuner.choose_builders(probe(9), running_jobs=3) == 2
    assert tuner.choose_builders(probe(4), running_jobs=10) == 1


def test_build_processes_override():
    assert tuner.choose_builders(probe(8), env_override="3", running_jobs=4) == 3
    assert tuner.choose_builders(probe(8), env_override="0") == 0
    assert tuner.choose_builders(probe(8), env_override=" ") == 7
    assert tuner.choose_builders(probe(8), env_override="abc") == 7


def test_choose_worker_concurrency():
    assert tuner.choose_worker_concurrency(1) == 1
    assert tuner.choose_worker_concurrency(5) == 2
    assert tuner.choose_worker_concurrency(64) == 4
    assert tuner.choose_worker_concurrency(64, pending=2) == 2
    assert tuner.choose_worker_concurrency(64, mem_available_bytes=1200 * MB) == 2
    assert tuner.choose_worker_concurrency(64, cap=16, cores_per_job=4) == 15


def test_manual_settings():
    s = {"tuning": "manual", "workers": 6, "builders": 3}
    assert not tuner.is_auto(s) and tuner.is_auto({}) and tuner.is_auto({"tuning": "auto"})
    assert tuner.manual_settings(s) == {"builders": 3, "connections": 6, "mode": "manual", "fallback": False}
    assert tuner.manual_settings(s, env_override="1")["builders"] == 1
    assert tuner.manual_settings({"tuning": "manual"}) == {"builders": 1, "connections": 1, "mode": "manual",
                                                           "fallback": False}


# ------------------------------------------------------------------ classify_error
class FakeImapError(Exception):
    def __init__(self, msg, kind):
        super().__init__(msg)
        self.kind = kind


@pytest.mark.parametrize("err", ["NO [LIMIT] too many APPENDs", "NO [UNAVAILABLE] backend down", "* BYE server shutting down",
                                 "Too many connections from your IP", ConnectionRefusedError("refused"),
                                 TimeoutError("timed out"), FakeImapError("whatever", "temp"),
                                 "IMAP connection: [Errno 111] Connection refused"])
def test_classify_transient(err):
    assert tuner.classify_error(err)


@pytest.mark.parametrize("err", [None, "NO [TRYCREATE] no such mailbox", "BAD parse error in literal",
                                 FakeImapError("message too big", "perm"), ValueError("bad")])
def test_classify_permanent(err):
    assert not tuner.classify_error(err)


# ------------------------------------------------------------------ AIMD simulation
def saturating(sat=8, per_conn=10 * MB):
    """Throughput grows linearly with connections up to `sat`, then flat."""
    return lambda c: per_conn * min(c, sat)


def run_sim(t: AutoTuner, curve, windows=60, state=tuner.BACKLOG, dur=10.0, **kw):
    history, logs = [], []
    for _ in range(windows):
        d = t.feed(Sample(int(curve(t.connections) * dur), dur, state, **kw))
        history.append(d.connections)
        if d.log:
            logs.append(d.log)
        assert (d.log is None) == (not d.changed)
    return history, logs


def test_settles_at_saturation():
    t = AutoTuner(builders=6, max_builders=7)
    hist, logs = run_sim(t, saturating(8))
    assert max(hist) <= 16
    assert hist[-1] == 8 and set(hist[-20:]) == {8}  # settled, no oscillation
    assert logs[0] == "Auto-tune: 6 builders, IMAP connections 4→6 (APPEND backlog)"
    assert "Auto-tune: 6 builders, IMAP connections 6→8 (throughput +50%)" in logs
    assert any("no throughput gain" in line and line.endswith("→8 (no throughput gain, +0%)") for line in logs)
    assert t.current() == {"builders": 6, "connections": 8, "mode": "auto", "fallback": False}


def test_never_above_cap():
    t = AutoTuner(builders=2, max_builders=2, cap=16)
    hist, _ = run_sim(t, saturating(100), windows=80)
    assert max(hist) == 16 and hist[-1] == 16
    t2 = AutoTuner(builders=2, max_builders=2, cap=10)
    hist2, _ = run_sim(t2, saturating(100), windows=80)
    assert max(hist2) == 10


def test_no_growth_when_starved_or_balanced():
    for state in (tuner.STARVED, tuner.BALANCED):
        t = AutoTuner(builders=2, max_builders=2)
        hist, logs = run_sim(t, saturating(100), state=state, windows=30)
        assert set(hist) == {4} and logs == []


def test_hysteresis_requires_consecutive_backlog_windows():
    t = AutoTuner(builders=2, max_builders=2, min_window_s=1)
    states = [tuner.BACKLOG, tuner.BALANCED] * 10
    for st in states:
        assert not t.feed(Sample(10 * MB, 5.0, st)).changed


def test_short_windows_are_merged():
    t = AutoTuner(builders=2, max_builders=2, min_window_s=5)
    out = [t.feed(Sample(MB, 1.0, tuner.BACKLOG)) for _ in range(20)]
    assert t.window == 4  # 20 x 1 s merged into four 5 s windows
    assert sum(d.changed for d in out) == 1  # grows once 2 evaluated backlog windows are seen


@pytest.mark.parametrize("err,reason", [
    ("NO [LIMIT] too many concurrent APPEND", "server said [LIMIT]"),
    ("NO [UNAVAILABLE] try later", "server said [UNAVAILABLE]"),
    ("* BYE Too many connections", "server said too many connections"),
    ("* BYE disconnecting", "server said BYE"),
    (ConnectionRefusedError(111, "Connection refused"), "connection refused"),
    (FakeImapError("IMAP connection: timed out", "temp"), "timeout"),
    (FakeImapError("weird", "temp"), "temporary error"),
])
def test_backs_off_on_each_error_type(err, reason):
    t = AutoTuner(builders=2, max_builders=2, connections=8)
    d = t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=[err]))
    assert (d.prev_connections, d.connections) == (8, 4)
    assert d.log == f"Auto-tune: IMAP connections 8→4 ({reason})"


def test_permanent_errors_ignored():
    t = AutoTuner(builders=2, max_builders=2, connections=8)
    assert not t.feed(Sample(MB, 10.0, tuner.BALANCED, errors=["NO [TRYCREATE]", ValueError("x")])).changed


def test_error_in_short_window_acts_immediately():
    t = AutoTuner(builders=2, max_builders=2, connections=8, min_window_s=5)
    d = t.feed(Sample(0, 0.5, tuner.BACKLOG, errors=["NO [LIMIT]"]))
    assert d.connections == 4


def test_latency_backoff():
    """Sustained latency excess (2 consecutive windows) together with a throughput drop -> back-off."""
    t = AutoTuner(builders=2, max_builders=2, connections=8)
    for _ in range(3):
        assert not t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=1.0)).changed
    assert not t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=1.4)).changed  # < 1.5x
    assert not t.feed(Sample(5 * MB, 10.0, tuner.BALANCED, append_latency_s=2.0)).changed  # 1st window
    d = t.feed(Sample(5 * MB, 10.0, tuner.BALANCED, append_latency_s=2.0))
    assert d.connections == 4
    assert d.log.startswith("Auto-tune: IMAP connections 8→4 (APPEND latency ") and "for 2 windows" in d.log
    assert "throughput -" in d.log


def test_latency_single_large_attachment_window_does_not_back_off():
    """Regression: one window holding a large attachment (raw mean latency x20) latched the job to 2
    connections. Latencies are compared per size bucket, the excess must persist and throughput drop."""
    t = AutoTuner(builders=2, max_builders=2, connections=8)
    small = {tuner.size_bucket(20_000): (0.2 * 50, 50)}
    for _ in range(5):
        t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_buckets=small))
    big = {tuner.size_bucket(20_000): (0.2 * 10, 10), tuner.size_bucket(40 * MB): (25.0, 1)}
    for _ in range(3):  # big attachment windows: new bucket, no baseline, bytes throughput up
        d = t.feed(Sample(48 * MB, 10.0, tuner.BALANCED, append_buckets=big))
        assert not d.changed, d.log
    # a raw mean of the same windows (old behaviour input) still does not latch: excess without throughput drop
    t2 = AutoTuner(builders=2, max_builders=2, connections=8)
    for _ in range(5):
        t2.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=0.2))
    for _ in range(3):
        assert not t2.feed(Sample(48 * MB, 10.0, tuner.BALANCED, append_latency_s=2.4)).changed
    assert t.connections == t2.connections == 8 and not t.fallback


def test_latency_excess_in_one_window_only_is_ignored():
    t = AutoTuner(builders=2, max_builders=2, connections=8)
    for _ in range(4):
        t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=1.0))
    for _ in range(4):  # alternating: never 2 consecutive windows in excess
        assert not t.feed(Sample(4 * MB, 10.0, tuner.BALANCED, append_latency_s=3.0)).changed
        assert not t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=1.0)).changed


def test_latency_baseline_is_smoothed_not_all_time_min():
    t = AutoTuner(builders=2, max_builders=2, connections=8)
    t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=0.1))  # one lucky window
    for _ in range(40):
        t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=1.0))
    assert 0.9 < t.baseline_latency <= 1.0  # an all-time minimum would stay at 0.1
    assert t.connections == 8


def test_size_bucket():
    assert tuner.size_bucket(None) == tuner.size_bucket(100) == tuner.size_bucket(16383) == 0
    assert tuner.size_bucket(16384) == 1 and tuner.size_bucket(65535) == 1 and tuner.size_bucket(65536) == 2
    assert tuner.size_bucket(1 << 20) == 4 and tuner.size_bucket(1 << 40) == 9


def test_error_kind_is_honoured_without_text_pattern():
    """Regression: the runner stored str(err), losing ImapError.kind ('[Errno 113] No route to host',
    'NO [CONTACTADMIN]' never backed off)."""
    for note in (tuner.ErrorNote("temp", "IMAP connection: [Errno 113] No route to host"),
                 tuner.ErrorNote("temp", "NO [CONTACTADMIN] contact your administrator")):
        t = AutoTuner(builders=2, max_builders=2, connections=8)
        d = t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=[note]))
        assert (d.prev_connections, d.connections) == (8, 4), note
    assert not tuner.classify_error(tuner.ErrorNote("perm", "NO [CANNOT] message too big"))
    assert tuner.classify_error("NO [CONTACTADMIN]")
    assert tuner.ErrorNote.of(FakeImapError("x" * 400, "temp")).kind == "temp"


def test_ceiling_respected_after_backoff():
    """Server accepts up to 9 connections then answers [LIMIT]: after the back-off, never above the
    highest count that worked without errors."""
    t = AutoTuner(builders=2, max_builders=2)
    curve = saturating(100)
    hist = []
    for _ in range(80):
        c = t.connections
        errs = ["NO [LIMIT] max 9 sessions"] if c > 9 else []
        d = t.feed(Sample(int(curve(c) * 10), 10.0, tuner.BACKLOG, errors=errs))
        hist.append(d.connections)
    first_err = next(i for i, c in enumerate(hist) if c > 9)
    after = hist[first_err + 1:]
    assert max(after) <= 8  # 8 was the highest error-free count (4, 6, 8, then 10 failed)
    assert t.ceiling == 8 and hist[-1] == 8 and not t.fallback


def test_fallback_after_repeated_errors():
    lines = []
    t = AutoTuner(builders=6, max_builders=7, connections=16, log=lines.append)
    out = [t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=["NO [UNAVAILABLE]"])) for _ in range(3)]
    assert [d.connections for d in out] == [8, 4, 2]
    assert out[-1].fallback and out[-1].builders == 2
    assert lines[-1] == "Auto-tune: repeated errors, falling back to 2 builder(s), 2 IMAP connection(s)"
    assert t.current() == {"builders": 2, "connections": 2, "mode": "auto", "fallback": True}
    # latched: no growth whatever happens
    hist, logs = run_sim(t, saturating(100), windows=30)
    assert set(hist) == {2} and logs == []
    # ... but server errors still back off (regression: they were ignored once in fallback)
    d = t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=["NO [LIMIT]"]))
    assert (d.prev_connections, d.connections, d.fallback) == (2, 1, True)
    assert d.log == "Auto-tune: IMAP connections 2→1 (server said [LIMIT])"
    assert not t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=["NO [LIMIT]"])).changed  # already at 1
    hist, logs = run_sim(t, saturating(100), windows=30)
    assert set(hist) == {1} and logs == []


def test_fallback_on_error_at_minimum():
    """Regression: an error at 1 connection raised the count to 2 (fallback default)."""
    t = AutoTuner(builders=3, max_builders=7, connections=1)
    d = t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=["* BYE"]))
    assert d.fallback and (d.builders, d.connections) == (2, 1)
    hist, _ = run_sim(t, saturating(100), windows=20)
    assert set(hist) == {1}


def test_fallback_never_increases():
    t = AutoTuner(builders=1, max_builders=7, connections=16, fallback_builders=2, fallback_connections=4)
    t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=["NO [LIMIT]"]))     # 16 -> 8, ceiling 8
    t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=["NO [LIMIT]"]))     # 8 -> 4
    d = t.feed(Sample(MB, 10.0, tuner.BACKLOG, errors=["NO [LIMIT]"]))  # 3rd: fallback
    assert d.fallback and d.builders == 1 and d.connections <= 4 and t.ceiling == d.connections


def test_latency_backoff_still_applies_in_fallback():
    t = AutoTuner(builders=2, max_builders=2, connections=4, fallback_after=1)
    for _ in range(3):
        t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, append_latency_s=1.0))
    t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, errors=["NO [LIMIT]"]))
    assert t.fallback and t.connections == 2
    t.feed(Sample(3 * MB, 10.0, tuner.BALANCED, append_latency_s=3.0))
    d = t.feed(Sample(3 * MB, 10.0, tuner.BALANCED, append_latency_s=3.0))
    assert (d.prev_connections, d.connections) == (2, 1)


def test_spaced_errors_do_not_trigger_fallback():
    t = AutoTuner(builders=2, max_builders=2, connections=16, fallback_windows=10)
    for i in range(30):
        errs = ["NO [LIMIT]"] if i % 12 == 0 else []
        t.feed(Sample(10 * MB, 10.0, tuner.BALANCED, errors=errs))
    assert not t.fallback and t.connections == 2  # 16 -> 8 -> 4 -> 2, spaced out


def _builder_sim(t, windows, util=0.95, headroom=3.0, state=tuner.STARVED):
    for _ in range(windows):
        t.feed(Sample(10 * MB, 10.0, state, builder_util=util, cpu_headroom=headroom))


def test_builder_growth_bounded_by_probe_limit():
    lines = []
    t = AutoTuner(builders=2, max_builders=5, log=lines.append)
    _builder_sim(t, 40)
    assert t.builders == 5 and t.connections == 4
    assert lines[0] == "Auto-tune: builders 2→3 (builders busy, CPU headroom 3.0 cores)"


def test_builder_growth_needs_headroom_and_busy_builders():
    for kw in ({"headroom": 0.5}, {"util": 0.5}, {"state": tuner.BACKLOG, "headroom": 3.0}):
        t = AutoTuner(builders=2, max_builders=5, cap=4)  # cap=4: BACKLOG cannot grow connections either
        _builder_sim(t, 30, **kw)
        assert t.builders == 2, kw


def test_queue_state_helper():
    assert tuner.queue_state(append_pending=12, connections=4) == tuner.BACKLOG
    assert tuner.queue_state(append_pending=2, connections=4, inflight_bytes=95, byte_budget=100) == tuner.BACKLOG
    assert tuner.queue_state(append_pending=1, connections=4) == tuner.STARVED
    assert tuner.queue_state(append_pending=4, connections=4) == tuner.BALANCED


def test_deterministic():
    def go():
        t = AutoTuner(builders=3, max_builders=6)
        out = []
        for i in range(60):
            c = t.connections
            errs = ["NO [LIMIT]"] if c > 11 and i % 3 == 0 else []
            st = tuner.BACKLOG if i % 7 else tuner.STARVED
            d = t.feed(Sample(int(saturating(10)(c) * 10), 10.0, st, append_latency_s=1.0, builder_util=0.9,
                              cpu_headroom=2.0, errors=errs))
            out.append((d.builders, d.connections, d.log))
        return out
    assert go() == go()
