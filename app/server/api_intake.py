"""API of the server-side intake: notification / inbox / auto-chain settings, remote fetch of PSTs."""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, Body, HTTPException

from . import db, intake, intake_settings, netsafe, notify, repo
from .config import settings

router = APIRouter()


def _settings_view() -> dict:
    v = intake_settings.view()
    v["inbox"]["path"] = str(settings.inbox_dir)  # read-only: set with INBOX_DIR
    return v


@router.get("/api/intake/settings")
def get_settings():
    return _settings_view()


@router.put("/api/intake/settings")
def put_settings(body: dict = Body(...)):
    """Partial update of {notifications, inbox, auto_chain}. Secrets (notifications.ntfy.token,
    notifications.webhook.token, notifications.email.password): omitted = kept, "" = cleared."""
    try:
        intake_settings.update(body)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _settings_view()


@router.post("/api/intake/notifications/test")
def test_notification(body: dict = Body(default={})):
    """Sends a test message now to one channel (`{"channel": "ntfy"|"webhook"|"email"}`) or to every enabled one.
    The master switch is ignored so a channel can be tried before turning notifications on."""
    cfg = intake_settings.load()["notifications"]
    ch = body.get("channel")
    if ch is not None and ch not in notify.CHANNELS:
        raise HTTPException(422, "channel must be ntfy, webhook or email")
    names = [ch] if ch else [n for n in notify.CHANNELS if cfg[n]["enabled"]]
    if not names:
        raise HTTPException(422, "no channel enabled")
    msg = notify.Message("test", "mccb test notification", "If you read this, notifications work.\n"
                         "Example address: jane.doe@example.com", 3, {"test": True, "ts": time.time()})
    res = notify.deliver(msg, cfg, names, retries=False)
    return {"ok": all(r["ok"] for r in res.values()), "results": res}


@router.get("/api/intake/inbox")
def inbox_listing():
    """Files currently in the drop folder (waiting to be picked up, or skipped duplicates)."""
    cfg = intake_settings.load()["inbox"]
    now = time.time()
    files = []
    for p, st in intake.Inbox().files():
        files.append({"name": os.path.relpath(p, settings.inbox_dir), "size": st.st_size,
                      "modified_ago": round(max(0.0, now - st.st_mtime), 1)})
    return {"path": str(settings.inbox_dir), "enabled": cfg["enabled"],
            "settle_seconds": cfg["settle_seconds"], "files": files}


@router.post("/api/psts/fetch", status_code=201)
def fetch_pst(body: dict = Body(...)):
    """Starts a server-side download. `kind`:
    http  {url, token? | username? password?}
    sftp  {host, port?, path, username, password? | private_key? passphrase?, host_key_sha256?}
    s3    {endpoint, bucket, key, region?, access_key, secret_key}
    All kinds accept `filename?`. Secrets are encrypted and never returned. Returns the PST row (status
    'fetching', size 0 until the remote size is known)."""
    try:
        pid = intake.create_fetch(body)
    except netsafe.BlockedAddress as e:
        raise HTTPException(422, f"destination refused: {e}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    except intake.FetchError as e:
        raise HTTPException(507, str(e))
    return repo.get_pst(pid)


@router.post("/api/psts/{pid}/cancel")
def cancel_pst_fetch(pid: int):
    try:
        state = intake.cancel_fetch(pid)
    except LookupError:
        raise HTTPException(404, "unknown PST")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "state": state}


@router.post("/api/psts/{pid}/retry-fetch")
def retry_pst_fetch(pid: int):
    try:
        intake.retry_fetch(pid)
    except LookupError:
        raise HTTPException(404, "unknown PST")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return repo.get_pst(pid)
