"""Import order newest -> oldest: indexing of item dates + global sort across folders.

At the start of each item phase the job indexes the items it still has to import: for every item only
``(sort value, item_key, folder index, item index)`` is kept (no MIME, no body), then everything is sorted
across ALL the folders of the phase:

- mail: PR_MESSAGE_DELIVERY_TIME, else PR_CLIENT_SUBMIT_TIME, else PR_CREATION_TIME;
- calendar: start; contacts: last modification, else creation (see engine ``PstFile.item_date``);
- items without a date go last; ties are broken by item_key (deterministic: a resume yields the same order).

The order is not persisted: it is recomputed on every run (the PST is read-only, the sort deterministic),
already imported items are filtered out while indexing, so a resume continues with the remaining items in
the same relative order. Item keys and folder keys are unchanged (state of earlier versions is reused).

Crash isolation: the dates are read in builder processes (see builders.py), by chunks. A builder that dies
on a chunk makes it retried item by item; an item whose date read kills a builder gets no date (it is
still imported, last); only an item whose mere fetch kills a builder again is reported to the caller
(``on_fetch_crash``: same handling as a fetch crash during iteration, index skipped).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

NO_DATE = float("inf")  # sort value of items without a date (descending dates = ascending negated timestamps)
CHUNK = 256             # items per indexing task


class IndexingError(Exception):
    """The engine cannot iterate a folder (not a crash)."""


@dataclass
class IndexTask:
    """Reads (index, key, date) of items [start, stop) of one folder. Runs in a builder process."""
    folder_index: int
    path: list
    kind: str             # "mail" | "event" | "contact"
    start: int
    stop: int
    key_only: bool = False  # retry of an item whose date read crashed: fetch only
    meta: object = None     # caller data, never sent to the builder

    def run(self, eng, pst, folders) -> dict:
        from . import crash
        try:
            f = folders[self.folder_index]
            if list(f.path) != list(self.path):
                raise RuntimeError("folder mismatch")
        except Exception as e:  # noqa: BLE001
            return {"index_error": f"{type(e).__name__}: {e}"}
        date_of = getattr(pst, "item_date", None)
        out: list[tuple[int, str, float | None]] = []
        gen = pst.iter_items(f, self.start)
        try:
            i = self.start
            while i < self.stop:
                crash.maybe_crash(f"fetch:{i}")  # same test hook as the job process' iteration marker
                try:
                    idx, key, handle = next(gen)
                except StopIteration:
                    break
                ts = None
                if not self.key_only and date_of is not None:
                    crash.maybe_crash(f"date:{key}")
                    try:
                        d = date_of(handle, self.kind)
                        ts = d.timestamp() if d is not None else None
                    except Exception:  # noqa: BLE001 - unreadable date: sorted last
                        ts = None
                out.append((idx, key, ts))
                i = idx + 1
        except Exception as e:  # noqa: BLE001
            return {"items": out, "index_error": f"{type(e).__name__}: {e}"}
        finally:
            gen.close()
        return {"items": out}


def sort_value(ts: float | None) -> float:
    return -ts if ts is not None else NO_DATE


@dataclass
class FolderSpec:
    folder_index: int
    folder: object                        # FolderInfo
    fkey: str
    skips: set = field(default_factory=set)  # item indexes never to read (known to crash libpff)


@dataclass
class IndexStats:
    seen: int = 0          # items read
    kept: int = 0          # items to import
    undated: int = 0       # kept items without a date
    date_crashes: int = 0  # items whose date read crashed a builder
    seconds: float = 0.0


def _chunks(spec: FolderSpec, kind: str, chunk: int) -> list[IndexTask]:
    """Tasks covering [0, count) without the skipped indexes."""
    tasks, count, start = [], int(spec.folder.count or 0), 0
    path = list(spec.folder.path)
    while start < count:
        if start in spec.skips:
            start += 1
            continue
        stop = start
        while stop < count and stop - start < chunk and stop not in spec.skips:
            stop += 1
        tasks.append(IndexTask(spec.folder_index, path, kind, start, stop, meta=spec))
        start = stop
    return tasks


def build_index(pool, specs: list[FolderSpec], kind: str, keep: Callable[[FolderSpec, int, str], bool],
                check: Callable[[], None], progress: Callable[[int, int], None],
                on_fetch_crash: Callable[[FolderSpec, int, int | None], None],
                begin_folder: Callable[[FolderSpec], None] | None = None,
                end_folder: Callable[[FolderSpec], None] | None = None,
                chunk: int = CHUNK) -> tuple[list[tuple[float, str, int, int]], IndexStats]:
    """Indexes the folders (one after the other, chunks of a folder in parallel on the pool) and returns
    the sorted entries ``(sort value, item_key, folder index, item index)`` of the items ``keep()`` accepts,
    plus statistics. ``begin_folder``/``end_folder`` frame the work of each folder (per-folder state of the
    caller, e.g. its DB rows, stays bounded to one folder)."""
    t0 = time.monotonic()
    stats = IndexStats()
    total = sum(int(s.folder.count or 0) for s in specs)
    entries: list[tuple[float, str, int, int]] = []
    progress(0, total)
    for spec in specs:
        if begin_folder is not None:
            begin_folder(spec)
        todo = _chunks(spec, kind, chunk)
        todo.reverse()  # used as a stack: pop() = next chunk in order
        while todo or pool.busy():
            check()
            while todo and pool.can_dispatch():
                pool.dispatch(todo.pop())
            for ev in pool.wait(0.2):
                t: IndexTask = ev.task
                if ev.crashed is not None:
                    if t.stop - t.start > 1:
                        # retried item by item: only the culprit is affected
                        todo.extend(IndexTask(t.folder_index, t.path, kind, i, i + 1, meta=t.meta)
                                    for i in range(t.stop - 1, t.start - 1, -1))
                    elif not t.key_only:
                        stats.date_crashes += 1
                        todo.append(IndexTask(t.folder_index, t.path, kind, t.start, t.stop, key_only=True,
                                              meta=t.meta))
                    else:
                        stats.seen += 1
                        stats.date_crashes -= 1  # the fetch itself crashes, not (only) the date read
                        on_fetch_crash(t.meta, t.start, ev.crashed)
                    continue
                res = ev.result or {}
                for idx, key, ts in res.get("items", ()):
                    stats.seen += 1
                    if keep(t.meta, idx, key):
                        stats.kept += 1
                        if ts is None:
                            stats.undated += 1
                        entries.append((sort_value(ts), key, t.folder_index, idx))
                if res.get("index_error"):
                    # not a crash: the engine could not iterate (its per-item fallback normally prevents
                    # this). Same outcome as an iteration failure before indexing existed: the job fails.
                    raise IndexingError(f"folder '{t.meta.fkey}': cannot read items from no. {t.start}: "
                                      f"{res['index_error']}")
                progress(min(stats.seen, total), total)
        if end_folder is not None:
            end_folder(spec)
    entries.sort()
    stats.seconds = time.monotonic() - t0
    return entries, stats
