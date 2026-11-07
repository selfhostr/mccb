"""Parallel message builders: N processes, each with its own read-only PST handle.

Building a MIME message (libpff reads, charset work, MIME encoding, self-verification, hash) is pure
Python and CPU-bound: one job process saturated one core while the APPEND threads waited. The job
process now keeps the iteration, the bookkeeping (DB), the AppendPool; builder processes receive
small tasks ``(folder index, path, item index, item key, import id)`` and return the finished message.
The same processes also run the date indexing of ordering.py (tasks with a ``run()`` method).

- One task at a time per builder: if a builder dies (libpff SIGSEGV) the item it held is known
  exactly; the caller marks it "libpff crash" and the builder is respawned.
- Builders are started with "spawn" (no fork of a process holding threads, sqlite connections and
  a pypff handle) and die with their parent (PR_SET_PDEATHSIG).
- Nothing is written to the DB here: a build that is in flight when the job pauses/stops is simply
  discarded (the builders are killed), so an item is never half-recorded.
"""
from __future__ import annotations

import hashlib
import importlib
import multiprocessing
import multiprocessing.connection
import os
import signal
import time
from dataclasses import dataclass

IMPORT_HEADER = "X-PST-Import-Id"


def default_processes() -> int:
    """BUILD_PROCESSES, default machine_default(). 0 = build inside the job process."""
    v = os.environ.get("BUILD_PROCESSES", "").strip()
    if v:
        try:
            return max(0, int(v))
        except ValueError:
            pass
    return machine_default()


def machine_default() -> int:
    """Builder count without BUILD_PROCESSES (manual mode with `builders` unset): the auto-tuner's static
    choice on a cheap machine probe (no benchmark) - usable cores (cgroup quota, affinity, cpu_count) - 1,
    bounded by the available memory (cgroup-aware), at most tuner.MAX_BUILDERS, at least 1."""
    from . import tuner
    try:
        return tuner.choose_builders(tuner.probe_machine())
    except Exception:  # noqa: BLE001 - probe failure: the former default
        return max(1, min((os.cpu_count() or 2) - 1, tuner.MAX_BUILDERS))


def byte_budget(mem_available_bytes: int = 0, running_jobs: int = 1) -> int:
    """Bytes of built messages allowed in flight (built, not yet APPENDed): BUILD_MAX_INFLIGHT_MB. Unset: 256 MiB, up
    to a quarter of the free memory split between the running jobs (auto-tuned job), at most 4 GiB."""
    base = 256 * 1024 * 1024
    v = os.environ.get("BUILD_MAX_INFLIGHT_MB", "").strip()
    if v:
        try:
            return max(8, int(v)) * 1024 * 1024
        except ValueError:
            return base
    return max(base, min(4096 * 1024 * 1024, mem_available_bytes // (4 * max(1, running_jobs))))


def norm_hash(raw: bytes) -> str:
    """Line-ending-insensitive hash (the server stores CRLF)."""
    data = raw.replace(b"\r\n", b"\n").rstrip(b"\n")
    return hashlib.sha256(data).hexdigest()


@dataclass
class BuildTask:
    folder_index: int     # index in PstFile.folders() (same file, same order in every process)
    path: list            # folder path, cross-checked by the builder
    index: int            # item index in the folder
    key: str              # item key as seen by the job process, cross-checked
    import_id: str
    meta: object = None   # caller data, never sent to the builder


@dataclass
class BuildEvent:
    task: BuildTask               # or any task with run() (ordering.IndexTask)
    result: dict | None = None    # see build_one()
    crashed: int | None = None    # exit code of the builder that died holding this task (< 0: signal)
    note: str | None = None       # set when the pool itself killed the builder (e.g. broken pipe): why


def is_oom_kill(ev: BuildEvent) -> bool:
    """The builder holding this task was SIGKILLed by someone else than the pool: on Linux that is the
    kernel OOM killer (cgroup memory limit) in practice - not a libpff crash of the item."""
    return ev.crashed == -signal.SIGKILL and ev.note is None


# ------------------------------------------------------------------ builder process side
def build_one(eng, pst, folders, task: BuildTask) -> dict:
    """Reads and builds one mail. Result dict "status":
    ok (raw, subject, flags, internaldate, warnings, hash) | skipped | error (error) | rejected (errs)."""
    from . import crash
    try:
        f = folders[task.folder_index]
        if list(f.path) != list(task.path):
            raise RuntimeError("folder mismatch")
        gen = pst.iter_items(f, task.index)
        try:
            idx, key, handle = next(gen)
        finally:
            gen.close()
        if idx != task.index or key != task.key:
            raise RuntimeError("item key mismatch (%s != %s)" % (key, task.key))
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"construction: {type(e).__name__}: {e}"}
    # same test hooks as JobRunner._mark(key) used to fire before the libpff read of an item
    crash.maybe_delay()
    crash.maybe_crash(task.key, "*")
    crash.maybe_oom(task.key)
    try:
        built = pst.build_message(handle, task.import_id)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"construction: {type(e).__name__}: {e}"}
    if built is None:
        return {"status": "skipped"}
    return finish_built(eng, built, handle)


