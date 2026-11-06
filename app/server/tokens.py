"""API tokens: bearer credentials for scripts and the `mccb` command line, next to the session cookie.

A token is `mccb_<8 hex>_<43 characters>`. The first part (`mccb_<8 hex>`, the prefix) is stored in clear to find the
row and to recognise the token in a list; only the SHA-256 of the whole token is kept. The secret part is 256 random
bits, so a plain hash is enough. The token is shown once, at creation. There is still a single administrator: a
token has the same rights as a session, except that it cannot create or revoke tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import db

PREFIX_LEN = len("mccb_") + 8
TOUCH_SECONDS = 60.0     # last_used_at is written at most once a minute per token


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(name: str) -> tuple[dict, str]:
    """Returns (row view, the token in clear: to show once)."""
    token = f"mccb_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"
    prefix = token[:PREFIX_LEN]
    cur = db.ex("INSERT INTO api_tokens(name, prefix, token_hash, created_at) VALUES (?,?,?,?)",
                (name.strip()[:100] or "token", prefix, _hash(token), time.time()))
    return get(cur.lastrowid), token


def view(r) -> dict:
    return {"id": r["id"], "name": r["name"], "prefix": r["prefix"], "created_at": r["created_at"],
            "last_used_at": r["last_used_at"], "revoked_at": r["revoked_at"]}


def get(tid: int) -> dict | None:
    r = db.q1("SELECT * FROM api_tokens WHERE id=?", (tid,))
    return view(r) if r else None


def listing() -> list[dict]:
    return [view(r) for r in db.q("SELECT * FROM api_tokens ORDER BY id DESC")]


def revoke(tid: int) -> bool:
    return bool(db.ex("UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                      (time.time(), tid)).rowcount)


def verify(token: str | None) -> bool:
    """Is this a live token? Constant-time comparison of the hash."""
    if not token or len(token) < PREFIX_LEN + 2 or not token.startswith("mccb_"):
        return False
    r = db.q1("SELECT id, token_hash, revoked_at, last_used_at FROM api_tokens WHERE prefix=?", (token[:PREFIX_LEN],))
    if r is None or r["revoked_at"] is not None or not hmac.compare_digest(_hash(token), r["token_hash"]):
        return False
    now = time.time()
    if not r["last_used_at"] or now - r["last_used_at"] > TOUCH_SECONDS:
        db.ex("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, r["id"]))
    return True


def verify_header(value: str | None) -> bool:
    """`Authorization: Bearer <token>`."""
    if not value:
        return False
    scheme, _, token = value.strip().partition(" ")
    return scheme.lower() == "bearer" and verify(token.strip())
