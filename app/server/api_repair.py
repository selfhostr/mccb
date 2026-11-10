"""API of the repair tab: mailboxes added by hand (the duplicates tab lists them too, see dedup_runs.py) to check
and repair, their runs and a before/after preview. See docs/repair.md and docs/api.md."""
from __future__ import annotations

import time

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import csvimport, db, repair_runs
from .imap import ImapError
from .security import encrypt

router = APIRouter(prefix="/api/repair")
ACTIVE = "('queued','running','stopping')"


def _server_or_404(sid: int):
    r = db.q1("SELECT * FROM servers WHERE id=?", (sid,))
    if r is None:
        raise HTTPException(404, "unknown server")
    return r


def _box_or_404(bid: int):
    r = db.q1("SELECT * FROM repair_boxes WHERE id=?", (bid,))
    if r is None:
        raise HTTPException(404, "unknown mailbox")
    return r


def _busy(bid: int) -> bool:
    return db.q1(f"SELECT id FROM repair_runs WHERE box_id=? AND status IN {ACTIVE}", (bid,)) is not None


def _in_use(bid: int) -> bool:
    """A repair run, or a duplicates run of the duplicates tab (same list of mailboxes), works on it now."""
    return _busy(bid) or db.q1(f"SELECT id FROM dedup_runs WHERE box_key=? AND status IN {ACTIVE}",
                               (f"repair:{bid}",)) is not None


def _view(bid: int) -> dict:
    return next(b for b in repair_runs.boxes_with_runs() if b["id"] == bid)


# ======================================================================= mailboxes
class BoxIn(BaseModel):
    server_id: int
    email: str = Field(min_length=3, max_length=320)
    password: str | None = None
    login: str | None = None


class BoxUpdate(BaseModel):
    server_id: int | None = None
    email: str | None = None
    password: str | None = None
    login: str | None = None


@router.get("/boxes")
def list_boxes():
    return repair_runs.boxes_with_runs()


@router.post("/boxes", status_code=201)
def create_box(b: BoxIn):
    _server_or_404(b.server_id)
    email = b.email.strip()
    if "@" not in email:
        raise HTTPException(422, "invalid email address")
    if not b.password:
        raise HTTPException(422, "password required")
    if db.q1("SELECT id FROM repair_boxes WHERE server_id=? AND email=?", (b.server_id, email)):
        raise HTTPException(409, "this mailbox is already in the list for this server")
    cur = db.ex("INSERT INTO repair_boxes(server_id, email, login, password_enc, created_at) VALUES (?,?,?,?,?)",
                (b.server_id, email, (b.login or "").strip(), encrypt(b.password), time.time()))
    return _view(cur.lastrowid)


@router.put("/boxes/{bid}")
def update_box(bid: int, b: BoxUpdate):
    cur = _box_or_404(bid)
    if _in_use(bid):
        raise HTTPException(409, "a run is active for this mailbox")
    sets: dict[str, object] = {}
    if b.server_id is not None and b.server_id != cur["server_id"]:
        _server_or_404(b.server_id)
        sets["server_id"] = b.server_id
    if b.email is not None and b.email.strip() != cur["email"]:
        if "@" not in b.email:
            raise HTTPException(422, "invalid email address")
        sets["email"] = b.email.strip()
    if b.login is not None:
        sets["login"] = b.login.strip()
    if b.password:
        sets["password_enc"] = encrypt(b.password)
    if sets.keys() & {"server_id", "email", "login", "password_enc"}:
        sets.update(status="new", last_test=None, last_test_result=None)   # the last test was another login
    if sets:
        try:
            db.ex(f"UPDATE repair_boxes SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), bid))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "this mailbox is already in the list for this server")
            raise
    return _view(bid)


@router.delete("/boxes/{bid}")
def delete_box(bid: int):
    _box_or_404(bid)
    if _in_use(bid):
        raise HTTPException(409, "a run is active for this mailbox")
    db.ex("DELETE FROM repair_boxes WHERE id=?", (bid,))
    return {"ok": True}


@router.post("/boxes/{bid}/test")
def test_box(bid: int):
    _box_or_404(bid)
    return repair_runs.test_box(bid)


@router.post("/boxes/import-csv")
async def import_csv(file: UploadFile = File(...), server_id: int = Form(...)):
    """email;password[;login], like the migration accounts. A known mailbox gets the new password."""
    _server_or_404(server_id)
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "CSV file too large (5 MB max)")
    rows, errors, _extended = csvimport.parse_ext(data)
    created = updated = 0
    ids: list[int] = []
    for row in rows:
        ex = db.q1("SELECT * FROM repair_boxes WHERE server_id=? AND email=?", (server_id, row["email"]))
        if not row["password"]:
            if ex is None:
                errors.append({"line": row.get("line"), "msg": f"missing password for {row['email']}"})
            continue
        if ex is not None:
            if _in_use(ex["id"]):
                errors.append({"line": row.get("line"), "msg": f"a run is active for {row['email']}: not changed"})
                continue
            db.ex("UPDATE repair_boxes SET password_enc=?, login=?, status='new', last_test=NULL, "
                  "last_test_result=NULL WHERE id=?", (encrypt(row["password"]), row["login"] or "", ex["id"]))
            updated += 1
            ids.append(ex["id"])
        else:
            cur = db.ex("INSERT INTO repair_boxes(server_id, email, login, password_enc, created_at) "
                        "VALUES (?,?,?,?,?)", (server_id, row["email"], row["login"] or "", encrypt(row["password"]),
                                              time.time()))
            created += 1
            ids.append(cur.lastrowid)
    return {"created": created, "updated": updated, "errors": errors, "ids": ids}