def finish_built(eng, built, handle) -> dict:
    """Verification + hash of a BuiltMessage (also used by the in-process path)."""
    try:
        errs = list(eng.verify_message(built, handle) or [])
    except Exception as e:  # noqa: BLE001
        errs = [f"verification: {type(e).__name__}: {e}"]
    if IMPORT_HEADER.lower().encode() not in built.raw[:65536].lower():
        errs.append(f"header {IMPORT_HEADER} missing")
    out = {"subject": built.subject, "warnings": list(built.warnings or [])}
    if errs:
        out.update(status="rejected", errs=errs)
        return out
    out.update(status="ok", raw=built.raw, flags=list(built.flags or []), internaldate=built.internaldate,
               hash=norm_hash(built.raw))
    return out


def _die_with_parent(parent_pid: int) -> None:
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:  # pragma: no cover - non-Linux
        return
    if os.getppid() != parent_pid:
        os._exit(1)  # parent already gone


def _builder_main(conn, pst_path: str, engine_module: str, in_child: bool, parent_pid: int) -> None:
    from . import crash
    crash.IN_CHILD = in_child  # crash/delay test hooks active only under the worker (like the job process)
    _die_with_parent(parent_pid)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        eng = importlib.import_module(engine_module)
        pst = eng.PstFile(pst_path)
        folders = pst.folders()
    except Exception as e:  # noqa: BLE001
        try:
            conn.send({"fatal": f"{type(e).__name__}: {e}"})
        except Exception:  # noqa: BLE001
            pass
        return
    conn.send({"ready": True})
    while True:
        try:
            task = conn.recv()
        except (EOFError, OSError):
            return
        if task is None:
            return
        run = getattr(task, "run", None)  # other read-only tasks (e.g. ordering.IndexTask) carry their code
        conn.send(run(eng, pst, folders) if run is not None else build_one(eng, pst, folders, task))


# ------------------------------------------------------------------ job process side
class BuilderError(Exception):
    """A builder cannot open the PST (not a crash): the job fails."""


class _Slot:
    __slots__ = ("proc", "conn", "task", "ready", "retire", "kill_note")

    def __init__(self):
        self.kill_note: str | None = None  # the pool killed this builder itself (reported with the crash)
        self.proc = None
        self.conn = None
        self.task: BuildTask | None = None
        self.ready = False
        self.retire = False  # shrink requested: no new task, leaves once its current task is reported


