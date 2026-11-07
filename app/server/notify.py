"""Notifications: ntfy, generic webhook, e-mail.

Detection is done by the worker (`Notifier.tick`) by polling the database, so nothing here is hooked into the job
runner: a notification problem can never slow or break a job. Delivery runs on a background thread with short
timeouts and bounded retries; failures are logged and dropped. Events that happen together (many jobs finishing at
once) are grouped into one message."""
from __future__ import annotations

import logging
import queue
import re
import shutil
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from . import crash, db, intake_settings, netsafe
from .config import settings

log = logging.getLogger("notify")

CONNECT_TIMEOUT = 5.0
TOTAL_TIMEOUT = 10.0
SMTP_TIMEOUT = 15.0
RETRY_DELAYS = (1.0, 4.0)  # pauses between the (at most 3) attempts
ACTIVE = ("queued", "running", "paused", "stopping")
TERMINAL = ("done", "done_with_errors", "failed", "stopped")
PHASES = ("precheck", "mail", "calendar", "contacts", "verify")
MAX_LINES = 15
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9])[A-Za-z0-9.-]*\.([A-Za-z]{2,})")


@dataclass
class Message:
    kind: str
    title: str
    text: str
    priority: int | None = None  # 1..5; None = channel default
    data: dict = field(default_factory=dict)


def mask_text(t: str) -> str:
    """jane.doe@example.com -> j***@e***.com"""
    return EMAIL_RE.sub(r"\1***@\2***.\3", t)


def _mask_any(v):
    if isinstance(v, str):
        return mask_text(v)
    if isinstance(v, list):
        return [_mask_any(x) for x in v]
    if isinstance(v, dict):
        return {k: _mask_any(x) for k, x in v.items()}
    return v


# ----------------------------------------------------------------------------------------- channels
def _check_url(url: str) -> None:
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("invalid URL")
    netsafe.resolve(u.hostname, u.port or (443 if u.scheme == "https" else 80))


def _post_json(url: str, payload: dict, token: str) -> None:
    _check_url(url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT), follow_redirects=False) as c:
        r = c.post(url, json=payload, headers=headers)
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}")


def send_ntfy(cfg: dict, msg: Message) -> None:
    c = cfg["ntfy"]
    # ntfy "publish as JSON": POST to the server root URL (UTF-8 safe, no header encoding issues)
    payload = {"topic": c["topic"], "title": msg.title, "message": msg.text,
               "priority": msg.priority or c["priority"], "tags": ["package"]}
    _post_json(c["url"].rstrip("/") + "/", payload, intake_settings.secret("notifications", "ntfy"))


def send_webhook(cfg: dict, msg: Message) -> None:
    payload = {"event": msg.kind, "title": msg.title, "message": msg.text, "priority": msg.priority,
               "ts": time.time(), "data": msg.data}
    _post_json(cfg["webhook"]["url"], payload, intake_settings.secret("notifications", "webhook"))


def send_email(cfg: dict, msg: Message) -> None:
    c = cfg["email"]
    m = EmailMessage()
    m["Subject"] = msg.title.replace("\n", " ")
    m["From"], m["To"] = c["from"], c["to"]
    m.set_content(msg.text)
    netsafe.resolve(c["host"], c["port"])
    smtp = (smtplib.SMTP_SSL(c["host"], c["port"], timeout=SMTP_TIMEOUT, context=ssl.create_default_context())
            if c["security"] == "ssl" else smtplib.SMTP(c["host"], c["port"], timeout=SMTP_TIMEOUT))
    with smtp:
        if c["security"] == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if c["username"]:
            smtp.login(c["username"], intake_settings.secret("notifications", "email"))
        smtp.send_message(m)


CHANNELS = {"ntfy": send_ntfy, "webhook": send_webhook, "email": send_email}


def prepare(cfg: dict, msg: Message) -> Message:
    if cfg.get("mask_addresses"):
        return Message(msg.kind, mask_text(msg.title), mask_text(msg.text), msg.priority, _mask_any(msg.data))
    return msg


