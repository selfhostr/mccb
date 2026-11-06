"""Migration policy of a server and of an account: folder mapping, filters, allowed hours, bandwidth.

Stored as JSON in `servers.policy` and `accounts.policy` (NULL = nothing configured = behaviour of earlier versions).
The account overrides the server field by field; the lists (folder_map, exclude_folders) are added, the account's
rules first. Allowed hours and the bandwidth limit belong to the server only: they protect a target server.

    {"folder_map": [{"source": "Sent Items", "target": "@sent"}, {"source": "Old", "target": "Archive/Old"}],
     "exclude_folders": ["Junk*", "Deleted Items"],
     "date_from": "2020-01-01", "date_to": "2023-12-31", "max_size_mb": 25,
     "window": {"days": [0, 1, 2, 3, 4], "start": "20:00", "end": "06:00", "tz": "Europe/Paris"},
     "rate_mb_s": 5}
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import db, foldermap

FILTERED = "filtered"                 # items.status of an item left out by a filter (not an error, counted done)
FILTER_PREFIX = "filtered: "          # start of items.error for those rows, followed by the reason
OVERSIZE_PREFIX = "[oversize] "       # start of items.error of a message the target refuses for its size
REASONS = ("date", "size", "folder")

COMMON_KEYS = {"folder_map", "exclude_folders", "date_from", "date_to", "max_size_mb"}
SERVER_KEYS = {"window", "rate_mb_s"}


# ------------------------------------------------------------------ validation
def _day(v, name: str) -> str | None:
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date YYYY-MM-DD") from None


def _hm(v, name: str) -> str:
    try:
        h, m = str(v).split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError
        return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        raise ValueError(f"window.{name} must be HH:MM") from None


def _tz(name: str | None):
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone or no tz database
        raise ValueError(f"unknown time zone {name!r}") from None


def check_window(w) -> dict | None:
    if w in (None, {}):
        return None
    if not isinstance(w, dict):
        raise ValueError("window must be an object")
    days = w.get("days")
    if days in (None, []):
        days = None
    elif not (isinstance(days, list) and all(isinstance(d, int) and 0 <= d <= 6 for d in days)):
        raise ValueError("window.days: list of 0 (Monday) to 6 (Sunday)")
    tz = str(w.get("tz") or "UTC")
    _tz(tz)
    out = {"start": _hm(w.get("start", "00:00"), "start"), "end": _hm(w.get("end", "00:00"), "end"), "tz": tz}
    if days is not None:
        out["days"] = sorted(set(days))
    return out


def normalize(doc, server: bool) -> dict | None:
    """Checks a policy document and returns it in canonical form (None when nothing is configured).
    Raises ValueError with a message for the API."""
    if doc in (None, {}):
        return None
    if not isinstance(doc, dict):
        raise ValueError("policy must be an object")
    allowed = COMMON_KEYS | (SERVER_KEYS if server else set())
    unknown = sorted(set(doc) - allowed)
    if unknown:
        raise ValueError("unknown or not allowed here: " + ", ".join(unknown))
    out: dict = {}
    rules = foldermap.check_rules(doc.get("folder_map"))
    if rules:
        out["folder_map"] = rules
    ex = doc.get("exclude_folders")
    if ex:
        if not (isinstance(ex, list) and all(isinstance(x, str) and x.strip() for x in ex)):
            raise ValueError("exclude_folders must be a list of folder names or patterns")
        out["exclude_folders"] = [x.strip() for x in ex]
    for k in ("date_from", "date_to"):
        d = _day(doc.get(k), k)
        if d:
            out[k] = d
    if out.get("date_from") and out.get("date_to") and out["date_from"] > out["date_to"]:
        raise ValueError("date_from is after date_to")
    if doc.get("max_size_mb") not in (None, ""):
        try:
            mb = float(doc["max_size_mb"])
        except (TypeError, ValueError):
            raise ValueError("max_size_mb must be a number") from None
        if mb <= 0:
            raise ValueError("max_size_mb must be positive")
        out["max_size_mb"] = mb
    if server:
        w = check_window(doc.get("window"))
        if w:
            out["window"] = w
        if doc.get("rate_mb_s") not in (None, ""):
            try:
                r = float(doc["rate_mb_s"])
            except (TypeError, ValueError):
                raise ValueError("rate_mb_s must be a number") from None
            if r <= 0:
                raise ValueError("rate_mb_s must be positive")
            out["rate_mb_s"] = r
    return out or None


def _load(row) -> dict:
    if row is None or "policy" not in row.keys():
        return {}
    d = db.jload(row["policy"], {})
    return d if isinstance(d, dict) else {}


# ------------------------------------------------------------------ allowed hours
def _minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def window_open(w: dict | None, now: float | None = None) -> bool:
    """Is `now` inside the allowed hours? A window whose end is before its start runs over midnight and belongs
    to the day it starts; start == end means the whole day. No window: always open."""
    if not w:
        return True
    t = datetime.fromtimestamp(time.time() if now is None else now, _tz(w.get("tz")))
    s, e = _minutes(w["start"]), _minutes(w["end"])
    days = set(w.get("days") or range(7))
    cur = t.hour * 60 + t.minute
    if s == e:
        return t.weekday() in days
    if s < e:
        return t.weekday() in days and s <= cur < e
    if cur >= s:
        return t.weekday() in days
    return (t.weekday() - 1) % 7 in days and cur < e


def next_open(w: dict | None, now: float | None = None) -> float | None:
    """When the window opens next (None: open now or never)."""
    if not w or window_open(w, now):
        return None
    now = time.time() if now is None else now
    for step in range(1, 8 * 24 * 60 + 1):  # minute by minute for a week: cheap, and exact for any zone
        if window_open(w, now + step * 60):
            return now + step * 60
    return None


def describe_window(w: dict | None) -> str:
    if not w:
        return "always"
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    days = w.get("days")
    d = "every day" if not days or len(days) == 7 else ", ".join(names[i] for i in days)
    return f"{d} {w['start']}-{w['end']} ({w.get('tz', 'UTC')})"


# ------------------------------------------------------------------ effective policy of a job
@dataclass
class Policy:
    rules: list[dict] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None      # exclusive upper bound (the day after `date_to`)
    max_size: int | None = None          # bytes
    window: dict | None = None
    rate: float | None = None            # bytes per second, for the whole server

    @property
    def has_filters(self) -> bool:
        return bool(self.date_from or self.date_to or self.max_size)

    def mapper(self) -> foldermap.Mapper:
        return foldermap.Mapper.of(self.rules)

    def excluded(self, path: list[str]) -> bool:
        return foldermap.excluded(self.exclude, path)

    def reason(self, when: datetime | None, size: int | None) -> str | None:
        """Why an item is left out (None: it is migrated). An item without a date is never judged on its date."""
        if when is not None and (self.date_from or self.date_to):
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if (self.date_from and when < self.date_from) or (self.date_to and when >= self.date_to):
                return "date"
        if self.max_size and size is not None and size > self.max_size:
            return "size"
        return None

    def reason_ts(self, ts: float | None) -> str | None:
        return self.reason(datetime.fromtimestamp(ts, timezone.utc), None) if ts is not None else None

    def summary(self) -> dict:
        """What the report says about the policy that was applied."""
        out: dict = {}
        if self.rules:
            out["folder_map"] = self.rules
        if self.exclude:
            out["exclude_folders"] = self.exclude
        if self.date_from:
            out["date_from"] = self.date_from.date().isoformat()
        if self.date_to:
            out["date_to"] = (self.date_to - timedelta(days=1)).date().isoformat()
        if self.max_size:
            out["max_size_mb"] = round(self.max_size / 2 ** 20, 6)
        if self.window:
            out["window"] = self.window
        if self.rate:
            out["rate_mb_s"] = round(self.rate / 2 ** 20, 6)
        return out


def _midnight(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc)


def effective(acc, srv) -> Policy:
    """Policy applied to a job of `acc` on `srv` (rows or None). Never raises: a damaged document is ignored."""
    a, s = _load(acc), _load(srv)
    p = Policy()
    p.rules = list(a.get("folder_map") or []) + list(s.get("folder_map") or [])
    p.exclude = list(dict.fromkeys(list(a.get("exclude_folders") or []) + list(s.get("exclude_folders") or [])))
    try:
        df = a.get("date_from") or s.get("date_from")
        dt = a.get("date_to") or s.get("date_to")
        p.date_from = _midnight(df) if df else None
        p.date_to = _midnight(dt) + timedelta(days=1) if dt else None
    except ValueError:
        pass
    mb = a.get("max_size_mb") or s.get("max_size_mb")
    p.max_size = int(float(mb) * 2 ** 20) if mb else None
    p.window = s.get("window") or None
    p.rate = float(s["rate_mb_s"]) * 2 ** 20 if s.get("rate_mb_s") else None
    return p


# ------------------------------------------------------------------ bandwidth
class Limiter:
    """Token bucket for the bytes sent to one server. Every job runs in its own process, so the limit is split:
    each one gets `rate / jobs running on that server` (counted in the database every few seconds). The bucket
    holds one second of data; a message bigger than that makes the sender wait until the debt is paid.

    Used by the threads of AppendPool before they send; `wait` returns as soon as `stop()` says so."""
    RECOUNT_SECONDS = 5.0

    def __init__(self, server_id: int, rate: float, log=None):
        self.server_id = server_id
        self.rate = float(rate)
        self.log = log
        self.lock = threading.Lock()
        self.share = self.rate
        self.jobs = 1
        self._counted = 0.0
        self._allow = self.rate
        self._t = time.monotonic()
        self.last_wait = 0.0            # monotonic time of the last wait: the tuner reads it (see active())
        self.waited = 0.0

    def _recount(self) -> None:
        now = time.monotonic()
        if now - self._counted < self.RECOUNT_SECONDS:
            return
        self._counted = now
        try:
            n = db.q1("SELECT COUNT(*) n FROM jobs j JOIN accounts a ON a.id=j.account_id "
                      "WHERE j.status='running' AND a.server_id=?", (self.server_id,))["n"]
        except Exception:  # noqa: BLE001 - DB busy: keep the previous share
            return
        n = max(1, n)
        if n != self.jobs:
            self.jobs, self.share = n, self.rate / n
            if self.log:
                self.log(f"Bandwidth limit {self.rate / 2 ** 20:.1f} MB/s for the server, shared by {n} running "
                         f"job(s): {self.share / 2 ** 20:.2f} MB/s each")

    def delay(self, nbytes: int) -> float:
        """Books `nbytes` and returns how long the caller must wait before sending them."""
        with self.lock:
            self._recount()
            now = time.monotonic()
            self._allow = min(self.share, self._allow + (now - self._t) * self.share)
            self._t = now
            self._allow -= max(0, nbytes)
            return -self._allow / self.share if self._allow < 0 else 0.0

    def wait(self, nbytes: int, stopping) -> None:
        d = self.delay(nbytes)
        if d <= 0:
            return
        self.last_wait = time.monotonic()
        end = time.monotonic() + d
        while True:
            left = end - time.monotonic()
            if left <= 0 or stopping():
                break
            time.sleep(min(0.2, left))
        self.waited += d

    def active(self, within: float = 15.0) -> bool:
        """Has the limit made a sender wait recently? Then the tuner must not read the backlog as a reason to open
        more connections: it would only queue more data behind the same limit."""
        return self.last_wait > 0 and time.monotonic() - self.last_wait < within


# ------------------------------------------------------------------ what a target says about size
_OVERSIZE_WORDS = ("TOOBIG", "TOO BIG", "TOO LARGE", "MESSAGE SIZE", "SIZE LIMIT", "SIZE EXCEEDS",
                   "EXCEEDS THE MAXIMUM", "EXCEEDS MAXIMUM", "EXCEEDS THE LIMIT", "MAXIMUM MESSAGE", "MAX MESSAGE",
                   "MESSAGE EXCEEDS", "LARGER THAN", "APPENDLIMIT", "EXCEEDS THE SIZE")


def is_oversize(text: str | None) -> bool:
    """Does a refusal of the target mean "this message is too big"? (RFC 7889 [TOOBIG] and the usual wordings.) A quota refusal is not one: the whole mailbox stops then."""
    up = (text or "").upper()
    if "OVERQUOTA" in up or "QUOTA EXCEEDED" in up or "OVER QUOTA" in up:
        return False
    return any(w in up for w in _OVERSIZE_WORDS)


def append_limit(caps) -> int | None:
    """APPENDLIMIT=<bytes> (RFC 7889) in the capabilities of an IMAP server."""
    for c in caps or ():
        c = str(c).upper()
        if c.startswith("APPENDLIMIT="):
            try:
                n = int(c.split("=", 1)[1])
            except ValueError:
                return None
            return n if n > 0 else None
    return None


def _size_text(n: int) -> str:
    return f"{n / 2 ** 20:.1f} MB" if n >= 2 ** 20 else f"{max(n, 0) / 1024:.0f} KB"


def oversize_message(size: int | None, limit: int | None, server_text: str = "") -> str:
    shown = _size_text(size or 0)
    if limit:
        return f"{OVERSIZE_PREFIX}{shown}, over the limit of the target ({_size_text(limit)})"
    return f"{OVERSIZE_PREFIX}{shown}, refused by the target for its size: {server_text.strip()[:300]}"


# ------------------------------------------------------------------ report
def report_extras(account_id: int, key: str | None, policy: Policy | None = None) -> dict:
    """`filtered` and `oversized` sections of a job report: one grouped query each, at the end of the job only."""
    out: dict = {"filtered": {"total": 0, "by_reason": {}, "by_folder": []},
                 "oversized": {"count": 0, "bytes": 0, "largest": 0, "items": []}}
    if policy is not None:
        out["policy"] = policy.summary()
    if not key:
        return out
    f = out["filtered"]
    for r in db.q("SELECT error, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND status=? GROUP BY error",
                  (account_id, key, FILTERED)):
        reason = (r["error"] or "").removeprefix(FILTER_PREFIX) or "other"
        f["by_reason"][reason] = f["by_reason"].get(reason, 0) + r["n"]
        f["total"] += r["n"]
    for r in db.q("SELECT kind, folder, COUNT(*) n FROM items WHERE account_id=? AND pst_key=? AND status=? "
                  "GROUP BY kind, folder ORDER BY n DESC LIMIT 200", (account_id, key, FILTERED)):
        f["by_folder"].append({"kind": r["kind"], "folder": r["folder"], "items": r["n"]})
    o = out["oversized"]
    row = db.q1("SELECT COUNT(*) n, COALESCE(SUM(size),0) s, COALESCE(MAX(size),0) m FROM items WHERE account_id=? "
                "AND pst_key=? AND status='error' AND error LIKE ?", (account_id, key, OVERSIZE_PREFIX + "%"))
    o["count"], o["bytes"], o["largest"] = row["n"], row["s"], row["m"]
    for r in db.q("SELECT kind, folder, item_key, subject, size, error FROM items WHERE account_id=? AND pst_key=? "
                  "AND status='error' AND error LIKE ? ORDER BY size DESC LIMIT 500",
                  (account_id, key, OVERSIZE_PREFIX + "%")):
        o["items"].append(dict(r))
    return out