class BuilderPool:
    """N builder processes, at most one task each. wait() returns BuildEvents (result or crash).

    resize(n): growing starts processes; shrinking retires idle builders at once and busy ones only after
    their current task's result (or crash) has been reported by wait(): no task is lost or built twice."""

    def __init__(self, pst_path: str, n: int, in_child: bool, engine_module: str | None = None):
        if engine_module is None:
            from .config import settings
            engine_module = settings.engine_module
        self.pst_path = pst_path
        self.engine_module = engine_module
        self.in_child = in_child
        self.ctx = multiprocessing.get_context("spawn")
        self.slots: list[_Slot] = []
        self._leaving: list[_Slot] = []  # retired builders asked to exit, reaped lazily
        self.idle_deaths = 0
        self._busy_t = 0.0               # sum over time of busy builders (utilisation)
        self._cap_t = 0.0                # sum over time of builders
        self._last_t = time.monotonic()
        self.resize(max(1, n))

    def _start(self, s: _Slot) -> None:
        parent, child = self.ctx.Pipe(duplex=True)
        p = self.ctx.Process(target=_builder_main, name="builder",
                             args=(child, self.pst_path, self.engine_module, self.in_child, os.getpid()),
                             daemon=True)
        p.start()
        child.close()
        s.proc, s.conn, s.task, s.ready, s.kill_note = p, parent, None, False, None

    def _reap(self, s: _Slot) -> int | None:
        try:
            s.conn.close()
        except Exception:  # noqa: BLE001
            pass
        s.proc.join(5)
        if s.proc.exitcode is None:
            s.proc.kill()
            s.proc.join()
        return s.proc.exitcode

    def _active(self) -> list[_Slot]:
        return [s for s in self.slots if not s.retire]

    @property
    def size(self) -> int:
        """Current (target) number of builders: retiring ones excluded."""
        return len(self._active())

    def resize(self, n: int) -> None:
        n = max(1, int(n))
        self._account()
        active = self._active()
        if n > len(active):
            # cancel pending retirements first (busy builders asked to leave), then start new processes
            for s in self.slots:
                if s.retire and len(active) < n:
                    s.retire = False
                    active.append(s)
            while len(active) < n:
                s = _Slot()
                self._start(s)
                self.slots.append(s)
                active.append(s)
            return
        surplus = len(active) - n
        for s in sorted(active, key=lambda x: x.task is not None):  # idle builders leave first
            if surplus <= 0:
                break
            s.retire = True
            surplus -= 1
            if s.task is None:
                self._dismiss(s)

    def _dismiss(self, s: _Slot) -> None:
        """Retired builder with nothing in hand: asked to exit, removed from the pool."""
        try:
            s.conn.send(None)
        except Exception:  # noqa: BLE001
            pass
        self.slots.remove(s)
        self._leaving.append(s)

    def _reap_leaving(self, force: bool = False) -> None:
        for s in list(self._leaving):
            if force or not s.proc.is_alive():
                if force and s.proc.is_alive():
                    s.proc.join(1.0)
                    if s.proc.exitcode is None:
                        s.proc.kill()
                s.proc.join()
                try:
                    s.conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._leaving.remove(s)

    def _account(self) -> None:
        now = time.monotonic()
        dt = now - self._last_t
        self._last_t = now
        if dt > 0:
            self._busy_t += dt * self.in_flight()
            self._cap_t += dt * max(1, self.size)

    def take_utilisation(self) -> float | None:
        """Mean busy fraction of the builders since the previous call (None if no time elapsed)."""
        self._account()
        u = self._busy_t / self._cap_t if self._cap_t > 0 else None
        self._busy_t = self._cap_t = 0.0
        return None if u is None else min(1.0, u)

    def can_dispatch(self) -> bool:
        return any(s.task is None and not s.retire for s in self.slots)

    def busy(self) -> bool:
        return any(s.task is not None for s in self.slots)

    def in_flight(self) -> int:
        return sum(s.task is not None for s in self.slots)

    def dispatch(self, task: BuildTask) -> None:
        self._account()
        s = next(s for s in self.slots if s.task is None and not s.retire)
        meta, task.meta = task.meta, None
        try:
            s.conn.send(task)
        except (BrokenPipeError, OSError):
            pass  # dead builder: reported by wait() as a crash holding this task
        task.meta = meta
        s.task = task

    def wait(self, timeout: float) -> list[BuildEvent]:
        """Events of finished/crashed tasks (possibly none after timeout). Dead builders are respawned."""
        self._reap_leaving()
        watch = {}
        for s in self.slots:
            watch[s.conn] = s
            watch[s.proc.sentinel] = s
        events: list[BuildEvent] = []
        try:
            ready = multiprocessing.connection.wait(list(watch), timeout)
        except OSError:
            ready = list(watch)
        self._account()
        seen = set()
        for obj in ready:
            s = watch[obj]
            if id(s) in seen:
                continue
            seen.add(id(s))
            self._service(s, events)
        return events

    def _kill(self, s: _Slot, why: str) -> None:
        """The pipe of a live builder is unusable: kill it, so that the code below reports it as a crash
        holding its task (instead of a task held forever and wait() returning at once in a loop)."""
        if s.proc.is_alive():
            s.kill_note = why
            s.proc.kill()
            s.proc.join(5)

    def _service(self, s: _Slot, events: list[BuildEvent]) -> None:
        while True:
            try:
                if not s.conn.poll():
                    break
                msg = s.conn.recv()
            except EOFError:  # the builder closed its end: it is exiting (or already dead)
                s.proc.join(2.0)
                self._kill(s, "pipe closed but process still alive")
                break
            except Exception as e:  # noqa: BLE001 - truncated/corrupt message, OSError...
                s.proc.join(0.5)  # died mid-send: its real exit code is kept
                self._kill(s, f"result unreadable ({type(e).__name__}: {e})")
                break
            if isinstance(msg, dict) and "fatal" in msg:
                raise BuilderError(f"builder cannot open the PST: {msg['fatal']}")
            if isinstance(msg, dict) and msg.get("ready"):
                s.ready = True
                continue
            if s.task is not None:
                events.append(BuildEvent(s.task, result=msg))
                s.task = None
        if not s.proc.is_alive():
            rc = self._reap(s)
            task, s.task = s.task, None
            if task is not None:
                events.append(BuildEvent(task, crashed=rc, note=s.kill_note))
            elif not s.retire:
                self.idle_deaths += 1
                if self.idle_deaths > 3 * len(self.slots) + 3:
                    raise BuilderError(f"builder processes keep dying at start (exit {rc})")
            if s.retire:
                self.slots.remove(s)  # retiring: not respawned
            else:
                self._start(s)
            return
        if s.retire and s.task is None:
            self._dismiss(s)  # its last task has just been reported

    def close(self) -> None:
        """Stops every builder at once: in-flight builds are discarded (nothing was recorded for them)."""
        for s in self.slots:
            if s.proc is None:
                continue
            if s.task is None:
                try:
                    s.conn.send(None)
                except Exception:  # noqa: BLE001
                    pass
            else:
                s.proc.kill()  # read-only work: nothing to lose
        deadline = time.time() + 2.0
        for s in self.slots:
            if s.proc is None:
                continue
            s.proc.join(max(0.0, deadline - time.time()))
            if s.proc.exitcode is None:
                s.proc.kill()
                s.proc.join()
            try:
                s.conn.close()
            except Exception:  # noqa: BLE001
                pass
            s.task = None
        self._reap_leaving(force=True)
