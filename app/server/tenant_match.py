"""Pairs the mailboxes of a Microsoft 365 tenant with the accounts of a target server. Pure: no database, no network.

Keys are the account addresses, lower-cased. A mailbox matches by primary address, then user principal name, then
alias. An account wanted by two mailboxes goes to the stronger match; a tie, or the weaker one, is a conflict."""
from __future__ import annotations

from typing import Any

STATES = ("ready", "linked", "no_target", "no_source", "other_source", "conflict")  # display order
RANK = {"mail": 0, "upn": 1, "alias": 2}
RESOURCES = ("room", "equipment")


def _free(acct: dict, connection_id: int, tenant_id: str | None) -> bool:
    """No source worth keeping: none, a PST without a valid file, or a per-mailbox config of this very tenant."""
    kind = acct.get("source_kind")
    if not kind:
        return True
    if kind in ("pst", "files"):
        return not acct.get("pst_valid")
    return bool(kind == "m365_graph" and not acct.get("source_connection_id") and tenant_id
                and str(acct.get("source_tenant_id") or "").lower() == tenant_id.lower())


def _dav(acct: dict, server: dict, opt: str, key: str, extra: str | None) -> str:
    if not acct.get(opt, True):
        return "off"
    if not server.get("dav_enabled"):
        return "no_dav"
    if extra:
        return extra
    result = (acct.get("last_test_result") or {}).get(key)
    if server.get(f"{key}_ok") is False or (isinstance(result, dict) and result.get("ok") is False):
        return "failed"
    return "on" if isinstance(result, dict) and result.get("ok") else "unknown"


def coverage(acct: dict, server: dict, tasks_granted: bool) -> dict:
    """What lands where for one linked pair: on | off (option off) | no_dav | failed | unknown | no_permission."""
    imap = (acct.get("last_test_result") or {}).get("imap")
    if not acct.get("opt_mail", True):
        mail = "off"
    elif isinstance(imap, dict) and imap.get("ok") is False:
        mail = "failed"
    else:
        mail = "on"
    return {"mail": mail,
            "calendar": _dav(acct, server, "opt_calendar", "caldav", None),
            "contacts": _dav(acct, server, "opt_contacts", "carddav", None),
            "tasks": _dav(acct, server, "opt_calendar", "caldav", None if tasks_granted else "no_permission")}


def match(mailboxes: list[dict], accounts: list[dict], connection_id: int, server: dict | None = None,
          tasks_granted: bool = True, tenant_id: str | None = None) -> dict:
    """mailboxes: the listing of the tenant. accounts: those of the chosen server, as dicts {id, email, source_kind,
    source_connection_id, source_tenant_id?, pst_valid, opt_mail, opt_calendar, opt_contacts, last_test_result, job}.
    server: {dav_enabled, caldav_ok, carddav_ok}. Returns {rows, counts}."""
    server = server or {}
    by_email = {str(a["email"]).lower(): a for a in accounts}
    wanted: dict[int, list[tuple[int, int, str]]] = {}   # account id -> [(rank, mailbox index, alias used)]
    for i, mb in enumerate(mailboxes):
        for rank, addr in _addresses(mb):
            a = by_email.get(addr)
            if a is not None:
                wanted.setdefault(a["id"], []).append((rank, i, addr))
                break
    rows: list[dict] = []
    claimed: set[int] = set()
    matched: dict[int, tuple[dict, str, str]] = {}
    for i, mb in enumerate(mailboxes):
        hit = next(((rank, addr, by_email[addr]) for rank, addr in _addresses(mb) if addr in by_email), None)
        if hit is None:
            rows.append(_row("no_target", mb, None, None, server, tasks_granted))
            continue
        rank, addr, acct = hit
        claimed.add(acct["id"])
        best = sorted(wanted[acct["id"]])
        winner = best[0][1] if len(best) == 1 or best[0][0] < best[1][0] else None
        by = ("mail", "upn", "alias")[rank]
        if winner != i:
            rows.append(_row("conflict", mb, acct, by, server, tasks_granted))
            continue
        if acct.get("source_connection_id") == connection_id:
            state = "linked"
        elif _free(acct, connection_id, tenant_id):
            state = "ready"
        else:
            state = "other_source"
        rows.append(_row(state, mb, acct, by, server, tasks_granted, addr if by == "alias" else None))
    for a in accounts:
        if a["id"] not in claimed:
            rows.append(_row("no_source", None, a, None, server, tasks_granted))
    rows.sort(key=lambda r: (STATES.index(r["state"]), r["address"], r["account_id"] or 0))
    counts: dict[str, int] = {"all": len(rows), **{s: 0 for s in STATES}}
    for r in rows:
        counts[r["state"]] += 1
    return {"rows": rows, "counts": counts}


def _addresses(mb: dict) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for rank, addr in ((0, mb.get("mail")), (1, mb.get("upn"))):
        if addr:
            out.append((rank, str(addr).lower()))
    out += [(2, str(a).lower()) for a in mb.get("aliases") or []]
    return out


def _row(state: str, mb: dict | None, acct: dict | None, by: str | None, server: dict, tasks_granted: bool,
         via: str | None = None) -> dict:
    row: dict[str, Any] = {
        "state": state,
        "tickable": state in ("ready", "linked"),
        "ticked": state == "ready" and (mb or {}).get("kind") not in RESOURCES,
        "address": (mb or {}).get("mail") or (acct or {}).get("email", "").lower(),
        "mailbox": mb, "matched_by": by, "via": via,
        "account_id": acct["id"] if acct else None, "account_email": acct["email"] if acct else None,
        "test": acct.get("last_test_result") if acct else None,
        "job": acct.get("job") if acct else None,
        "coverage": coverage(acct, server, tasks_granted) if acct and state in ("ready", "linked") else None,
    }
    return row