def deliver(msg: Message, cfg: dict | None = None, channels: list[str] | None = None,
            retries: bool = True) -> dict[str, dict]:
    """Synchronous delivery to the enabled channels (or `channels`). Returns {channel: {ok, error}}; never raises."""
    cfg = cfg or intake_settings.load()["notifications"]
    msg = prepare(cfg, msg)
    out: dict[str, dict] = {}
    for name in channels or [n for n in CHANNELS if cfg[n]["enabled"]]:
        delays = (0.0, *RETRY_DELAYS) if retries else (0.0,)
        err = ""
        for i, d in enumerate(delays):
            if d:
                time.sleep(d)
            try:
                CHANNELS[name](cfg, msg)
                err = ""
                break
            except Exception as e:  # network, DNS, refused address, SMTP...
                err = f"{type(e).__name__}: {e}"
                log.warning("notification %s via %s failed (attempt %d/%d): %s", msg.kind, name, i + 1,
                            len(delays), err)
        out[name] = {"ok": not err, "error": err or None}
    return out


class Dispatcher:
    """Background delivery thread with a bounded queue: `submit` never blocks and never raises."""

    def __init__(self, maxsize: int = 200):
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.thread = threading.Thread(target=self._run, daemon=True, name="notify")
        self.thread.start()

    def submit(self, msg: Message) -> bool:
        try:
            self.q.put_nowait(msg)
            return True
        except queue.Full:
            log.warning("notification queue full: %s dropped", msg.kind)
            return False

    def _run(self) -> None:
        while True:
            msg = self.q.get()
            if msg is None:
                self.q.task_done()
                return
            try:
                deliver(msg)
            except Exception:
                log.exception("notification delivery")
            finally:
                self.q.task_done()

    def stop(self) -> None:
        self.q.put(None)

    def drain(self, timeout: float = 10.0) -> None:
        """Wait until everything queued has been delivered or dropped (tests, shutdown)."""
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.05)


# ----------------------------------------------------------------------------------------- detection
@dataclass
class Event:
    group: str   # 'job' | 'pst' | 'phase'
    kind: str    # key of intake_settings.EVENTS
    ref: int
    label: str   # one line, used in grouped messages
    text: str    # full text, single message
    priority: int | None = None
    on: bool = True  # event toggle at detection time


def _claim(kind: str, ref: int) -> bool:
    cur = db.ex("INSERT OR IGNORE INTO notify_sent(kind, ref_id, ts) VALUES (?,?,?)", (kind, ref, time.time()))
    return cur.rowcount > 0


def _totals(scan: dict | None) -> str:
    t = (scan or {}).get("totals") or {}
    return ", ".join(f"{v} {k}" for k, v in t.items() if isinstance(v, int) and v)


