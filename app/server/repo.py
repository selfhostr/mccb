"""Reading/serialization of the objects exposed by the API (API Contract format)."""
from __future__ import annotations

import time
from typing import Any

from . import db

JOB_ACTIVE = ("queued", "running", "paused", "stopping")
JOB_FINAL = ("stopped", "done", "done_with_errors", "failed")


def empty_progress() -> dict:
    return {k: {"done": 0, "total": 0, "errors": 0} for k in ("mail", "calendar", "contacts")}


def server_view(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "imap_host": r["imap_host"], "imap_port": r["imap_port"],
        "imap_security": r["imap_security"], "verify_tls": bool(r["verify_tls"]),
        "root_folder": r["root_folder"] or None, "caldav_url": r["caldav_url"] or None,
        "carddav_url": r["carddav_url"] or None, "dav_enabled": bool(r["dav_enabled"]),
        "workers": r["workers"], "notes": r["notes"] or "",
        "tuning": (r["tuning"] if "tuning" in r.keys() else None) or "auto",
        "builders": r["builders"] if "builders" in r.keys() else None,
    }


def get_server(sid: int) -> dict | None:
    r = db.q1("SELECT * FROM servers WHERE id=?", (sid,))
    return server_view(r) if r else None


def job_view(r) -> dict:
    prog = db.jload(r["progress"], None) or empty_progress()
    return {
        "id": r["id"], "account_id": r["account_id"], "status": r["status"], "phase": r["phase"],
        "progress": prog, "current_folder": r["current_folder"], "rate": r["rate"],
        # byte rate only alongside an item rate (rate is reset to NULL on pause/crash, rate_bytes may lag)
        "rate_bytes": (r["rate_bytes"] if "rate_bytes" in r.keys() else None) if r["rate"] is not None else None,
        "eta": r["eta"],
        "started_at": r["started_at"], "finished_at": r["finished_at"], "error": r["error"],
        "mode": r["mode"], "created_at": r["created_at"],
        # current pipeline sizes {mode: auto|manual, builders, connections, fallback} (None before the job starts)
        "tuning": db.jload(r["tuning"], None) if "tuning" in r.keys() else None,
    }


def get_job(jid: int) -> dict | None:
    r = db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
    return job_view(r) if r else None


def latest_job_row(account_id: int):
    return db.q1("SELECT * FROM jobs WHERE account_id=? ORDER BY id DESC LIMIT 1", (account_id,))


def account_view(r, with_job: bool = True) -> dict:
    job = None
    if with_job:
        jr = latest_job_row(r["id"])
        job = job_view(jr) if jr else None
    return {
        "id": r["id"], "server_id": r["server_id"], "email": r["email"], "login": r["login"] or r["email"],
        "has_password": bool(r["password_enc"]), "status": r["status"], "last_test": r["last_test"],
        "last_test_result": db.jload(r["last_test_result"], None),
        "pst_id": r["pst_id"],
        "options": {"mail": bool(r["opt_mail"]), "calendar": bool(r["opt_calendar"]), "contacts": bool(r["opt_contacts"])},
        "job": job,
    }


def get_account(aid: int) -> dict | None:
    r = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    return account_view(r) if r else None


def received_chunks(pst_id: int) -> list[int]:
    return [r["n"] for r in db.q("SELECT n FROM upload_chunks WHERE pst_id=? ORDER BY n", (pst_id,))]


def pst_view(r) -> dict:
    return {
        "id": r["id"], "filename": r["filename"], "size": r["size"], "status": r["status"],
        "scan": db.jload(r["scan"], None), "error": r["error"], "pst_sha": r["pst_sha"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def get_pst(pid: int) -> dict | None:
    r = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    return pst_view(r) if r else None


def upload_view(r) -> dict:
    return {
        "id": r["id"], "filename": r["filename"], "size": r["size"], "chunk_size": r["chunk_size"],
        "received": received_chunks(r["id"]), "status": r["status"],
        "chunks": n_chunks(r["size"], r["chunk_size"]), "error": r["error"],
    }


def get_upload(pid: int) -> dict | None:
    r = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    return upload_view(r) if r else None


def n_chunks(size: int, chunk_size: int) -> int:
    return max(1, -(-size // chunk_size)) if size > 0 else 0


def log(job_id: int, msg: str, level: str = "info") -> None:
    from .config import settings
    ts = time.time()
    db.ex("INSERT INTO job_logs(job_id, ts, level, msg) VALUES (?,?,?,?)", (job_id, ts, level, msg))
    try:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(settings.logs_dir / f"job-{job_id}.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} [{level.upper()}] {msg}\n")
    except OSError:
        pass


def event_object(kind: str, ref_id: int) -> Any:
    if kind == "job":
        return get_job(ref_id)
    if kind == "account":
        return get_account(ref_id)
    if kind == "pst":
        return get_pst(ref_id)
    if kind == "upload":
        return get_upload(ref_id)
    return None