# ======================================================================= runs
class RunIn(BaseModel):
    box_ids: list[int] = Field(min_length=1, max_length=2000)
    mode: str = "scan"
    folder: str | None = Field(default=None, max_length=500)


@router.post("/runs", status_code=202)
def create_runs(body: RunIn):
    """Queues one run per mailbox. scan writes nothing; repair and restore change the mailbox (see docs)."""
    if body.mode not in repair_runs.MODES:
        raise HTTPException(422, f"mode must be one of {', '.join(repair_runs.MODES)}")
    ids = list(dict.fromkeys(body.box_ids))
    known = {r["id"] for r in db.q(f"SELECT id FROM repair_boxes WHERE id IN ({','.join('?' * len(ids))})", ids)}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(422, {"msg": "unknown mailbox(es)", "box_ids": unknown})
    busy = [i for i in ids if _busy(i)]
    free = [i for i in ids if i not in busy]
    return {"runs": repair_runs.start(free, body.mode, body.folder), "skipped_busy": busy}


@router.get("/runs/{rid}")
def get_run(rid: int):
    r = db.q1("SELECT * FROM repair_runs WHERE id=?", (rid,))
    if r is None:
        raise HTTPException(404, "unknown run")
    return repair_runs.run_view(r, full=True)


@router.get("/boxes/{bid}/runs")
def box_runs(bid: int, limit: int = 20):
    _box_or_404(bid)
    rows = db.q("SELECT * FROM repair_runs WHERE box_id=? ORDER BY id DESC LIMIT ?", (bid, max(1, min(limit, 100))))
    return [repair_runs.run_view(r) for r in rows]


@router.post("/runs/{rid}/stop")
def stop_run(rid: int):
    r = db.q1("SELECT * FROM repair_runs WHERE id=?", (rid,))
    if r is None:
        raise HTTPException(404, "unknown run")
    if r["status"] == "queued":
        db.ex("UPDATE repair_runs SET status='stopped', stop_requested=1, finished_at=? WHERE id=? AND status='queued'",
              (time.time(), rid))
    elif r["status"] == "running":
        db.ex("UPDATE repair_runs SET status='stopping', stop_requested=1 WHERE id=? AND status='running'", (rid,))
    return repair_runs.run_view(db.q1("SELECT * FROM repair_runs WHERE id=?", (rid,)))


class PreviewIn(BaseModel):
    folder: str
    uid: str = Field(pattern=r"^\d+$")


@router.post("/boxes/{bid}/preview")
def preview(bid: int, body: PreviewIn):
    _box_or_404(bid)
    try:
        return repair_runs.preview(bid, body.folder, body.uid)
    except ImapError as e:
        raise HTTPException(502 if e.temporary else 422, str(e))