class Notifier:
    def __init__(self, dispatcher: Dispatcher | None = None):
        self.dispatcher = dispatcher or Dispatcher()
        self.pending: list[Event] = []
        self.first_ts = 0.0
        self.last_ts = 0.0
        self.batch: list[Event] = []  # finished jobs since the queue was last idle
        self._cfg: dict | None = None
        self._cfg_t = 0.0
        self._disk_t = 0.0

    def cfg(self) -> dict:
        now = time.time()
        if self._cfg is None or now - self._cfg_t > 3:
            self._cfg, self._cfg_t = intake_settings.load()["notifications"], now
        return self._cfg

    # ------------------------------------------------------------------ polling
    def tick(self) -> None:
        try:
            cfg = self.cfg()
            if not cfg["enabled"] or not any(cfg[c]["enabled"] for c in CHANNELS):
                return
            since = db.kv_get("notify:since")
            if since is None:
                since = str(time.time())
                db.kv_set("notify:since", since)
            since_f = float(since)
            self._poll_psts(cfg, since_f)
            self._poll_jobs(cfg, since_f)
            self._poll_batches(cfg, since_f)
            self._poll_phases(cfg, since_f)
            self._poll_crashes(cfg)
            self._check_disk(cfg)
            self._flush(cfg)
        except Exception:
            log.exception("notifier")  # never propagates to the worker loop

    def _add(self, cfg: dict, ev: Event) -> None:
        ev.on = bool(cfg["events"].get(ev.kind, False))
        if ev.group == "job":
            self.batch.append(ev)  # job events always feed the batch detection, whatever their toggle
        elif not ev.on:
            return
        now = time.time()
        if not self.pending:
            self.first_ts = now
        self.last_ts = now
        self.pending.append(ev)

    def _poll_psts(self, cfg: dict, since: float) -> None:
        for r in db.q("SELECT * FROM psts WHERE status IN ('validating','valid','invalid') AND updated_at>=? "
                      "ORDER BY id", (since,)):
            name = r["filename"]
            if _claim("upload", r["id"]):
                self._add(cfg, Event("pst", "upload_finished", r["id"], name, f"Upload finished: {name}"))
            if r["status"] in ("valid", "invalid") and _claim("analysis", r["id"]):
                if r["status"] == "valid":
                    d = _totals(db.jload(r["scan"], None))
                    self._add(cfg, Event("pst", "analysis_valid", r["id"], f"{name} ({d})" if d else name,
                                         f"PST analysed, ready to import: {name}" + (f" ({d})" if d else "")))
                else:
                    self._add(cfg, Event("pst", "analysis_invalid", r["id"], f"{name}: {r['error'] or 'invalid'}",
                                         f"PST invalid: {name}\n{r['error'] or ''}".strip(), priority=4))

    def _poll_jobs(self, cfg: dict, since: float) -> None:
        for r in db.q(f"SELECT j.*, a.email AS email FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id "
                      f"WHERE j.status IN ({','.join('?' * len(TERMINAL))}) AND COALESCE(j.finished_at,0)>=? "
                      f"ORDER BY j.finished_at", (*TERMINAL, since)):
            if not _claim("job", r["id"]):
                continue
            who = r["email"] or f"mailbox {r['account_id']}"
            prog = db.jload(r["progress"], None) or {}
            counts = ", ".join(f"{k} {v.get('done', 0)}/{v.get('total', 0)}"
                               + (f" ({v['errors']} errors)" if v.get("errors") else "")
                               for k, v in prog.items() if isinstance(v, dict) and v.get("total"))
            st = r["status"]
            head = {"done": "Migration finished", "done_with_errors": "Migration finished with errors",
                    "failed": "Migration failed", "stopped": "Migration stopped"}[st]
            text = f"{head}: {who}" + (f"\n{counts}" if counts else "") + (f"\n{r['error']}" if r["error"] else "")
            self._add(cfg, Event("job", f"job_{st}", r["id"], f"{who}: {st.replace('_', ' ')}", text,
                                 priority=4 if st == "failed" else None))

    def _poll_batches(self, cfg: dict, since: float) -> None:
        """A named batch (POST /api/runs with `name`) whose jobs are all over: one `batch_finished` message with its
        name and totals. Its jobs then leave the generic "queue idle" summary, and the individual messages of the
        jobs that went well, so that it is not announced twice; the ones with problems keep their own."""
        done = db.q(f"SELECT b.* FROM batches b WHERE b.created_at>=? AND EXISTS (SELECT 1 FROM jobs j WHERE "
                    f"j.batch_id=b.id) AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.batch_id=b.id AND j.status IN "
                    f"({','.join('?' * len(ACTIVE))})) AND NOT EXISTS (SELECT 1 FROM notify_sent n WHERE "
                    f"n.kind='batch' AND n.ref_id=b.id) ORDER BY b.id", (since, *ACTIVE))
        for b in done:
            if not _claim("batch", b["id"]):
                continue
            jobs = db.q("SELECT j.*, a.email AS email FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id "
                        "WHERE j.batch_id=? ORDER BY j.id", (b["id"],))
            ids = {j["id"] for j in jobs}
            self.batch = [e for e in self.batch if e.ref not in ids]
            self.pending = [e for e in self.pending
                            if not (e.group == "job" and e.ref in ids and e.kind in ("job_done", "job_stopped"))]
            if not cfg["events"].get("batch_finished"):
                continue
            counts: dict[str, int] = {}
            for j in jobs:
                counts[j["status"]] = counts.get(j["status"], 0) + 1
            bad = [j for j in jobs if j["status"] in ("failed", "done_with_errors")]
            head = f"Batch '{b['name']}' finished: " + ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in
                                                                 sorted(counts.items()))
            lines = [f"- {j['email'] or 'mailbox ' + str(j['account_id'])}: {j['status'].replace('_', ' ')}"
                     + (f" ({j['error']})" if j["error"] else "") for j in bad[:MAX_LINES]]
            if len(bad) > MAX_LINES:
                lines.append(f"... and {len(bad) - MAX_LINES} more")
            self.dispatcher.submit(Message("batch_finished", head, "\n".join(lines) or head, 4 if bad else None,
                                           {"batch_id": b["id"], "name": b["name"], "jobs": len(jobs),
                                            "by_status": counts}))

    def _poll_phases(self, cfg: dict, since: float) -> None:
        if not any(cfg["events"].get(f"phase_{k}") for k in ("mail", "calendar", "contacts")):
            return
        for r in db.q(f"SELECT j.*, a.email AS email FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id "
                      f"WHERE COALESCE(j.started_at,j.created_at)>=? AND j.status IN "
                      f"({','.join('?' * (len(ACTIVE) + 2))})", (since, *ACTIVE, "done", "done_with_errors")):
            prog = db.jload(r["progress"], None) or {}
            ph = PHASES.index(r["phase"]) if r["phase"] in PHASES else 0
            for k in ("mail", "calendar", "contacts"):
                p = prog.get(k) or {}
                over = r["status"] in ("done", "done_with_errors") or ph > PHASES.index(k)
                if over and p.get("total") and p.get("done", 0) + p.get("errors", 0) >= p["total"] \
                        and _claim(f"phase:{k}", r["id"]):
                    who = r["email"] or f"mailbox {r['account_id']}"
                    self._add(cfg, Event("phase", f"phase_{k}", r["id"], f"{who}: {k}",
                                         f"Phase finished ({k}): {who}\n{p.get('done', 0)}/{p['total']}"
                                         + (f", {p['errors']} errors" if p.get("errors") else "")))

    def _poll_crashes(self, cfg: dict) -> None:
        if not cfg["events"].get("repeated_crash"):
            return
        for r in db.q(f"SELECT id, account_id FROM jobs WHERE status IN ('queued','running')"):
            n = crash.get_crashes(r["id"])
            if n >= 2 and _claim("crash", r["id"]):
                self._immediate(cfg, Message("repeated_crash", "Repeated crash",
                                             f"Job {r['id']} crashed {n} times (libpff). It resumes after the "
                                             f"faulty item; it is failed after {settings.max_child_crashes}.",
                                             priority=4, data={"job_id": r["id"], "crashes": n}))

    def _check_disk(self, cfg: dict) -> None:
        now = time.time()
        if not cfg["events"].get("disk_low") or now - self._disk_t < 60:
            return
        self._disk_t = now
        try:
            settings.ensure_dirs()
            free = shutil.disk_usage(settings.pst_dir).free
        except OSError:
            return
        limit = cfg["disk_low_mb"] * 2 ** 20
        if free < limit:
            last = float(db.kv_get("notify:disk_alert") or 0)
            if now - last > 6 * 3600:
                db.kv_set("notify:disk_alert", str(now))
                self._immediate(cfg, Message("disk_low", "Disk space low",
                                             f"{free // 2 ** 20} MB free on the PST volume "
                                             f"(threshold {cfg['disk_low_mb']} MB).", priority=4,
                                             data={"free_bytes": free}))
        elif free >= 2 * limit:
            db.kv_set("notify:disk_alert", "0")

    # ------------------------------------------------------------------ sending
    def _immediate(self, cfg: dict, msg: Message) -> None:
        self.dispatcher.submit(msg)

    def _summary(self, kind: str, title: str, events: list[Event]) -> Message:
        lines = [f"- {e.label}" for e in events[:MAX_LINES]]
        if len(events) > MAX_LINES:
            lines.append(f"... and {len(events) - MAX_LINES} more")
        pr = max([e.priority or 0 for e in events] or [0]) or None
        return Message(kind, title, "\n".join(lines), pr,
                       {"count": len(events), "items": [e.label for e in events]})

    def _flush(self, cfg: dict) -> None:
        if not self.pending:
            return
        now = time.time()
        win = cfg["group_window"]
        if now - self.last_ts < win and now - self.first_ts < max(win * 4, 120):
            return
        events, self.pending = self.pending, []
        idle = db.q1(f"SELECT COUNT(*) n FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE))})",
                     ACTIVE)["n"] == 0
        by_group: dict[str, list[Event]] = {}
        for e in events:
            by_group.setdefault(e.group, []).append(e)
        nouns = {"job": "migrations", "pst": "PST files", "phase": "phases"}
        for group, evs in by_group.items():
            if group == "job" and idle and len(self.batch) >= 2 and cfg["events"].get("batch_finished"):
                bad = [e for e in self.batch if e.kind in ("job_failed", "job_done_with_errors")]
                self.dispatcher.submit(self._summary(
                    "batch_finished", f"Batch finished: {len(self.batch)} migrations"
                    + (f", {len(bad)} with problems" if bad else ""), self.batch))
                continue
            evs = [e for e in evs if e.on]
            if len(evs) == 1:
                e = evs[0]
                self.dispatcher.submit(Message(e.kind, e.text.split("\n")[0], e.text, e.priority,
                                               {"ref_id": e.ref}))
            elif evs:
                self.dispatcher.submit(self._summary(f"{group}_group", f"{len(evs)} {nouns[group]}", evs))
        if idle:
            self.batch = []
