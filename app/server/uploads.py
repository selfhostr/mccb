"""Resumable upload: pre-allocated file, chunks written at their offset, SHA-256 per chunk."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time

from fastapi import HTTPException, Request

from . import db, repo
from .config import settings

WRITE_BUF = 4 * 1024 * 1024


def free_space() -> int:
    settings.ensure_dirs()
    return shutil.disk_usage(settings.pst_dir).free


_FALLOC_CACHE: dict[str, bool] = {}


def _fallocate_supported() -> bool:
    """Probe (cached): does the pst_dir FS support posix_fallocate?"""
    settings.ensure_dirs()
    key = str(settings.pst_dir)
    if key not in _FALLOC_CACHE:
        probe = settings.pst_dir / ".falloc_probe"
        ok = False
        try:
            fd = os.open(str(probe), os.O_WRONLY | os.O_CREAT, 0o600)
            try:
                os.posix_fallocate(fd, 0, 4096)
                ok = True
            finally:
                os.close(fd)
        except (OSError, AttributeError):
            ok = False
        finally:
            try:
                os.unlink(probe)
            except OSError:
                pass
        _FALLOC_CACHE[key] = ok
    return _FALLOC_CACHE[key]


def _remaining(row) -> int:
    got = db.q1("SELECT COUNT(*) AS c FROM upload_chunks WHERE pst_id=?", (row["id"],))["c"]
    return max(0, row["size"] - got * row["chunk_size"])


def _others_remaining(exclude_id: int | None) -> int:
    rows = db.q("SELECT id,size,chunk_size FROM psts WHERE status='uploading' AND id IS NOT ?",
                (exclude_id,))
    return sum(_remaining(r) for r in rows)


def _check_space(need: int, exclude_id: int | None) -> None:
    """Refuse (507) if space is insufficient. Without fallocate, sparse files do not
    reserve anything: subtract what remains to be received from the other uploads in progress."""
    free = free_space()
    reserved = 0 if _fallocate_supported() else _others_remaining(exclude_id)
    avail = free - reserved
    need += settings.disk_margin
    if avail < need:
        raise HTTPException(507, f"insufficient disk space: {max(avail, 0) // 2**20} MB available "
                                 f"(after uploads in progress), {need // 2**20} MB required")


def _preallocate(path: str, size: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o640)
    try:
        if size > 0:
            try:
                os.posix_fallocate(fd, 0, size)
            except (OSError, AttributeError):
                os.ftruncate(fd, size)
    finally:
        os.close(fd)


def create_or_resume(filename: str, size: int, last_modified: int | None) -> dict:
    filename = os.path.basename(filename.replace("\\", "/")).strip() or "file.pst"
    if size <= 0:
        raise HTTPException(422, "invalid size")
    existing = db.q1(
        "SELECT * FROM psts WHERE filename=? AND size=? AND COALESCE(last_modified,-1)=COALESCE(?,-1) "
        "ORDER BY (status='invalid'), id DESC LIMIT 1",
        (filename, size, last_modified),
    )
    if existing is not None and existing["status"] != "invalid":
        if existing["status"] == "uploading":
            if not os.path.exists(existing["path"]):
                _check_space(size, existing["id"])
                _preallocate(existing["path"], size)
                db.ex("DELETE FROM upload_chunks WHERE pst_id=?", (existing["id"],))
            elif not _fallocate_supported():
                _check_space(_remaining(existing), existing["id"])
        return repo.upload_view(existing)
    _check_space(size, None)
    now = time.time()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO psts(filename,size,last_modified,chunk_size,status,created_at,updated_at) "
            "VALUES (?,?,?,?, 'uploading', ?, ?)",
            (filename, size, last_modified, settings.chunk_size, now, now),
        )
        pid = cur.lastrowid
        path = str(settings.pst_dir / f"{pid}.pst")
        c.execute("UPDATE psts SET path=? WHERE id=?", (path, pid))
    try:
        _preallocate(path, size)
    except OSError as e:
        db.ex("DELETE FROM psts WHERE id=?", (pid,))
        raise HTTPException(507, f"pre-allocation failed: {e}")
    return repo.get_upload(pid)  # type: ignore[return-value]


def _write_at(fd: int, buf: bytes, offset: int) -> None:
    view = memoryview(buf)
    while view:
        n = os.pwrite(fd, view, offset)
        view = view[n:]
        offset += n


_CHUNK_LOCKS: dict[tuple[int, int], tuple[asyncio.Lock, int]] = {}


async def put_chunk(pid: int, n: int, request: Request) -> dict:
    """Serializes concurrent sends of the same chunk (same offset); the rest is parallel."""
    key = (pid, n)
    lock, users = _CHUNK_LOCKS.get(key) or (asyncio.Lock(), 0)
    _CHUNK_LOCKS[key] = (lock, users + 1)
    try:
        async with lock:
            return await _put_chunk(pid, n, request)
    finally:
        lock, users = _CHUNK_LOCKS[key]
        if users <= 1:
            del _CHUNK_LOCKS[key]
        else:
            _CHUNK_LOCKS[key] = (lock, users - 1)


def _record_chunk(pid: int, n: int, sha: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO upload_chunks(pst_id, n, sha256) VALUES (?,?,?) "
                  "ON CONFLICT(pst_id, n) DO UPDATE SET sha256=excluded.sha256", (pid, n, sha))
        c.execute("UPDATE psts SET updated_at=? WHERE id=?", (time.time(), pid))


def _finalized_reply(pid: int, n: int, sha: str) -> bool:
    prev = db.q1("SELECT sha256 FROM upload_chunks WHERE pst_id=? AND n=?", (pid, n))
    return bool(prev and prev["sha256"] == sha)


def _hash_and_write(h, fd: int, data: bytearray, offset: int) -> None:
    """In a worker thread: the event loop keeps serving requests, parallel chunks hash on separate cores."""
    h.update(data)
    _write_at(fd, data, offset)


async def _put_chunk(pid: int, n: int, request: Request) -> dict:
    row = await asyncio.to_thread(db.q1, "SELECT * FROM psts WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "unknown upload")
    size, cs = row["size"], row["chunk_size"]
    total = repo.n_chunks(size, cs)
    if n < 0 or n >= total:
        raise HTTPException(416, f"chunk {n} out of range (0..{total - 1})")
    expected_sha = (request.headers.get("x-chunk-sha256") or "").strip().lower()
    if len(expected_sha) != 64:
        raise HTTPException(400, "missing or invalid X-Chunk-Sha256 header")
    offset = n * cs
    expected_len = min(cs, size - offset)
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) != expected_len:
        raise HTTPException(422, f"incorrect size for chunk {n}: {cl} instead of {expected_len}")

    if row["status"] != "uploading":
        async for _ in request.stream():
            pass
        if await asyncio.to_thread(_finalized_reply, pid, n, expected_sha):
            return {"n": n, "ok": True}
        raise HTTPException(409, f"upload already finalized (status {row['status']})")

    h = hashlib.sha256()
    written = 0
    fd = await asyncio.to_thread(os.open, row["path"], os.O_WRONLY)
    try:
        buf = bytearray()
        async for part in request.stream():
            if not part:
                continue
            if written + len(buf) + len(part) > expected_len:
                raise HTTPException(422, f"chunk {n} too long (> {expected_len} bytes)")
            buf += part
            if len(buf) >= WRITE_BUF:
                data, buf = buf, bytearray()
                await asyncio.to_thread(_hash_and_write, h, fd, data, offset + written)
                written += len(data)
        if buf:
            await asyncio.to_thread(_hash_and_write, h, fd, buf, offset + written)
            written += len(buf)
        if written != expected_len:
            raise HTTPException(422, f"chunk {n} incomplete: {written}/{expected_len} bytes")
        got = h.hexdigest()
        if got != expected_sha:
            raise HTTPException(422, f"incorrect SHA-256 for chunk {n} (received {got})")
        await asyncio.to_thread(os.fdatasync, fd)
    finally:
        os.close(fd)
    await asyncio.to_thread(_record_chunk, pid, n, got)
    # no list of received chunks here: 16,000 chunks for 500 GB, read and sent again for each of them
    # (GET /api/uploads/{id} gives it)
    return {"n": n, "ok": True}


def complete(pid: int) -> dict:
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "unknown upload")
    if row["status"] in ("validating", "valid"):
        return repo.get_upload(pid)  # type: ignore[return-value]
    total = repo.n_chunks(row["size"], row["chunk_size"])
    got = set(repo.received_chunks(pid))
    missing = [i for i in range(total) if i not in got]
    if missing:
        raise HTTPException(409, {"msg": f"{len(missing)} missing chunk(s)", "missing": missing[:1000]})
    try:
        actual = os.path.getsize(row["path"])
    except OSError:
        actual = -1
    if actual != row["size"]:
        db.ex("UPDATE psts SET status='invalid', error=?, updated_at=? WHERE id=?",
              (f"final size {actual} != {row['size']}", time.time(), pid))
        return repo.get_upload(pid)  # type: ignore[return-value]
    shas = [r["sha256"] for r in db.q("SELECT sha256 FROM upload_chunks WHERE pst_id=? ORDER BY n", (pid,))]
    pst_sha = hashlib.sha256((":".join(shas) + f":{row['size']}").encode()).hexdigest()[:16]
    db.ex("UPDATE psts SET status='validating', error=NULL, pst_sha=?, updated_at=? WHERE id=?",
          (pst_sha, time.time(), pid))
    return repo.get_upload(pid)  # type: ignore[return-value]


def delete_pst(pid: int) -> None:
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "unknown PST")
    busy = db.q1(
        "SELECT j.id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE a.pst_id=? "
        "AND j.status IN ('queued','running','paused','stopping')", (pid,))
    if busy:
        raise HTTPException(409, f"PST in use by running job {busy['id']}")
    if row["path"]:
        try:
            os.unlink(row["path"])
        except FileNotFoundError:
            pass
    db.ex("DELETE FROM psts WHERE id=?", (pid,))


def _scan_other_file(path: str, filename: str) -> dict:
    """Analysis of a file that is not a PST. Raises ValueError, with the PST message first, when it is none of the
    supported formats."""
    from ..connectors import files
    if not files.looks_like_files(path, filename):
        raise ValueError("PST signature '!BDN' missing: this is not a PST file, nor a supported mbox, .eml, .msg, "
                         ".ics, .vcf or .zip file")
    try:
        return files.scan_upload(path, filename)
    except (ValueError, OSError) as e:
        raise ValueError(f"not a PST; as a mail, calendar or contact file: {e}") from e


def validate(pid: int) -> None:
    """Run by the worker: signature + full scan by the engine."""
    from .engine_loader import check_signature, engine
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None or row["status"] != "validating":
        return
    path = row["path"]
    try:
        if not check_signature(path):
            scan = _scan_other_file(path, row["filename"])  # mbox, Maildir, .eml...: see connectors/files.py
            db.ex("UPDATE psts SET status='valid', scan=?, error=NULL, updated_at=? WHERE id=? AND status='validating'",
                  (db.jdump(scan), time.time(), pid))
            from ..connectors import wiring
            wiring.sync_files_sources_of(pid)
            return
        eng = engine()
        from .crash import maybe_crash
        maybe_crash("validate")  # test hook (subprocess only)
        with eng.PstFile(path) as pst:
            scan = pst.scan()
        status, err = "valid", None
        if not scan or not isinstance(scan, dict) or "totals" not in scan:
            status, err = "invalid", "PST scan empty or unreadable"
    except Exception as e:
        scan, status, err = None, "invalid", f"{type(e).__name__}: {e}"
    db.ex("UPDATE psts SET status=?, scan=?, error=?, updated_at=? WHERE id=? AND status='validating'",
          (status, db.jdump(scan) if scan is not None else None, err, time.time(), pid))
