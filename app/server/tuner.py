"""Automatic tuning of the migration pipeline: builder processes and IMAP APPEND connections.

Pure and deterministic: no thread, no I/O except the explicit probe helpers (whose paths, clock and
inputs are injectable). The runner feeds :class:`AutoTuner` periodic :class:`Sample` windows and applies
the returned :class:`Decision` (resize its BuilderPool / AppendPool) when ``decision.changed``.

Pipeline reminder (runner.py / builders.py): the job process dispatches build tasks to
``BUILD_PROCESSES`` builder processes; built messages are submitted to the AppendPool (N IMAP
connections, one thread each, task queue ``maxsize = 2N``) within a byte budget of built-but-not-yet-
APPENDed messages (``BUILD_MAX_INFLIGHT_MB``). Two bottleneck states follow:

- ``backlog``: built messages queue up waiting for an APPEND slot (task queue full / byte budget
  exhausted, builders idle because ``_submit`` blocks). The APPEND side limits throughput: more IMAP
  connections may help.
- ``starved``: APPEND threads wait on ``tasks.get()`` (empty queue): builders limit throughput. More
  connections cannot help; more builders can, if CPU headroom exists.

The original wording ("increase connections while the APPEND queue is starved for builders") is read
as "while the *builders* are starved, i.e. waiting for APPEND slots" — the ``backlog`` state above.
Growing connections while the appenders themselves wait on builders would only add idle connections.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

# ------------------------------------------------------------------------------------------------ probe
UNLIMITED = 1 << 60  # cgroup v1 reports "no limit" as a huge number (PAGE_COUNTER_MAX * page size)


@dataclass(frozen=True)
class MachineProbe:
    cpus: int                     # usable cores (cgroup quota, affinity, cpu_count: smallest)
    mem_available_bytes: int      # MemAvailable or cgroup (limit - usage), whichever is smaller
    bench_rate: float | None = None  # optional micro-benchmark result (items/s or bytes/s, caller-defined)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def parse_cpu_max(text: str | None) -> float | None:
    """cgroup v2 ``cpu.max`` ("quota period"; quota "max" = unlimited) -> cores (float) or None."""
    if not text:
        return None
    parts = text.split()
    if not parts or parts[0] == "max":
        return None
    try:
        quota = int(parts[0])
        period = int(parts[1]) if len(parts) > 1 else 100000
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def cgroup_cpu_quota(root: str = "/sys/fs/cgroup") -> float | None:
    """Cores allowed by the cgroup quota (v2 cpu.max, else v1 cfs_quota_us/cfs_period_us), None = no quota."""
    q = parse_cpu_max(_read(os.path.join(root, "cpu.max")))
    if q is not None:
        return q
    for sub in ("cpu", "cpu,cpuacct", "cpuacct,cpu", ""):
        base = os.path.join(root, sub) if sub else root
        quota = _int(_read(os.path.join(base, "cpu.cfs_quota_us")))
        period = _int(_read(os.path.join(base, "cpu.cfs_period_us")))
        if quota is not None and period:
            return quota / period if quota > 0 else None
    return None


def cgroup_mem_available(root: str = "/sys/fs/cgroup") -> int | None:
    """Bytes still available in the cgroup (limit - usage), None = no limit."""
    lim = _read(os.path.join(root, "memory.max"))
    if lim is not None:
        if lim == "max":
            return None
        limit, usage = _int(lim), _int(_read(os.path.join(root, "memory.current"))) or 0
    else:
        limit = usage = None
        for sub in ("memory", ""):
            base = os.path.join(root, sub) if sub else root
            limit = _int(_read(os.path.join(base, "memory.limit_in_bytes")))
            if limit is not None:
                usage = _int(_read(os.path.join(base, "memory.usage_in_bytes"))) or 0
                break
    if limit is None or limit >= UNLIMITED:
        return None
    return max(0, limit - (usage or 0))


def meminfo_available(path: str = "/proc/meminfo") -> int | None:
    text = _read(path)
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            v = _int(line.split(":", 1)[1])
            return v * 1024 if v is not None else None
    return None


def default_benchmark(seconds: float = 1.5, clock: Callable[[], float] = time.monotonic) -> float:
    """Synthetic build work: builds and serialises multipart MIME messages (pure-Python ``email``,
    representative of the engine's MIME/encoding cost) for ``seconds``. Returns messages/s."""
    from email.message import EmailMessage

    body = ("Bonjour, ceci est un message de test accentué — " * 40) + "\n"
    blob = bytes(range(256)) * 64  # 16 KiB attachment, base64-encoded
    n, start = 0, clock()
    while True:
        m = EmailMessage()
        m["From"] = "Émetteur <a@example.test>"
        m["To"] = "b@example.test"
        m["Subject"] = f"Benchmark n°{n}"
        m.set_content(body)
        m.add_alternative(f"<html><body><p>{body}</p></body></html>", subtype="html")
        m.add_attachment(blob, maintype="application", subtype="octet-stream", filename="pièce.bin")
        m.as_bytes()
        n += 1
        elapsed = clock() - start
        if elapsed >= seconds:
            return n / elapsed if elapsed > 0 else float(n)


def probe_machine(cgroup_root: str = "/sys/fs/cgroup", meminfo_path: str = "/proc/meminfo",
                  cpu_count: int | None = None, affinity: Iterable[int] | None = None,
                  benchmark: Callable[[], float] | bool | None = None) -> MachineProbe:
    """Usable cores = min(ceil(cgroup quota), len(sched_getaffinity), os.cpu_count()), at least 1.
    Available memory = min(/proc/meminfo MemAvailable, cgroup limit - usage).
    ``benchmark``: None/False = no benchmark, True = :func:`default_benchmark`, a callable = run it
    (~1-2 s of representative build work, returns items/s or bytes/s)."""
    counts: list[int] = []
    total = cpu_count if cpu_count is not None else os.cpu_count()
    if total:
        counts.append(int(total))
    if affinity is None and hasattr(os, "sched_getaffinity"):
        try:
            affinity = os.sched_getaffinity(0)
        except OSError:  # pragma: no cover
            affinity = None
    if affinity is not None:
        n_aff = len(list(affinity))
        if n_aff:
            counts.append(n_aff)
    quota = cgroup_cpu_quota(cgroup_root)
    if quota is not None:
        counts.append(max(1, math.ceil(quota - 1e-9)))
    cpus = max(1, min(counts) if counts else 1)

    mems = [m for m in (meminfo_available(meminfo_path), cgroup_mem_available(cgroup_root)) if m is not None]
    mem = min(mems) if mems else 0

    rate = None
    if benchmark is True:
        rate = default_benchmark()
    elif callable(benchmark):
        try:
            rate = float(benchmark())
        except Exception:  # a failing benchmark must not prevent the job from starting
            rate = None
    return MachineProbe(cpus=cpus, mem_available_bytes=mem, bench_rate=rate)


# ------------------------------------------------------------------------------------ static choices
def parse_env_builders(value: str | int | None) -> int | None:
    """BUILD_PROCESSES value -> int (0 = build inside the job process) or None if unset/invalid."""
    if value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    v = str(value).strip()
    if not v:
        return None
    try:
        return max(0, int(v))
    except ValueError:
        return None


def choose_builders(probe: MachineProbe, mem_budget_mb: float | None = None, per_builder_mb: float = 200,
                    env_override: str | int | None = None, running_jobs: int = 1, cap: int = 8) -> int:
    """Builder processes for one job.

    - ``env_override`` (the BUILD_PROCESSES value) forces the result globally when set (0 allowed).
    - Otherwise: leave 1 core free (job process / IMAP threads), split the rest fairly between the
      ``running_jobs`` concurrent jobs, at most ``mem_budget_mb // per_builder_mb`` builders
      (default budget: half of the available memory, itself split between jobs), at most ``cap``, min 1.
    """
    forced = parse_env_builders(env_override)
    if forced is not None:
        return forced
    jobs = max(1, int(running_jobs or 1))
    fair = max(1, (probe.cpus - 1) // jobs)
    if mem_budget_mb is None:
        mem_budget_mb = probe.mem_available_bytes / (1024 * 1024) / 2 / jobs if probe.mem_available_bytes else None
    by_mem = fair if mem_budget_mb is None else int(mem_budget_mb // max(1.0, per_builder_mb))
    return max(1, min(fair, by_mem, cap))


def choose_worker_concurrency(cpus: int, pending: int | None = None, mem_available_bytes: int | None = None,
                              per_job_mb: float = 512, cores_per_job: int = 2, cap: int = 4) -> int:
    """Mailboxes (jobs) migrated in parallel in auto mode, without oversubscription: each job needs
    ``cores_per_job`` cores (job process + at least one builder) out of ``cpus - 1``, and ``per_job_mb``
    of memory; at most ``cap`` and at most ``pending`` jobs waiting; min 1."""
    n = max(1, (max(1, cpus) - 1) // max(1, cores_per_job))
    if mem_available_bytes:
        n = min(n, max(1, int(mem_available_bytes / (1024 * 1024) // max(1.0, per_job_mb))))
    if pending is not None:
        n = min(n, max(1, pending))
    return max(1, min(n, cap))


def is_auto(settings: dict | None) -> bool:
    return str((settings or {}).get("tuning") or "auto").lower() != "manual"


def manual_settings(settings: dict, env_override: str | int | None = None, default_builders: int = 1) -> dict:
    """Manual mode: fixed values from the server settings ``{tuning: "manual", workers: N, builders: M}``
    (``workers`` = IMAP APPEND connections). BUILD_PROCESSES still overrides the builder count."""
    try:
        conns = max(1, int(settings.get("workers") or 1))
    except (TypeError, ValueError):
        conns = 1
    forced = parse_env_builders(env_override)
    if forced is not None:
        b = forced
    else:
        try:
            b = max(0, int(settings.get("builders"))) if settings.get("builders") is not None else default_builders
        except (TypeError, ValueError):
            b = default_builders
    return {"builders": b, "connections": conns, "mode": "manual", "fallback": False}


# --------------------------------------------------------------------------------- error classification
TRANSIENT_PATTERNS = ("[LIMIT]", "[UNAVAILABLE]", "[INUSE]", "[SERVERBUG]", "[CONTACTADMIN]", "TOO MANY CONNECTIONS",
                      "TOO MANY SIMULTANEOUS", "MAXIMUM NUMBER OF CONNECTIONS", "CONNECTION LIMIT", "BYE",
                      "CONNECTION REFUSED", "CONNECTION RESET", "TRY AGAIN", "TEMPORAR", "THROTTL",
                      "TIMED OUT", "TIMEOUT", "BROKEN PIPE", "EOF")


@dataclass(frozen=True)
class ErrorNote:
    """An error as recorded for the tuner: its classified ``kind`` (imap.ImapError: temp, perm, fatal...) and
    message. Keeps the classification that ``str(err)`` would lose (``[Errno 113] No route to host`` or
    ``NO [CONTACTADMIN]`` are ``temp`` without matching any text pattern)."""
    kind: str | None
    message: str

    @classmethod
    def of(cls, err) -> "ErrorNote":
        return cls(getattr(err, "kind", None), str(err)[:300])

    def __str__(self) -> str:
        return self.message


def classify_error(exc_or_message) -> bool:
    """True if the error is transient / a server limit (-> back off the connection count)."""
    if exc_or_message is None:
        return False
    kind = getattr(exc_or_message, "kind", None)
    if kind == "temp":
        return True
    if isinstance(exc_or_message, BaseException):
        if isinstance(exc_or_message, (ConnectionError, TimeoutError, EOFError)):
            return True
        text = f"{type(exc_or_message).__name__}: {exc_or_message}"
    else:
        text = str(exc_or_message)
    up = text.upper()
    return any(p in up for p in TRANSIENT_PATTERNS)


def _reason_of(err) -> str:
    up = str(err).upper()
    for code in ("[LIMIT]", "[UNAVAILABLE]", "[INUSE]", "[SERVERBUG]", "[CONTACTADMIN]"):
        if code in up:
            return f"server said {code}"
    if "TOO MANY" in up or "CONNECTION LIMIT" in up or "MAXIMUM NUMBER OF CONNECTIONS" in up:
        return "server said too many connections"
    if "BYE" in up:
        return "server said BYE"
    if "REFUSED" in up:
        return "connection refused"
    if "TIMEOUT" in up or "TIMED OUT" in up:
        return "timeout"
    if "UNREACHABLE" in up or "NO ROUTE" in up:
        return "server unreachable"
    return "temporary error"


# ------------------------------------------------------------------------------------- AIMD controller
BACKLOG, STARVED, BALANCED = "backlog", "starved", "balanced"


def queue_state(append_pending: int, connections: int, inflight_bytes: int = 0, byte_budget: int = 0,
                idle_appenders: int | None = None) -> str:
    """Helper for the runner: ``backlog`` when built messages wait for an APPEND slot (more queued than
    connections, or byte budget >= 90 % used), ``starved`` when APPEND threads are idle / queue empty,
    else ``balanced``."""
    if (byte_budget and inflight_bytes >= 0.9 * byte_budget) or append_pending > max(1, connections):
        return BACKLOG
    if append_pending < connections or (idle_appenders or 0) > 0:
        return STARVED
    return BALANCED


@dataclass
class Sample:
    bytes_appended: int                   # bytes APPENDed during the window
    duration_s: float                     # window length
    queue_state: str = BALANCED           # backlog | starved | balanced (see module doc / queue_state())
    append_latency_s: float | None = None  # mean APPEND latency over the window (message size unknown)
    builder_util: float | None = None     # mean builder busy fraction 0..1
    cpu_headroom: float | None = None     # idle cores on the machine (cgroup-aware)
    errors: list = field(default_factory=list)  # exceptions, ErrorNotes or messages seen during the window
    # APPEND latencies by message size bucket (size_bucket()): {bucket: (sum of seconds, count)}. Preferred to
    # append_latency_s: a window holding one large attachment is not compared with the baseline of small mails
    append_buckets: dict | None = None

    def latency_buckets(self) -> dict:
        out = dict(self.append_buckets or {})
        if self.append_latency_s is not None and self.append_latency_s > 0 and UNSIZED not in out:
            out[UNSIZED] = (float(self.append_latency_s), 1)
        return out


UNSIZED = -1  # bucket of latencies given without message size (Sample.append_latency_s)


def size_bucket(size: int | None) -> int:
    """Message size bucket for latency comparison: 0 = < 16 KiB, then one bucket per factor 4 (16-64 KiB: 1,
    64-256 KiB: 2, 256 KiB-1 MiB: 3, ...), at most 9."""
    if size is None or size < 16384:
        return 0
    return min(9, ((int(size) // 16384).bit_length() + 1) // 2)


def merge_buckets(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    for k, (t, n) in (b or {}).items():
        t0, n0 = out.get(k, (0.0, 0))
        out[k] = (t0 + t, n0 + n)
    return out


@dataclass(frozen=True)
class Decision:
    builders: int
    connections: int
    prev_builders: int
    prev_connections: int
    reason: str = ""
    log: str | None = None      # None = no change, nothing to log
    fallback: bool = False

    @property
    def changed(self) -> bool:
        return self.builders != self.prev_builders or self.connections != self.prev_connections


class AutoTuner:
    """AIMD controller for IMAP APPEND connections (and builder count).

    Rules, evaluated once per window of at least ``min_window_s`` (shorter samples are merged):

    1. Transient/limit error in the window (:func:`classify_error`: temp error, refused connection, BYE,
       [LIMIT], [UNAVAILABLE], too many connections...) -> connections halved immediately (min 1).
       Latency: APPEND latencies are compared per message size bucket (:func:`size_bucket`) with a smoothed
       (EWMA) baseline of that bucket; a back-off needs the latency excess (> ``latency_factor`` x baseline)
       for ``latency_windows`` consecutive windows AND the bytes throughput down by >= ``latency_drop``
       from its smoothed reference (one window holding a large attachment never backs off).
       The ceiling becomes the highest count that ran error-free and is below the failing count (never
       exceeded again).
    2. Repeated trouble (>= ``fallback_after`` back-offs within ``fallback_windows`` windows, or an error
       while already at 1 connection) -> fallback mode, latched for the job: min(``fallback_builders``,
       current) builders, min(``fallback_connections``, ceiling, current) connections (never an increase).
       In fallback mode nothing grows any more, but errors / latency still back the connections off.
    3. After a change: ``cooldown_windows`` windows ignored (settling), then the throughput at the new
       level is compared with the previous level; an increase that brought < ``min_gain`` is reverted
       and that level becomes a plateau (no further growth).
    4. Additive increase (+2 if the last gain was >= 15 % or no gain is known yet, else +1, up to
       min(cap, ceiling, plateau)) when the APPEND side is the bottleneck (``backlog``) for
       ``streak`` consecutive windows and the last increase paid off.
    5. Builders +1 (up to ``max_builders``, the probe limit) when builders are the bottleneck
       (``starved``), busy (util >= ``builder_busy``) and >= 1 core of CPU headroom, for ``streak``
       consecutive windows.
    At most one change per window; no change -> no log line.
    """

    def __init__(self, builders: int, max_builders: int, connections: int = 4, cap: int = 16,
                 min_gain: float = 0.05, latency_factor: float = 1.5, min_window_s: float = 5.0,
                 streak: int = 2, cooldown_windows: int = 1, fallback_after: int = 3, fallback_windows: int = 10,
                 builder_busy: float = 0.85, fallback_builders: int = 2, fallback_connections: int = 2,
                 latency_windows: int = 2, latency_drop: float = 0.15, latency_alpha: float = 0.2,
                 log: Callable[[str], None] | None = None):
        self.cap = max(1, cap)
        self.max_builders = max(1, max_builders)
        self.builders = max(1, min(builders, self.max_builders))
        self.connections = max(1, min(connections, self.cap))
        self.min_gain, self.latency_factor, self.min_window_s = min_gain, latency_factor, min_window_s
        self.streak_needed, self.cooldown_windows = max(1, streak), max(0, cooldown_windows)
        self.fallback_after, self.fallback_windows = fallback_after, fallback_windows
        self.builder_busy = builder_busy
        self.fallback_builders, self.fallback_connections = fallback_builders, fallback_connections
        self.latency_windows, self.latency_drop, self.latency_alpha = max(1, latency_windows), latency_drop, \
            latency_alpha
        self._log = log
        self.ceiling = self.cap
        self.plateau: int | None = None
        self.fallback = False
        self.window = 0                       # evaluated windows
        self.baseline: dict[int, float] = {}  # size bucket -> smoothed APPEND latency (s)
        self._ref_rate: float | None = None   # smoothed bytes/s of windows without latency excess
        self._lat_streak = 0
        self._good: set[int] = set()          # connection counts that ran a window without error
        self._backoffs: list[int] = []        # window indices of back-offs
        self._since_change = 0
        self._level_rates: list[float] = []   # post-cooldown rates at the current level
        self._prev_level: int | None = None   # connection count before the last increase
        self._prev_rate: float | None = None  # its throughput
        self._last_gain: float | None = None
        self._conn_streak = 0
        self._build_streak = 0
        self._pending: Sample | None = None

    # -------------------------------------------------------------------------------- public
    def current(self) -> dict:
        return {"builders": self.builders, "connections": self.connections, "mode": "auto",
                "fallback": self.fallback}

    def feed(self, s: Sample) -> Decision:
        s = self._merge(s)
        if s is None:
            return self._same()
        self.window += 1
        rate = s.bytes_appended / s.duration_s if s.duration_s > 0 else 0.0
        errs = [e for e in s.errors if classify_error(e)]
        if errs:
            self._lat_streak = 0
            return self._back_off(_reason_of(errs[0]))
        why = self._latency_check(s, rate)
        if why is not None:
            return self._back_off(why)
        if self.fallback:  # latched: back-offs only, never any growth
            return self._same()
        # error-free window
        self._good.add(self.connections)
        self._since_change += 1
        if self._since_change <= self.cooldown_windows:
            return self._same()
        self._level_rates.append(rate)
        level_rate = sum(self._level_rates) / len(self._level_rates)

        # outcome of the last increase
        if self._prev_level is not None and self._prev_rate is not None and self._last_gain is None:
            gain = level_rate / self._prev_rate - 1 if self._prev_rate > 0 else 1.0
            if gain < self.min_gain:
                old, self.plateau = self.connections, self._prev_level
                self._prev_level = self._prev_rate = None
                return self._change(self.builders, self.plateau,
                                    f"no throughput gain {gain * 100:+.0f}%",
                                    f"Auto-tune: IMAP connections {old}→{self.plateau} "
                                    f"(no throughput gain, {gain * 100:+.0f}%)")
            self._last_gain = gain

        limit = min(self.cap, self.ceiling, self.plateau or self.cap)
        if s.queue_state == BACKLOG and self.connections < limit:
            self._build_streak = 0
            self._conn_streak += 1
            if self._conn_streak >= self.streak_needed:
                step = 2 if self._last_gain is None or self._last_gain >= 0.15 else 1
                new = min(limit, self.connections + step)
                reason = (f"throughput {self._last_gain * 100:+.0f}%" if self._last_gain is not None
                          else "APPEND backlog")
                old = self.connections
                self._prev_level, self._prev_rate, self._last_gain = old, level_rate, None
                return self._change(self.builders, new, reason,
                                    f"Auto-tune: {self.builders} builders, IMAP connections {old}→{new} ({reason})")
            return self._same()
        self._conn_streak = 0
        if s.queue_state == STARVED and self.builders < self.max_builders \
                and (s.builder_util or 0) >= self.builder_busy and (s.cpu_headroom or 0) >= 1.0:
            self._build_streak += 1
            if self._build_streak >= self.streak_needed:
                old = self.builders
                reason = f"builders busy, CPU headroom {s.cpu_headroom:.1f} cores"
                return self._change(old + 1, self.connections, reason,
                                    f"Auto-tune: builders {old}→{old + 1} ({reason})")
            return self._same()
        self._build_streak = 0
        return self._same()

    @property
    def baseline_latency(self) -> float | None:
        """Smoothed latency of the smallest size bucket observed (display / compatibility)."""
        return self.baseline[min(self.baseline)] if self.baseline else None

    def _latency_check(self, s: Sample, rate: float) -> str | None:
        """Updates the latency baselines / throughput reference; returns a back-off reason when the latency
        excess has lasted ``latency_windows`` windows together with a throughput drop, else None."""
        buckets = {k: (t / n) for k, (t, n) in s.latency_buckets().items() if n > 0 and t > 0}
        weights = {k: n for k, (t, n) in s.latency_buckets().items() if n > 0 and t > 0}
        known = [k for k in buckets if k in self.baseline and self.baseline[k] > 0]
        ratio = None
        if known:
            w = sum(weights[k] for k in known)
            ratio = sum(weights[k] * buckets[k] / self.baseline[k] for k in known) / w
        excess = ratio is not None and ratio > self.latency_factor
        dropped = self._ref_rate is not None and self._ref_rate > 0 and rate < (1 - self.latency_drop) * self._ref_rate
        a = self.latency_alpha
        # baselines: new buckets start at their first value; a window in excess barely moves them (a lasting
        # shift is still absorbed slowly, a transient one is not)
        for k, v in buckets.items():
            b = self.baseline.get(k)
            self.baseline[k] = v if b is None else b + (a if not excess else a / 4) * (v - b)
        if not excess:
            self._ref_rate = rate if self._ref_rate is None else self._ref_rate + a * (rate - self._ref_rate)
        if excess and dropped:
            self._lat_streak += 1
        else:
            self._lat_streak = 0
        if self._lat_streak < self.latency_windows:
            return None
        drop = rate / self._ref_rate - 1
        self._lat_streak = 0
        return (f"APPEND latency {ratio:.1f}× baseline for {self.latency_windows} windows, "
                f"throughput {drop * 100:+.0f}%")

    # ------------------------------------------------------------------------------ internals
    def _merge(self, s: Sample) -> Sample | None:
        p = self._pending
        if p is not None:
            dur = p.duration_s + s.duration_s
            lats = [x for x in (p.append_latency_s, s.append_latency_s) if x is not None]
            s = Sample(p.bytes_appended + s.bytes_appended, dur, s.queue_state,
                       max(lats) if lats else None,
                       s.builder_util if s.builder_util is not None else p.builder_util,
                       s.cpu_headroom if s.cpu_headroom is not None else p.cpu_headroom,
                       list(p.errors) + list(s.errors),
                       merge_buckets(p.append_buckets, s.append_buckets) or None)
        if s.duration_s < self.min_window_s and not any(classify_error(e) for e in s.errors):
            self._pending = s
            return None
        self._pending = None
        return s

    def _same(self) -> Decision:
        return Decision(self.builders, self.connections, self.builders, self.connections, fallback=self.fallback)

    def _change(self, builders: int, conns: int, reason: str, line: str, fallback: bool = False) -> Decision:
        d = Decision(builders, conns, self.builders, self.connections, reason, line, fallback)
        builders_only = builders != self.builders and conns == self.connections
        self.builders, self.connections = builders, conns
        self._since_change = 0
        self._conn_streak = self._build_streak = 0
        self._level_rates = []
        if builders_only:  # throughput reference no longer comparable
            self._prev_level = self._prev_rate = None
            self._last_gain = None
        if self._log and line:
            self._log(line)
        return d

    def _back_off(self, reason: str) -> Decision:
        old = self.connections
        if self.fallback:  # already latched: keep backing off (min 1), the ceiling follows
            if old <= 1:
                return self._same()
            new = max(1, old // 2)
            self.ceiling = min(self.ceiling, new)
            return self._change(self.builders, new, reason, f"Auto-tune: IMAP connections {old}→{new} ({reason})",
                                fallback=True)
        recent = [w for w in self._backoffs if self.window - w < self.fallback_windows]
        recent.append(self.window)
        self._backoffs = recent
        if old <= 1 or len(recent) >= self.fallback_after:
            return self._enter_fallback()
        new = max(1, old // 2)
        good_below = [c for c in self._good if c < old]
        self.ceiling = max(new, max(good_below) if good_below else new)
        self._good = {c for c in self._good if c <= self.ceiling}
        if self.plateau is not None:
            self.plateau = min(self.plateau, self.ceiling)
        self._prev_level = self._prev_rate = None
        self._last_gain = None
        return self._change(self.builders, new, reason, f"Auto-tune: IMAP connections {old}→{new} ({reason})")

    def _enter_fallback(self) -> Decision:
        """Never an increase: an error at 1 connection stays at 1 connection."""
        b = max(1, min(self.fallback_builders, self.max_builders, self.builders))
        c = max(1, min(self.fallback_connections, self.cap, self.ceiling, self.connections))
        self.fallback = True
        self.ceiling = c
        self._prev_level = self._prev_rate = None
        self._last_gain = None
        return self._change(b, c, "repeated errors",
                            f"Auto-tune: repeated errors, falling back to {b} builder(s), {c} IMAP connection(s)", fallback=True)
