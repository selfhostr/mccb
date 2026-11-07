"""API tokens: list, create (shown once), revoke. Managed from a session only, so that a leaked token cannot mint
others. See tokens.py and docs/api.md."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import tokens

router = APIRouter(prefix="/api")


class TokenIn(BaseModel):
    name: str = Field("", max_length=100)


def _session_only(request: Request) -> None:
    if getattr(request.state, "via", "session") != "session":
        raise HTTPException(403, "API tokens are managed from a session, not with a token")


@router.get("/tokens")
def list_tokens(request: Request):
    _session_only(request)
    return tokens.listing()


@router.post("/tokens", status_code=201)
def create_token(body: TokenIn, request: Request):
    """The token is in the answer, once: only its hash is kept."""
    _session_only(request)
    view, token = tokens.create(body.name)
    return {**view, "token": token}


@router.delete("/tokens/{tid}")
def revoke_token(tid: int, request: Request):
    _session_only(request)
    if not tokens.revoke(tid):
        if tokens.get(tid) is None:
            raise HTTPException(404, "unknown token")
    return tokens.get(tid)
