"""Settings of the server-side intake: notifications, drop folder, auto chain.

Stored as one JSON document in `kv` (key `intake_settings`). Secrets (ntfy token, webhook token, SMTP password) are
encrypted with the application key (security.encrypt) and never returned: the view exposes `has_<secret>` only.
On update, an omitted secret is kept and an explicit empty string clears it."""
from __future__ import annotations

import copy
import json
import re
import time

from . import db
from .security import decrypt, encrypt

KEY = "intake_settings"

EVENTS = {  # event kind -> enabled by default
    "upload_finished": False,
    "analysis_valid": False,
    "analysis_invalid": True,
    "job_done": True,
    "job_done_with_errors": True,
    "job_failed": True,
    "job_stopped": False,
    "phase_mail": False,
    "phase_calendar": False,
    "phase_contacts": False,
    "batch_finished": True,
    "disk_low": True,
    "repeated_crash": True,
}

DEFAULTS: dict = {
    "notifications": {
        "enabled": False,
        "mask_addresses": False,
        "group_window": 30,    # seconds without a new event before a group of events is sent
        "disk_low_mb": 5120,   # free space on the PST volume below which "disk_low" fires
        "events": dict(EVENTS),
        "ntfy": {"enabled": False, "url": "https://ntfy.sh", "topic": "", "priority": 3, "token_enc": ""},
        "webhook": {"enabled": False, "url": "", "token_enc": ""},
        "email": {"enabled": False, "host": "", "port": 587, "security": "starttls", "username": "",
                  "password_enc": "", "from": "", "to": ""},
    },
    "inbox": {"enabled": True, "settle_seconds": 60},
    "auto_chain": {"enabled": False, "start_import": False, "default_domains": {}, "enabled_at": 0},
}

SECRETS = {("notifications", "ntfy"): "token", ("notifications", "webhook"): "token",
           ("notifications", "email"): "password"}
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict) and k != "default_domains":
            out[k] = _merge(out[k], v)
        elif k in out or k == "default_domains":
            out[k] = v
    return out


def load() -> dict:
    raw = db.kv_get(KEY)
    try:
        stored = json.loads(raw) if raw else {}
    except ValueError:
        stored = {}
    return _merge(DEFAULTS, stored if isinstance(stored, dict) else {})


def secret(section: str, channel: str) -> str:
    """Decrypted secret of a channel ("" when unset or undecryptable)."""
    name = SECRETS[(section, channel)]
    try:
        return decrypt(load()[section][channel].get(name + "_enc", ""))
    except ValueError:
        return ""


def view(s: dict | None = None) -> dict:
    s = copy.deepcopy(s or load())
    for (sec, ch), name in SECRETS.items():
        d = s[sec][ch]
        d[f"has_{name}"] = bool(d.pop(name + "_enc", ""))
    return s


def _bool(v, what: str) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{what} must be true or false")
    return v


