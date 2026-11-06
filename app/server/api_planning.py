"""API of the planning tools: dry run, policies, batches, queue order, comparison, filtered items.
See docs/planning.md and docs/api.md."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import csvaccounts, crash, db, planning, policy, repo

router = APIRouter(prefix="/api")
ACTIVE = "('queued','running','paused','stopping')"


# ======================================================================= dry run
class PlanIn(BaseModel):
    account_ids: list[int] = Field(min_length=1, max_length=2000)


@router.post("/runs/plan", status_code=202)
def create_plan(body: PlanIn):
    """Dry run: nothing is written to any target. Runs in the worker; poll GET /api/plans/{id} (or follow the
    `plan` events of /api/events)."""
    ids = list(dict.fromkeys(body.account_ids))
    known = {r["id"] for r in db.q(f"SELECT id FROM accounts WHERE id IN ({','.join('?' * len(ids))})", ids)}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(422, {"msg": "unknown account(s)", "account_ids": unknown})
    now = time.time()
    cur = db.ex("INSERT INTO plans(status, request, created_at, updated_at) VALUES ('queued', ?, ?, ?)",
                (db.jdump({"account_ids": ids}), now, now))
    return planning.plan_view(db.q1("SELECT * FROM plans WHERE id=?", (cur.lastrowid,)), full=False)


@router.get("/plans")
def list_plans(limit: int = 20):
    rows = db.q("SELECT * FROM plans ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))
    return [planning.plan_view(r, full=False) for r in rows]


@router.get("/plans/{pid}")
def get_plan(pid: int):
    r = db.q1("SELECT * FROM plans WHERE id=?", (pid,))
    if r is None:
        raise HTTPException(404, "unknown plan")
    return planning.plan_view(r)


# ======================================================================= policies
def _policy_get(row) -> dict:
    return {"policy": db.jload(row["policy"], None)}


@router.get("/servers/{sid}/policy")
def get_server_policy(sid: int):
    r = db.q1("SELECT * FROM servers WHERE id=?", (sid,))
    if r is None:
        raise HTTPException(404, "unknown server")
    return _policy_get(r)


@router.put("/servers/{sid}/policy")
def put_server_policy(sid: int, body: dict[str, Any] | None = Body(None)):
    """Replaces the policy of the server (folder_map, exclude_folders, date_from, date_to, max_size_mb, window,
    rate_mb_s). `null` or `{}` clears it. A running job keeps the policy it started with, except the allowed
    hours and the bandwidth limit, which it re-reads."""
    if db.q1("SELECT id FROM servers WHERE id=?", (sid,)) is None:
        raise HTTPException(404, "unknown server")
    try:
        doc = policy.normalize(body, server=True)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.ex("UPDATE servers SET policy=? WHERE id=?", (db.jdump(doc) if doc else None, sid))
    return _policy_get(db.q1("SELECT * FROM servers WHERE id=?", (sid,)))


@router.get("/accounts/{aid:int}/policy")
def get_account_policy(aid: int):
    r = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    if r is None:
        raise HTTPException(404, "unknown account")
    srv = db.q1("SELECT * FROM servers WHERE id=?", (r["server_id"],))
    return {**_policy_get(r), "effective": policy.effective(r, srv).summary()}


@router.put("/accounts/{aid:int}/policy")
def put_account_policy(aid: int, body: dict[str, Any] | None = Body(None)):
    """Policy of one account (folder_map, exclude_folders, date_from, date_to, max_size_mb), on top of the one of its
    server. `null` or `{}` clears it. Changing the folder mapping affects the items not yet imported only, see
    docs/planning.md."""
    r = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    if r is None:
        raise HTTPException(404, "unknown account")
    try:
        doc = policy.normalize(body, server=False)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.ex("UPDATE accounts SET policy=? WHERE id=?", (db.jdump(doc) if doc else None, aid))
    return get_account_policy(aid)


# ======================================================================= batches
@router.get("/batches")
def list_batches(limit: int = 200):
    return planning.list_batches(limit)


def _batch_or_404(bid: int):
    b = db.q1("SELECT * FROM batches WHERE id=?", (bid,))
    if b is None:
        raise HTTPException(404, "unknown batch")
    return b


@router.get("/batches/{bid}")
def get_batch(bid: int):
    return planning.batch_view(_batch_or_404(bid), with_jobs=True)


@router.post("/batches/{bid}/{action}")
def batch_action(bid: int, action: str):
    """action: pause | resume | stop | retry-failed. Jobs that cannot take it are left alone (`skipped`)."""
    _batch_or_404(bid)
    if action not in ("pause", "resume", "stop", "retry-failed"):
        raise HTTPException(404, "unknown action")
    return planning.batch_action(bid, action)


# ======================================================================= queue order
class ReorderIn(BaseModel):
    job_ids: list[int]


@router.post("/jobs/reorder")
def reorder_jobs(body: ReorderIn):
    """The queued jobs listed run first, in that order. Returns the whole queue in its new order."""
    order = planning.reorder(body.job_ids)
    return {"queue": order}


class PriorityIn(BaseModel):
    priority: int = Field(ge=-1000000, le=1000000)


@router.put("/jobs/{jid}/priority")
def set_priority(jid: int, body: PriorityIn):
    r = db.q1("SELECT id FROM jobs WHERE id=?", (jid,))
    if r is None:
        raise HTTPException(404, "unknown job")
    planning.set_priority(jid, body.priority)
    return repo.get_job(jid)


@router.post("/jobs/{jid}/delta")
def delta_job(jid: int):
    """Final resync of a finished job: runs it again in delta mode (what arrived since the previous pass)."""
    r = db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
    if r is None:
        raise HTTPException(404, "unknown job")
    if r["status"] not in repo.JOB_FINAL:
        raise HTTPException(409, f"job still active ('{r['status']}')")
    if db.q1(f"SELECT id FROM jobs WHERE account_id=? AND id<>? AND status IN {ACTIVE}", (r["account_id"], jid)):
        raise HTTPException(409, "another job is active for this account")
    db.ex("UPDATE jobs SET status='queued', mode='delta', error=NULL, finished_at=NULL, phase='precheck', "
          "pause_reason=NULL WHERE id=?", (jid,))
    crash.reset_crashes(jid)
    repo.log(jid, "Final resync (delta): what arrived since the previous pass")
    return repo.get_job(jid)


# ======================================================================= comparison
class CompareIn(BaseModel):
    detail: bool = False


def _job_or_404(jid: int):
    r = db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
    if r is None:
        raise HTTPException(404, "unknown job")
    return r


def _queue_compare(jid: int, detail: bool) -> dict:
    live = db.q1("SELECT * FROM compares WHERE job_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                 (jid,))
    if live is not None:
        return planning.compare_view(live)
    now = time.time()
    cur = db.ex("INSERT INTO compares(job_id, status, detail, created_at, updated_at) VALUES (?, 'queued', ?, ?, ?)",
                (jid, int(detail), now, now))
    return planning.compare_view(db.q1("SELECT * FROM compares WHERE id=?", (cur.lastrowid,)))


@router.post("/jobs/{jid}/compare", status_code=202)
def start_compare(jid: int, body: CompareIn | None = None):
    """Queues a comparison source versus target, run by the worker. `detail` also looks up, mail by mail, the
    ones recorded as sent to a folder that holds fewer (500 per folder at most)."""
    _job_or_404(jid)
    return _queue_compare(jid, bool(body and body.detail))


@router.get("/jobs/{jid}/compare")
def get_compare(jid: int, run: int = 0, detail: int = 0):
    """The latest comparison of the job: {id, status: queued|running|done|failed, result: {totals, folders:
    [{kind, target, folders, expected, on_target, missing, ...}], ok}}. `run=1` queues a new one first.
    `{status: "none"}` when none was asked."""
    _job_or_404(jid)
    if run:
        return _queue_compare(jid, bool(detail))
    r = db.q1("SELECT * FROM compares WHERE job_id=? ORDER BY id DESC LIMIT 1", (jid,))
    return planning.compare_view(r) if r is not None else {"status": "none", "job_id": jid, "result": None}


@router.get("/jobs/{jid}/compare/csv")
def compare_csv(jid: int):
    _job_or_404(jid)
    r = db.q1("SELECT * FROM compares WHERE job_id=? AND status='done' ORDER BY id DESC LIMIT 1", (jid,))
    if r is None:
        raise HTTPException(404, "no finished comparison for this job: POST /api/jobs/{id}/compare first")
    body = planning.compare_csv(db.jload(r["result"], {}))
    return Response(body.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="compare-job-{jid}.csv"'})


# ======================================================================= items left out
@router.get("/jobs/{jid}/filtered")
def job_filtered(jid: int, limit: int = 1000):
    """Items left out by a filter (not errors): kind, folder, reason (`date`, `size`)."""
    from ..connectors.wiring import items_key_of_account
    j = _job_or_404(jid)
    a = db.q1("SELECT * FROM accounts WHERE id=?", (j["account_id"],))
    key = items_key_of_account(a) if a is not None else None
    if key is None:
        return []
    rows = db.q("SELECT id, kind, folder, item_key, subject, size, error, updated_at FROM items WHERE account_id=? "
                "AND pst_key=? AND status=? ORDER BY id LIMIT ?",
                (j["account_id"], key, policy.FILTERED, max(1, min(limit, 10000))))
    return [{**dict(r), "reason": (r["error"] or "").removeprefix(policy.FILTER_PREFIX)} for r in rows]


@router.get("/jobs/{jid}/oversized")
def job_oversized(jid: int, limit: int = 1000):
    """Messages the target refused (or would refuse) for their size, largest first."""
    from ..connectors.wiring import items_key_of_account
    j = _job_or_404(jid)
    a = db.q1("SELECT * FROM accounts WHERE id=?", (j["account_id"],))
    key = items_key_of_account(a) if a is not None else None
    if key is None:
        return []
    rows = db.q("SELECT id, kind, folder, item_key, subject, size, error, updated_at FROM items WHERE account_id=? "
                "AND pst_key=? AND status='error' AND error LIKE ? ORDER BY size DESC LIMIT ?",
                (j["account_id"], key, policy.OVERSIZE_PREFIX + "%", max(1, min(limit, 10000))))
    return [dict(r) for r in rows]


# ======================================================================= CSV export
@router.get("/accounts/export-csv")
def export_accounts_csv(server_id: int | None = None):
    """The accounts as a CSV that the import reads back: email, login, server, pst, content toggles, source
    settings. Never a password or any secret."""
    body = csvaccounts.export(server_id)
    return Response(body.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="accounts-export.csv"'})
