"""Upload reprenable : fichier pré-alloué, chunks écrits à leur offset, SHA-256 par chunk."""
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
    """Sonde (mise en cache) : le FS de pst_dir gère-t-il posix_fallocate ?"""
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
    """Refuse (507) si l'espace est insuffisant. Sans fallocate, les fichiers sparse ne
    réservent rien : on retranche ce qu'il reste à recevoir des autres uploads en cours."""
    free = free_space()
    reserved = 0 if _fallocate_supported() else _others_remaining(exclude_id)
    avail = free - reserved
    need += settings.disk_margin
    if avail < need:
        raise HTTPException(507, f"espace disque insuffisant : {max(avail, 0) // 2**20} Mo disponibles "
                                 f"(après uploads en cours), {need // 2**20} Mo nécessaires")


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
    filename = os.path.basename(filename.replace("\\", "/")).strip() or "fichier.pst"
    if size <= 0:
        raise HTTPException(422, "taille invalide")
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
        raise HTTPException(507, f"pré-allocation impossible : {e}")
    return repo.get_upload(pid)  # type: ignore[return-value]


def _write_at(fd: int, buf: bytes, offset: int) -> None:
    view = memoryview(buf)
    while view:
        n = os.pwrite(fd, view, offset)
        view = view[n:]
        offset += n


_CHUNK_LOCKS: dict[tuple[int, int], tuple[asyncio.Lock, int]] = {}


async def put_chunk(pid: int, n: int, request: Request) -> dict:
    """Sérialise les envois concurrents du même chunk (même offset) ; le reste est parallèle."""
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


def _record_chunk(pid: int, n: int, sha: str) -> list[int]:
    db.ex("INSERT INTO upload_chunks(pst_id, n, sha256) VALUES (?,?,?) "
          "ON CONFLICT(pst_id, n) DO UPDATE SET sha256=excluded.sha256", (pid, n, sha))
    db.ex("UPDATE psts SET updated_at=? WHERE id=?", (time.time(), pid))
    return repo.received_chunks(pid)


def _finalized_reply(pid: int, n: int, sha: str) -> list[int] | None:
    prev = db.q1("SELECT sha256 FROM upload_chunks WHERE pst_id=? AND n=?", (pid, n))
    if prev and prev["sha256"] == sha:
        return repo.received_chunks(pid)
    return None


async def _put_chunk(pid: int, n: int, request: Request) -> dict:
    row = await asyncio.to_thread(db.q1, "SELECT * FROM psts WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "upload inconnu")
    size, cs = row["size"], row["chunk_size"]
    total = repo.n_chunks(size, cs)
    if n < 0 or n >= total:
        raise HTTPException(416, f"chunk {n} hors limites (0..{total - 1})")
    expected_sha = (request.headers.get("x-chunk-sha256") or "").strip().lower()
    if len(expected_sha) != 64:
        raise HTTPException(400, "en-tête X-Chunk-Sha256 manquant ou invalide")
    offset = n * cs
    expected_len = min(cs, size - offset)
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) != expected_len:
        raise HTTPException(422, f"taille du chunk {n} incorrecte : {cl} au lieu de {expected_len}")

    if row["status"] != "uploading":
        async for _ in request.stream():
            pass
        rec = await asyncio.to_thread(_finalized_reply, pid, n, expected_sha)
        if rec is not None:
            return {"n": n, "ok": True, "received": rec}
        raise HTTPException(409, f"upload déjà finalisé (statut {row['status']})")

    h = hashlib.sha256()
    written = 0
    fd = await asyncio.to_thread(os.open, row["path"], os.O_WRONLY)
    try:
        buf = bytearray()
        async for part in request.stream():
            if not part:
                continue
            if written + len(buf) + len(part) > expected_len:
                raise HTTPException(422, f"chunk {n} trop long (> {expected_len} octets)")
            h.update(part)
            buf += part
            if len(buf) >= WRITE_BUF:
                data = bytes(buf)
                buf.clear()
                await asyncio.to_thread(_write_at, fd, data, offset + written)
                written += len(data)
        if buf:
            data = bytes(buf)
            await asyncio.to_thread(_write_at, fd, data, offset + written)
            written += len(data)
        if written != expected_len:
            raise HTTPException(422, f"chunk {n} incomplet : {written}/{expected_len} octets")
        got = h.hexdigest()
        if got != expected_sha:
            raise HTTPException(422, f"SHA-256 du chunk {n} incorrect (reçu {got})")
        await asyncio.to_thread(os.fdatasync, fd)
    finally:
        os.close(fd)
    received = await asyncio.to_thread(_record_chunk, pid, n, got)
    return {"n": n, "ok": True, "received": received}


def complete(pid: int) -> dict:
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "upload inconnu")
    if row["status"] in ("validating", "valid"):
        return repo.get_upload(pid)  # type: ignore[return-value]
    total = repo.n_chunks(row["size"], row["chunk_size"])
    got = set(repo.received_chunks(pid))
    missing = [i for i in range(total) if i not in got]
    if missing:
        raise HTTPException(409, {"msg": f"{len(missing)} chunk(s) manquant(s)", "missing": missing[:1000]})
    try:
        actual = os.path.getsize(row["path"])
    except OSError:
        actual = -1
    if actual != row["size"]:
        db.ex("UPDATE psts SET status='invalid', error=?, updated_at=? WHERE id=?",
              (f"taille finale {actual} ≠ {row['size']}", time.time(), pid))
        return repo.get_upload(pid)  # type: ignore[return-value]
    shas = [r["sha256"] for r in db.q("SELECT sha256 FROM upload_chunks WHERE pst_id=? ORDER BY n", (pid,))]
    pst_sha = hashlib.sha256((":".join(shas) + f":{row['size']}").encode()).hexdigest()[:16]
    db.ex("UPDATE psts SET status='validating', error=NULL, pst_sha=?, updated_at=? WHERE id=?",
          (pst_sha, time.time(), pid))
    return repo.get_upload(pid)  # type: ignore[return-value]


def delete_pst(pid: int) -> None:
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "PST inconnu")
    busy = db.q1(
        "SELECT j.id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE a.pst_id=? "
        "AND j.status IN ('queued','running','paused','stopping')", (pid,))
    if busy:
        raise HTTPException(409, f"PST utilisé par le job {busy['id']} en cours")
    if row["path"]:
        try:
            os.unlink(row["path"])
        except FileNotFoundError:
            pass
    db.ex("DELETE FROM psts WHERE id=?", (pid,))


def validate(pid: int) -> None:
    """Exécuté par le worker : signature + scan complet par l'engine."""
    from .engine_loader import check_signature, engine
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None or row["status"] != "validating":
        return
    path = row["path"]
    try:
        if not check_signature(path):
            raise ValueError("signature PST « !BDN » absente : ce n'est pas un fichier PST")
        eng = engine()
        from .crash import maybe_crash
        maybe_crash("validate")  # crochet de test (sous-processus uniquement)
        with eng.PstFile(path) as pst:
            scan = pst.scan()
        status, err = "valid", None
        if not scan or not isinstance(scan, dict) or "totals" not in scan:
            status, err = "invalid", "scan PST vide ou illisible"
    except Exception as e:
        scan, status, err = None, "invalid", f"{type(e).__name__}: {e}"
    db.ex("UPDATE psts SET status=?, scan=?, error=?, updated_at=? WHERE id=? AND status='validating'",
          (status, db.jdump(scan) if scan is not None else None, err, time.time(), pid))