def _int(v, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{what} must be an integer between {lo} and {hi}")
    return v


def _str(v, what: str, maxlen: int = 500) -> str:
    if not isinstance(v, str) or len(v) > maxlen:
        raise ValueError(f"{what} must be a string")
    return v.strip()


def _url(v, what: str, allow_empty: bool = True) -> str:
    v = _str(v, what, 1000)
    if v and not re.match(r"^https?://[^\s/]+", v, re.I):
        raise ValueError(f"{what} must be an http(s) URL")
    if not v and not allow_empty:
        raise ValueError(f"{what} is required")
    return v.rstrip("/")


def update(patch: dict) -> dict:
    """Validated merge of `patch` into the stored settings. Raises ValueError (message for the client)."""
    if not isinstance(patch, dict):
        raise ValueError("body must be an object")
    cur = load()
    new = copy.deepcopy(cur)

    n = patch.get("notifications")
    if n is not None:
        if not isinstance(n, dict):
            raise ValueError("notifications must be an object")
        d = new["notifications"]
        if "enabled" in n:
            d["enabled"] = _bool(n["enabled"], "notifications.enabled")
        if "mask_addresses" in n:
            d["mask_addresses"] = _bool(n["mask_addresses"], "notifications.mask_addresses")
        if "group_window" in n:
            d["group_window"] = _int(n["group_window"], "notifications.group_window", 0, 3600)
        if "disk_low_mb" in n:
            d["disk_low_mb"] = _int(n["disk_low_mb"], "notifications.disk_low_mb", 1, 100_000_000)
        ev = n.get("events")
        if ev is not None:
            if not isinstance(ev, dict):
                raise ValueError("notifications.events must be an object")
            for k, v in ev.items():
                if k not in EVENTS:
                    raise ValueError(f"unknown event: {k}")
                d["events"][k] = _bool(v, f"events.{k}")
        for ch in ("ntfy", "webhook", "email"):
            c = n.get(ch)
            if c is None:
                continue
            if not isinstance(c, dict):
                raise ValueError(f"notifications.{ch} must be an object")
            t = d[ch]
            if "enabled" in c:
                t["enabled"] = _bool(c["enabled"], f"{ch}.enabled")
            if ch == "ntfy":
                if "url" in c:
                    t["url"] = _url(c["url"], "ntfy.url", allow_empty=False)
                if "topic" in c:
                    topic = _str(c["topic"], "ntfy.topic", 200)
                    if topic and not re.match(r"^[A-Za-z0-9_-]{1,64}$", topic):
                        raise ValueError("ntfy.topic: letters, digits, '_' and '-' only (max 64)")
                    t["topic"] = topic
                if "priority" in c:
                    t["priority"] = _int(c["priority"], "ntfy.priority", 1, 5)
            elif ch == "webhook":
                if "url" in c:
                    t["url"] = _url(c["url"], "webhook.url")
            else:
                if "host" in c:
                    t["host"] = _str(c["host"], "email.host", 255)
                if "port" in c:
                    t["port"] = _int(c["port"], "email.port", 1, 65535)
                if "security" in c:
                    if c["security"] not in ("ssl", "starttls", "none"):
                        raise ValueError("email.security must be ssl, starttls or none")
                    t["security"] = c["security"]
                for f in ("username", "from", "to"):
                    if f in c:
                        t[f] = _str(c[f], f"email.{f}", 500)
            name = SECRETS[("notifications", ch)]
            if name in c:  # omitted = kept, "" = cleared
                val = c[name]
                if not isinstance(val, str) or len(val) > 2000:
                    raise ValueError(f"{ch}.{name} must be a string")
                t[name + "_enc"] = encrypt(val) if val else ""
        # an enabled channel must be complete
        if d["enabled"]:
            if d["ntfy"]["enabled"] and not d["ntfy"]["topic"]:
                raise ValueError("ntfy.topic is required when ntfy is enabled")
            if d["webhook"]["enabled"] and not d["webhook"]["url"]:
                raise ValueError("webhook.url is required when the webhook is enabled")
            e = d["email"]
            if e["enabled"] and not (e["host"] and e["from"] and e["to"]):
                raise ValueError("email.host, email.from and email.to are required when e-mail is enabled")

    i = patch.get("inbox")
    if i is not None:
        if not isinstance(i, dict):
            raise ValueError("inbox must be an object")
        if "enabled" in i:
            new["inbox"]["enabled"] = _bool(i["enabled"], "inbox.enabled")
        if "settle_seconds" in i:
            new["inbox"]["settle_seconds"] = _int(i["settle_seconds"], "inbox.settle_seconds", 0, 86400)

    a = patch.get("auto_chain")
    if a is not None:
        if not isinstance(a, dict):
            raise ValueError("auto_chain must be an object")
        c = new["auto_chain"]
        for f in ("enabled", "start_import"):
            if f in a:
                c[f] = _bool(a[f], f"auto_chain.{f}")
        if "default_domains" in a:
            dd = a["default_domains"]
            if not isinstance(dd, dict):
                raise ValueError("auto_chain.default_domains must be an object {server_id: domain}")
            out = {}
            for sid, dom in dd.items():
                if not str(sid).isdigit() or not isinstance(dom, str):
                    raise ValueError("auto_chain.default_domains: keys are server ids, values domains")
                dom = dom.strip().lstrip("@").lower()
                if dom and not _DOMAIN_RE.match(dom):
                    raise ValueError(f"invalid domain: {dom}")
                if dom:
                    out[str(int(sid))] = dom
            c["default_domains"] = out
        if c["enabled"] and not cur["auto_chain"]["enabled"]:
            c["enabled_at"] = time.time()  # only PSTs analysed from now on are chained
    if new["notifications"]["enabled"] and not cur["notifications"]["enabled"]:
        db.kv_set("notify:since", str(time.time()))  # no flood of past events when enabling

    db.kv_set(KEY, json.dumps(new))
    return new
