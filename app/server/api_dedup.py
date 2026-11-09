"""API of the duplicates tab: the mailboxes MCCB knows, analysis and removal runs. See docs/duplicates.md and
docs/api.md."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db, dedup, dedup_runs

router = APIRouter(prefix="/api/dedup")
ACTIVE = "('queued','running','stopping')"


def _box_or_404(key: str) -> dict:
    b = dedup_runs.resolve(key)
    if b is None:
        raise HTTPException(404, "unknown mailbox")
    return b


def _busy(key: str) -> bool:
    return db.q1(f"SELECT id FROM dedup_runs WHERE box_key=? AND status IN {ACTIVE}", (key,)) is not None


@router.get("/boxes")
def list_boxes():
    return dedup_runs.boxes_with_runs()


class RunIn(BaseModel):
    box_keys: list[str] = Field(min_length=1, max_length=2000)
    mode: str = "scan"
    kinds: list[str] | None = None
    confirm: bool = False


@router.post("/runs", status_code=202)
def create_runs(body: RunIn):
    """Queues one run per mailbox. scan writes nothing; dedup deletes the copies permanently and needs confirm."""
    if body.mode not in dedup_runs.MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(dedup_runs.MODES)}")
    if body.mode == "dedup" and not body.confirm:
        raise HTTPException(422, "removing duplicates deletes them permanently: send confirm=true")
    kinds = list(dict.fromkeys(body.kinds or dedup.KINDS))
    if not kinds or any(k not in dedup.KINDS for k in kinds):
        raise HTTPException(422, f"kinds must be among {', '.join(dedup.KINDS)}")
    keys = list(dict.fromkeys(body.box_keys))
    boxes = {k: dedup_runs.resolve(k) for k in keys}
    unknown = [k for k, b in boxes.items() if b is None]
    if unknown:
        raise HTTPException(422, {"msg": "unknown mailbox(es)", "box_keys": unknown})
    no_pw = [k for k, b in boxes.items() if not b["row"]["password_enc"]]
    if no_pw:
        raise HTTPException(422, {"msg": "no password known for these mailboxes", "box_keys": no_pw})
    busy = [k for k, b in boxes.items() if _busy(k) or (
        body.mode == "dedup" and dedup_runs.busy_reason(b["server"]["id"], b["row"]["email"]))]
    free = [boxes[k] for k in keys if k not in busy]
    return {"runs": dedup_runs.start(free, body.mode, kinds), "skipped_busy": busy}


@router.get("/runs/{rid}")
def get_run(rid: int):
    r = db.q1("SELECT * FROM dedup_runs WHERE id=?", (rid,))
    if r is None:
        raise HTTPException(404, "unknown run")
    return dedup_runs.run_view(r, full=True)


@router.get("/boxes/{key}/runs")
def box_runs(key: str, limit: int = 20):
    _box_or_404(key)
    rows = db.q("SELECT * FROM dedup_runs WHERE box_key=? ORDER BY id DESC LIMIT ?", (key, max(1, min(limit, 100))))
    return [dedup_runs.run_view(r) for r in rows]


@router.post("/runs/{rid}/stop")
def stop_run(rid: int):
    r = db.q1("SELECT * FROM dedup_runs WHERE id=?", (rid,))
    if r is None:
        raise HTTPException(404, "unknown run")
    if r["status"] == "queued":
        db.ex("UPDATE dedup_runs SET status='stopped', stop_requested=1, finished_at=? WHERE id=? AND status='queued'",
              (time.time(), rid))
    elif r["status"] == "running":
        db.ex("UPDATE dedup_runs SET status='stopping', stop_requested=1 WHERE id=? AND status='running'", (rid,))
    return dedup_runs.run_view(db.q1("SELECT * FROM dedup_runs WHERE id=?", (rid,)))
