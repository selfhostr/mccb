"""Job creation, shared by POST /api/runs and the link + start of a source connection (api_sources.py)."""
from __future__ import annotations

import time

from ..connectors.wiring import source_problem
from . import db, repo


def create_jobs(account_ids: list[int], name: str | None = None, mode: str = "full",
                priority: int = 0) -> tuple[list[dict], list[dict], int | None]:
    """(jobs, errors, batch_id). An account with an active job gives that job back; the batch is created with the
    first new job only."""
    jobs, errors = [], []
    batch_id = None
    for aid in account_ids:
        a = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
        if a is None:
            errors.append({"account_id": aid, "msg": "unknown mailbox"})
            continue
        if not a["password_enc"] and not a["target_kind"]:
            errors.append({"account_id": aid, "msg": "password missing"})
            continue
        problem = source_problem(a)
        if problem:
            errors.append({"account_id": aid, "msg": problem})
            continue
        active = db.q1("SELECT * FROM jobs WHERE account_id=? AND status IN ('queued','running','paused','stopping') "
                       "ORDER BY id DESC LIMIT 1", (aid,))
        if active:
            jobs.append(repo.job_view(active))
            continue
        if name and name.strip() and batch_id is None:
            batch_id = db.ex("INSERT INTO batches(name, created_at) VALUES (?, ?)",
                             (name.strip(), time.time())).lastrowid
        cur = db.ex("INSERT INTO jobs(account_id, status, phase, progress, mode, batch_id, priority, created_at) "
                    "VALUES (?, 'queued', 'precheck', ?, ?, ?, ?, ?)",
                    (aid, db.jdump(repo.empty_progress()), mode, batch_id, priority, time.time()))
        repo.log(cur.lastrowid, "Job queued" + (" (final resync)" if mode == "delta" else ""))
        jobs.append(repo.get_job(cur.lastrowid))
    return jobs, errors, batch_id
