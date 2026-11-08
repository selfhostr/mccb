"""API FastAPI (Contrat API de docs/specification.md) + SPA statique. Run: python -m app.server.web"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..connectors.wiring import items_key_of_account, sync_files_source
from . import crash, csvaccounts, csvimport, db, planning, preview, repo, runs, tokens, uploads
from .api_connectors import router as connectors_router
from .api_planning import router as planning_router
from .api_repair import router as repair_router
from .api_sources import router as sources_router
from .api_tokens import router as tokens_router
from .config import settings
from .dav import DavClient
from .imap import ImapConn, ImapError, ImapParams
from .version import get_version
from .security import (admin_created, check_password, check_session, decrypt, encrypt, ensure_admin_password,
                       make_session, password_from_env, set_admin_password)

log = logging.getLogger("web")
COOKIE = "mccb_session"
PUBLIC_API = {"/api/login", "/api/health", "/api/logout", "/api/version"}



def _admin_startup() -> None:
    if password_from_env():
        log.info("Admin password taken from the ADMIN_PASSWORD environment variable")
        return
    pw = ensure_admin_password()
    if pw:
        lines = ["", "=" * 64, "  FIRST START: an admin password was generated for you",
                 "", f"  Admin password: {pw}", "",
                 "  Shown ONCE only. Change it in the web UI under Settings",
                 "  or set ADMIN_PASSWORD in the environment.", "=" * 64, ""]
        log.warning("\n".join(lines))
    else:
        log.warning("Admin password already set (generated on %s). To reset it: delete /data/admin.json "
                    "and restart, or set ADMIN_PASSWORD.", admin_created())


@asynccontextmanager
async def lifespan(_app):
    log.info("mccb v%s starting (web)", get_version())
    settings.ensure_dirs()
    db.conn()
    _admin_startup()
    task = asyncio.get_running_loop().create_task(_prune_events())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="PST → IMAP/CalDAV/CardDAV", docs_url=None, openapi_url=None,
              redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    path = request.url.path
    request.state.via = "session"
    if path.startswith("/api/") and path not in PUBLIC_API:
        if not check_session(request.cookies.get(COOKIE)):
            # scripts and the `mccb` command: Authorization: Bearer <API token> (see tokens.py)
            header = request.headers.get("authorization")
            if not (header and await asyncio.to_thread(tokens.verify_header, header)):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
            request.state.via = "token"
    return await call_next(request)


# ======================================================================= auth
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 300.0
_LOGIN_FAILS: dict[str, list[float]] = {}


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
    _LOGIN_FAILS[ip] = fails
    if len(fails) >= LOGIN_MAX_FAILS:
        raise HTTPException(429, "too many attempts, try again later")
    if not check_password(body.password):
        fails.append(now)
        time.sleep(0.5)
        raise HTTPException(401, "incorrect password")
    resp = JSONResponse({"ok": True, "authenticated": True})
    resp.set_cookie(COOKIE, make_session(), max_age=settings.session_max_age, httponly=True, samesite="lax",
                    secure=settings.cookie_secure or request.url.scheme == "https", path="/")
    return resp


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=8, max_length=256)


@app.post("/api/password")
def change_password(body: PasswordIn, request: Request):
    """Change the admin password (only when it is not forced by ADMIN_PASSWORD). Sessions are invalidated."""
    if password_from_env():
        raise HTTPException(409, "the password is set by ADMIN_PASSWORD; change the environment variable instead")
    if not check_password(body.current):
        time.sleep(0.5)
        raise HTTPException(403, "current password is incorrect")
    set_admin_password(body.new)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, make_session(), max_age=settings.session_max_age, httponly=True, samesite="lax",
                    secure=settings.cookie_secure or request.url.scheme == "https", path="/")
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/me")
def me():
    return {"authenticated": True, "user": "admin"}


@app.get("/api/health")
def health():
    hb = db.kv_get("worker_heartbeat")
    age = (time.time() - float(hb)) if hb else None
    return {"ok": True, "worker": {"alive": age is not None and age < 60, "last_seen": float(hb) if hb else None},
            "time": time.time(), "version": get_version()}


@app.get("/api/version")
def version():
    return {"version": get_version()}


# ======================================================================= servers
class ServerIn(BaseModel):
    name: str
    imap_host: str
    imap_port: int = 993
    imap_security: str = Field("ssl", pattern="^(ssl|starttls|none)$")
    verify_tls: bool = True
    root_folder: str | None = None
    caldav_url: str | None = None
    carddav_url: str | None = None
    dav_enabled: bool = True
    workers: int = Field(4, ge=1, le=32)  # IMAP APPEND connections (manual tuning)
    notes: str = ""
    # "auto": builders / IMAP connections chosen and adjusted during the job; "manual": `workers` connections,
    # `builders` builder processes (None = machine default). Omitted on creation: "manual" when `workers` is
    # not the default (API clients of earlier versions chose it), else "auto". Omitted on update: unchanged.
    tuning: str | None = Field(None, pattern="^(auto|manual)$")
    builders: int | None = Field(None, ge=0, le=32)


def _server_or_404(sid: int):
    r = db.q1("SELECT * FROM servers WHERE id=?", (sid,))
    if r is None:
        raise HTTPException(404, "unknown server")
    return r


def _srv_values(s: ServerIn, current=None) -> tuple:
    given = s.model_fields_set
    if s.tuning is not None:
        tuning = s.tuning
    elif current is not None:
        tuning = current["tuning"] or "auto"
    else:
        tuning = "manual" if "workers" in given and s.workers not in db.DEFAULT_WORKERS_LEGACY else "auto"
    builders = s.builders if ("builders" in given or current is None) else current["builders"]
    return (s.name.strip(), s.imap_host.strip(), s.imap_port, s.imap_security, int(s.verify_tls),
            (s.root_folder or "").strip() or None, (s.caldav_url or "").strip() or None,
            (s.carddav_url or "").strip() or None, int(s.dav_enabled), s.workers, s.notes or "", tuning, builders)


@app.get("/api/servers")
def list_servers():
    return [repo.server_view(r) for r in db.q("SELECT * FROM servers ORDER BY name, id")]


@app.post("/api/servers", status_code=201)
def create_server(s: ServerIn):
    cur = db.ex("INSERT INTO servers(name,imap_host,imap_port,imap_security,verify_tls,root_folder,caldav_url,"
                "carddav_url,dav_enabled,workers,notes,tuning,builders,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*_srv_values(s), time.time()))
    return repo.get_server(cur.lastrowid)


@app.get("/api/servers/{sid}")
def get_server(sid: int):
    return repo.server_view(_server_or_404(sid))


@app.put("/api/servers/{sid}")
def update_server(sid: int, s: ServerIn):
    cur = _server_or_404(sid)
    db.ex("UPDATE servers SET name=?,imap_host=?,imap_port=?,imap_security=?,verify_tls=?,root_folder=?,caldav_url=?,"
          "carddav_url=?,dav_enabled=?,workers=?,notes=?,tuning=?,builders=? WHERE id=?", (*_srv_values(s, cur), sid))
    new = db.q1("SELECT * FROM servers WHERE id=?", (sid,))
    if any(cur[k] != new[k] for k in _SRV_CONNECTION):  # the last test was about another connection
        db.ex("UPDATE servers SET last_test=NULL, last_test_result=NULL WHERE id=?", (sid,))
    return repo.get_server(sid)


# Settings a connection test depends on: changing one clears the stored result.
_SRV_CONNECTION = ("imap_host", "imap_port", "imap_security", "verify_tls", "caldav_url", "carddav_url", "dav_enabled")


@app.delete("/api/servers/{sid}")
def delete_server(sid: int):
    _server_or_404(sid)
    busy = db.q1("SELECT j.id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE a.server_id=? AND "
                 "j.status IN ('queued','running','paused','stopping')", (sid,))
    if busy:
        raise HTTPException(409, "jobs are running on this server")
    db.ex("DELETE FROM servers WHERE id=?", (sid,))
    return {"ok": True}


@app.post("/api/servers/{sid}/test")
def test_server(sid: int):
    r = _server_or_404(sid)
    import imaplib
    import ssl as _ssl
    res: dict[str, Any] = {}
    try:
        ctx = _ssl.create_default_context()
        if not r["verify_tls"]:
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        if r["imap_security"] == "ssl":
            M = imaplib.IMAP4_SSL(r["imap_host"], r["imap_port"], ssl_context=ctx, timeout=20)
        else:
            M = imaplib.IMAP4(r["imap_host"], r["imap_port"], timeout=20)
            if r["imap_security"] == "starttls":
                M.starttls(ssl_context=ctx)
        caps = sorted(M.capabilities)
        try:
            M.logout()
        except Exception:
            pass
        res["imap"] = {"ok": True, "msg": f"IMAP server reachable ({r['imap_host']}:{r['imap_port']})", "capabilities": caps}
    except Exception as e:
        res["imap"] = {"ok": False, "msg": f"connection failed: {e}"}
    for kind, col in (("caldav", "caldav_url"), ("carddav", "carddav_url")):
        res[kind] = _probe_dav(r, kind, r[col])
    db.ex("UPDATE servers SET last_test=?, last_test_result=? WHERE id=?", (time.time(), db.jdump(res), sid))
    return res


def _probe_dav(r, kind: str, template: str | None) -> dict:
    """Test without credentials: does the DAV entry point respond (401/207/200...)?
    With DAV switched off nothing fails, but the entry point is still looked up so that the verdict can say
    `found` (DAV answers here yet is off): a green "DAV disabled" alone hid a working CalDAV/CardDAV."""
    import httpx
    enabled = bool(r["dav_enabled"])
    if enabled and template and "{" in template:
        return {"ok": True, "msg": "template URL: checked when each mailbox is tested"}
    urls = [template] if template and "{" not in template else [f"https://{r['imap_host']}/.well-known/{kind}"]
    err = "no URL"
    for url in urls:
        try:
            with httpx.Client(verify=bool(r["verify_tls"]), timeout=15 if enabled else 8, follow_redirects=True) as c:
                resp = c.request("PROPFIND", url, headers={"Depth": "0"})
            reachable = resp.status_code < 500 and resp.status_code != 404
            if not enabled:
                if reachable:
                    return {"ok": True, "found": True, "msg": f"DAV is off, but {url} answers (HTTP {resp.status_code})"}
                err = ""
                break
            if reachable:
                return {"ok": True, "msg": f"{url} reachable (HTTP {resp.status_code})"}
            return {"ok": False, "msg": f"{url}: HTTP {resp.status_code}"}
        except Exception as e:
            if enabled:
                return {"ok": False, "msg": f"{url} unreachable: {e}"}
            err = ""
    if not enabled:
        return {"ok": True, "found": False, "msg": "DAV disabled for this server"}
    return {"ok": False, "msg": err}


# ======================================================================= accounts
class AccountIn(BaseModel):
    server_id: int
    email: str
    password: str | None = None
    login: str | None = None
    options: dict[str, bool] | None = None
    pst_id: int | None = None


class AccountUpdate(BaseModel):
    server_id: int | None = None
    email: str | None = None
    password: str | None = None
    login: str | None = None
    options: dict[str, bool] | None = None
    pst_id: int | None = None


def _account_or_404(aid: int):
    r = db.q1("SELECT * FROM accounts WHERE id=?", (aid,))
    if r is None:
        raise HTTPException(404, "unknown mailbox")
    return r


@app.get("/api/accounts")
def list_accounts(server_id: int | None = None):
    if server_id:
        rows = db.q("SELECT * FROM accounts WHERE server_id=? ORDER BY email", (server_id,))
    else:
        rows = db.q("SELECT * FROM accounts ORDER BY email")
    return [repo.account_view(r) for r in rows]


@app.post("/api/accounts", status_code=201)
def create_account(a: AccountIn):
    _server_or_404(a.server_id)
    email = a.email.strip()
    if not csvimport.EMAIL_RE.match(email):
        raise HTTPException(422, "invalid email address")
    if db.q1("SELECT id FROM accounts WHERE server_id=? AND email=?", (a.server_id, email)):
        raise HTTPException(409, "this mailbox already exists on this server")
    o = a.options or {}
    cur = db.ex("INSERT INTO accounts(server_id,email,login,password_enc,opt_mail,opt_calendar,opt_contacts,pst_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (a.server_id, email, (a.login or "").strip(), encrypt(a.password) if a.password else None,
                 int(o.get("mail", True)), int(o.get("calendar", True)), int(o.get("contacts", True)),
                 a.pst_id if a.pst_id and repo.get_pst(a.pst_id) else None, time.time()))
    sync_files_source(cur.lastrowid)
    return repo.get_account(cur.lastrowid)


@app.get("/api/accounts/{aid:int}")
def get_account(aid: int):
    return repo.account_view(_account_or_404(aid))


@app.put("/api/accounts/{aid:int}")
def update_account(aid: int, a: AccountUpdate):
    r = _account_or_404(aid)
    sets, vals = [], []
    if a.server_id is not None:
        _server_or_404(a.server_id)
        sets.append("server_id=?"); vals.append(a.server_id)
    if a.email is not None:
        if not csvimport.EMAIL_RE.match(a.email.strip()):
            raise HTTPException(422, "invalid email address")
        sets.append("email=?"); vals.append(a.email.strip())
    if a.login is not None:
        sets.append("login=?"); vals.append(a.login.strip())
    if a.password:
        sets.append("password_enc=?"); vals.append(encrypt(a.password))
        sets.append("status='new'")
    if a.options is not None:
        for k in ("mail", "calendar", "contacts"):
            if k in a.options:
                sets.append(f"opt_{k}=?"); vals.append(int(bool(a.options[k])))
    if "pst_id" in a.model_fields_set:
        if a.pst_id is not None and not repo.get_pst(a.pst_id):
            raise HTTPException(404, "unknown PST")
        sets.append("pst_id=?"); vals.append(a.pst_id)
    target_changed = ((a.server_id is not None and a.server_id != r["server_id"])
                      or (a.email is not None and a.email.strip() != r["email"])
                      or (a.login is not None and a.login.strip() != (r["login"] or "")))
    if target_changed and "status='new'" not in sets:
        sets.append("status='new'")
    if sets:
        try:
            db.ex(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", (*vals, aid))
            if target_changed:  # new target: everything must be re-sent
                db.ex("DELETE FROM items WHERE account_id=?", (aid,))
                planning.forget_cursors(aid)
            if "pst_id" in a.model_fields_set:
                sync_files_source(aid)
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "this mailbox already exists on this server")
            raise
    return repo.get_account(r["id"])


@app.delete("/api/accounts/{aid:int}")
def delete_account(aid: int):
    _account_or_404(aid)
    if db.q1("SELECT id FROM jobs WHERE account_id=? AND status IN ('queued','running','paused','stopping')", (aid,)):
        raise HTTPException(409, "a job is running for this mailbox")
    db.ex("DELETE FROM accounts WHERE id=?", (aid,))
    return {"ok": True}


@app.put("/api/accounts/{aid:int}/pst")
def set_account_pst(aid: int, body: dict = Body(...)):
    _account_or_404(aid)
    pid = body.get("pst_id")
    if pid is not None and not repo.get_pst(int(pid)):
        raise HTTPException(404, "unknown PST")
    db.ex("UPDATE accounts SET pst_id=? WHERE id=?", (pid, aid))
    sync_files_source(aid)
    return repo.get_account(aid)


def test_account_row(r) -> dict:
    srv = db.q1("SELECT * FROM servers WHERE id=?", (r["server_id"],))
    login_name = r["login"] or r["email"]
    try:
        pw = decrypt(r["password_enc"])
    except ValueError as e:
        pw, perr = "", str(e)
    else:
        perr = None
    res: dict[str, Any] = {}
    status = "ok"
    if not pw:
        res["imap"] = {"ok": False, "msg": perr or "password missing", "quota": None}
        status = "error"
    else:
        params = ImapParams(srv["imap_host"], srv["imap_port"], srv["imap_security"], bool(srv["verify_tls"]),
                            login_name, pw, timeout=30)
        try:
            with ImapConn(params) as c:
                quota = c.quota()
            res["imap"] = {"ok": True, "msg": "IMAP connection successful", "quota": quota}
        except ImapError as e:
            res["imap"] = {"ok": False, "msg": str(e), "quota": None}
            status = "auth_failed" if e.kind == "auth" else "error"
    for kind, tmpl in (("caldav", "caldav_url"), ("carddav", "carddav_url")):
        if not srv["dav_enabled"]:
            res[kind] = {"ok": True, "msg": "DAV disabled for this server"}
            continue
        if not pw:
            res[kind] = {"ok": False, "msg": "password missing"}
            continue
        try:
            with DavClient(login_name, pw, bool(srv["verify_tls"]), timeout=30) as d:
                ok, msg = d.test(kind, srv[tmpl] or None, r["email"], srv["imap_host"])
        except Exception as e:
            ok, msg = False, str(e)
        res[kind] = {"ok": ok, "msg": msg}
        want = r["opt_calendar"] if kind == "caldav" else r["opt_contacts"]
        if not ok and want and status == "ok":
            status = "error"
    db.ex("UPDATE accounts SET status=?, last_test=?, last_test_result=? WHERE id=?",
          (status, time.time(), db.jdump(res), r["id"]))
    return res


@app.post("/api/accounts/{aid:int}/test")
def test_account(aid: int):
    return test_account_row(_account_or_404(aid))


@app.post("/api/accounts/import-csv")
async def import_csv(file: UploadFile = File(...), server_id: int = Form(...)):
    _server_or_404(server_id)
    data = await file.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "CSV file too large (5 MB max)")
    rows, errors, _extended = csvimport.parse_ext(data)
    return csvaccounts.apply(server_id, rows, errors)


@app.get("/api/accounts/csv-template")
def csv_template():
    return Response(csvimport.TEMPLATE.encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="mailboxes.csv"'})


# ======================================================================= uploads / PST
class UploadIn(BaseModel):
    filename: str
    size: int
    last_modified: int | None = None


@app.post("/api/uploads")
def create_upload(u: UploadIn):
    return uploads.create_or_resume(u.filename, u.size, u.last_modified)


@app.get("/api/uploads/{pid}")
def get_upload(pid: int):
    v = repo.get_upload(pid)
    if v is None:
        raise HTTPException(404, "unknown upload")
    return v


@app.put("/api/uploads/{pid}/chunks/{n}")
async def put_chunk(pid: int, n: int, request: Request):
    return await uploads.put_chunk(pid, n, request)


@app.post("/api/uploads/{pid}/complete")
def complete_upload(pid: int):
    up = uploads.complete(pid)
    return {**up, "pst_id": pid, "pst": repo.get_pst(pid)}


@app.get("/api/psts")
def list_psts():
    return [repo.pst_view(r) for r in db.q("SELECT * FROM psts ORDER BY id DESC")]


@app.get("/api/psts/{pid}")
def get_pst(pid: int):
    v = repo.get_pst(pid)
    if v is None:
        raise HTTPException(404, "unknown PST")
    return v


@app.get("/api/psts/{pid}/preview")
async def preview_pst(pid: int, limit: int = 20):
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None:
        raise HTTPException(404, "unknown PST")
    if row["status"] != "valid":
        raise HTTPException(409, "PST not valid")
    if (db.jload(row["scan"], None) or {}).get("format") == "files":
        raise HTTPException(409, "no preview for this kind of file: it is not a PST")
    limit = max(1, min(200, limit))
    try:
        zpath, tmp = await preview.build_zip(row["path"], limit)
    except preview.PreviewCrash:
        return JSONResponse({"detail": "libpff crash during preview"}, status_code=500)
    except preview.PreviewError as e:
        return JSONResponse({"detail": str(e)}, status_code=500)
    base = Path(row["filename"]).stem
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in base) or "pst"
    return FileResponse(zpath, media_type="application/zip", filename="preview-%s.zip" % safe,
                        background=BackgroundTask(shutil.rmtree, tmp, True))


@app.delete("/api/psts/{pid}")
def delete_pst(pid: int):
    uploads.delete_pst(pid)
    return {"ok": True}


from .api_intake import router as intake_router  # noqa: E402
app.include_router(intake_router)


# ======================================================================= jobs
class RunIn(BaseModel):
    account_ids: list[int]
    # optional: a named batch groups the jobs started together (GET /api/batches); mode "delta" is the final resync
    # (what arrived since the previous pass); priority: higher runs first among the queued jobs
    name: str | None = Field(None, max_length=120)
    mode: str = Field("full", pattern="^(full|delta)$")
    priority: int = 0


@app.post("/api/runs")
def create_runs(body: RunIn):
    jobs, errors, _batch = runs.create_jobs(body.account_ids, body.name, body.mode, body.priority)
    if errors and not jobs:
        raise HTTPException(422, {"msg": "no job created", "errors": errors})
    for e in errors:
        log.warning("run refused for account %s : %s", e["account_id"], e["msg"])
    return jobs


def _job_or_404(jid: int):
    r = db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
    if r is None:
        raise HTTPException(404, "unknown job")
    return r


@app.get("/api/jobs")
def list_jobs(active: int = 0, account_id: int | None = None, limit: int = 200):
    sql, params = "SELECT * FROM jobs", []
    where = []
    if active:
        where.append("status IN ('queued','running','paused','stopping')")
    if account_id:
        where.append("account_id=?"); params.append(account_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 5000)))
    return [repo.job_view(r) for r in db.q(sql, params)]


@app.get("/api/jobs/{jid}")
def get_job(jid: int):
    return repo.job_view(_job_or_404(jid))


@app.post("/api/jobs/{jid}/pause")
def pause_job(jid: int):
    r = _job_or_404(jid)
    if r["status"] not in ("queued", "running"):
        raise HTTPException(409, f"cannot pause a job in status '{r['status']}'")
    db.ex("UPDATE jobs SET status='paused', pause_reason=NULL WHERE id=? AND status IN ('queued','running')", (jid,))
    repo.log(jid, "Pause requested")
    return repo.get_job(jid)


@app.post("/api/jobs/{jid}/resume")
def resume_job(jid: int):
    r = _job_or_404(jid)
    if r["status"] != "paused":
        raise HTTPException(409, f"the job is not paused ('{r['status']}')")
    if r["worker_id"]:
        # the worker has not yet taken the pause into account: let it continue
        db.ex("UPDATE jobs SET status='running', pause_reason=NULL WHERE id=? AND status='paused' AND worker_id IS NOT NULL",
              (jid,))
    db.ex("UPDATE jobs SET status='queued', pause_reason=NULL WHERE id=? AND status='paused'", (jid,))
    repo.log(jid, "Resume requested")
    return repo.get_job(jid)


@app.post("/api/jobs/{jid}/stop")
def stop_job(jid: int):
    r = _job_or_404(jid)
    if r["status"] in ("queued", "paused") and not r["worker_id"]:
        db.ex("UPDATE jobs SET status='stopped', finished_at=? WHERE id=?", (time.time(), jid))
    elif r["status"] in ("running", "paused"):
        db.ex("UPDATE jobs SET status='stopping' WHERE id=?", (jid,))
    elif r["status"] != "stopping":
        raise HTTPException(409, f"job already finished ('{r['status']}')")
    repo.log(jid, "Stop requested")
    return repo.get_job(jid)


def _requeue(jid: int, mode: str) -> dict:
    r = _job_or_404(jid)
    if r["status"] not in repo.JOB_FINAL:
        raise HTTPException(409, f"job still active ('{r['status']}')")
    if db.q1("SELECT id FROM jobs WHERE account_id=? AND id<>? AND status IN ('queued','running','paused','stopping')",
             (r["account_id"], jid)):
        raise HTTPException(409, "another job is active for this mailbox")
    db.ex("UPDATE jobs SET status='queued', mode=?, error=NULL, finished_at=NULL, phase='precheck', pause_reason=NULL "
          "WHERE id=?", (mode, jid))
    crash.reset_crashes(jid)  # new rerun: "retry errors" retries the "libpff crash" items once
    repo.log(jid, "Rerun (errors only)" if mode == "errors" else "Full rerun (already imported items skipped)")
    return repo.get_job(jid)  # type: ignore[return-value]


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: int):
    return _requeue(jid, "full")


@app.post("/api/jobs/{jid}/retry-errors")
def retry_errors(jid: int):
    return _requeue(jid, "errors")


@app.get("/api/jobs/{jid}/log")
def job_log(jid: int, after: int = 0, limit: int = 1000):
    _job_or_404(jid)
    rows = db.q("SELECT id, ts, level, msg FROM job_logs WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (jid, after, max(1, min(limit, 10000))))
    lines = [{"n": r["id"], "ts": r["ts"], "level": r["level"], "msg": r["msg"]} for r in rows]
    return {"lines": lines, "next": lines[-1]["n"] if lines else after}


@app.get("/api/jobs/{jid}/report")
def job_report(jid: int, download: int = 0):
    r = _job_or_404(jid)
    p = Path(r["report_path"]) if r["report_path"] else settings.reports_dir / f"job-{jid}.json"
    if not p.exists():
        raise HTTPException(404, "report not available yet")
    data = json.loads(p.read_text(encoding="utf-8"))
    cmp = db.q1("SELECT * FROM compares WHERE job_id=? AND status='done' ORDER BY id DESC LIMIT 1", (jid,))
    if cmp is not None:  # the last comparison asked for this job, with the date it was made
        data["compare"] = {"id": cmp["id"], "generated_at": cmp["updated_at"], **(db.jload(cmp["result"], {}) or {})}
    headers = {"Content-Disposition": f'attachment; filename="job-report-{jid}.json"'} if download else {}
    return JSONResponse(data, headers=headers)


@app.get("/api/jobs/{jid}/errors")
def job_errors(jid: int, limit: int = 1000):
    r = _job_or_404(jid)
    a = db.q1("SELECT * FROM accounts WHERE id=?", (r["account_id"],))
    pkey = items_key_of_account(a) if a is not None else None
    if pkey is None:
        return []
    rows = db.q("SELECT id, kind, folder, item_key, subject, error, updated_at FROM items WHERE account_id=? AND pst_key=? "
                "AND status='error' ORDER BY id LIMIT ?", (r["account_id"], pkey, max(1, min(limit, 10000))))
    return [dict(x) for x in rows]


# ======================================================================= SSE
@app.get("/api/events")
async def events(request: Request, max_seconds: float = 0):
    """SSE fed by the `events` table (SQLite triggers): the worker is another process.
    `max_seconds` > 0 closes the stream after that delay (the browser reconnects by itself)."""
    deadline = time.time() + max_seconds if max_seconds > 0 else None

    async def gen():
        r = db.q1("SELECT MAX(id) m FROM events")
        last = (r["m"] or 0) if r else 0
        yield "retry: 3000\n\n"
        idle = 0.0
        while True:
            if await request.is_disconnected() or (deadline and time.time() > deadline):
                break
            rows = await asyncio.to_thread(db.q, "SELECT id, kind, ref_id FROM events WHERE id>? ORDER BY id LIMIT 5000", (last,))
            if rows:
                last = rows[-1]["id"]
                seen: dict[tuple[str, int], None] = {}
                for e in rows:
                    seen[(e["kind"], e["ref_id"])] = None
                for kind, ref in seen:
                    obj = await asyncio.to_thread(repo.event_object, kind, ref)
                    if obj is None:
                        obj = {"id": ref, "deleted": True}
                    yield f"event: {kind}\ndata: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"
                idle = 0.0
            else:
                idle += 0.5
                if idle >= 15:
                    idle = 0.0
                    yield ": ping\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _prune_events() -> None:
    while True:
        try:
            await asyncio.to_thread(db.ex, "DELETE FROM events WHERE ts < ?", (time.time() - 3600,))
        except Exception:
            pass
        await asyncio.sleep(300)


app.include_router(connectors_router)  # before the /api catch-all below
app.include_router(planning_router)
app.include_router(repair_router)
app.include_router(tokens_router)
app.include_router(sources_router)


# ======================================================================= static SPA
@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_404(rest: str):
    raise HTTPException(404, "unknown API route")


@app.get("/{path:path}")
def spa(path: str):
    root = settings.web_dir.resolve()
    if path:
        f = (root / path).resolve()
        if f.is_file() and (f == root or root in f.parents):
            # Hashed file names from the build: the URL changes with the content.
            immutable = path.startswith("assets/")
            return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable"} if immutable else None)
        if path.startswith("assets/"):  # a stale page asking for an old chunk must not get HTML
            raise HTTPException(404, "unknown asset")
    index = root / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return PlainTextResponse("Web interface missing: build it with `npm run build` in web/ (app/web/index.html)", status_code=200)


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("app.server.web:app", host="0.0.0.0", port=settings.port, proxy_headers=True, forwarded_allow_ips="*",
                timeout_keep_alive=75, access_log=False, h11_max_incomplete_event_size=1024 * 1024)


if __name__ == "__main__":
    main()
