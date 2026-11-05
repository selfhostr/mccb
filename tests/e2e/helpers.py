"""End to end: REAL engine (libpff), REAL HTTP API (uvicorn) and REAL worker in a subprocess,
against GreenMail (IMAP, authentication enabled) and Radicale (CalDAV/CardDAV, htpasswd) from docker-compose.test.yml.

Each "Stack" = a temporary DATA_DIR + `python -m app.server.web` + `python -m app.server.worker` started with
a clean environment (no PSTMIG_* variable inherited from tests/server: no fake engine).
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import os
import re
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.svcusers import create_user  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
IMAP_HOST = os.environ.get("TEST_IMAP_HOST")
IMAP_PORT = int(os.environ.get("TEST_IMAP_PORT", "3143"))
DAV_URL = os.environ.get("TEST_DAV_URL")
ADMIN = "e2e-admin-é"
CHUNK = 1024 * 1024
TERMINAL = ("done", "done_with_errors", "failed", "stopped")

ENABLED = bool(IMAP_HOST and DAV_URL and os.environ.get("TEST_GREENMAIL_API"))


# ====================================================================== pile web + worker
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Stack:
    def __init__(self, base: Path, **extra_env: str):
        self.base = base
        self.data = base / "data"
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("PSTMIG_") and k not in ("ADMIN_PASSWORD", "DATA_DIR", "UPLOAD_CHUNK_SIZE")}
        env.update({
            "DATA_DIR": str(self.data), "ADMIN_PASSWORD": ADMIN, "UPLOAD_CHUNK_SIZE": str(CHUNK),
            "UPLOAD_DISK_MARGIN": "0", "PORT": str(self.port), "CHILD_TERM_GRACE": "5", "PYTHONPATH": str(ROOT),
            "PYTHONUNBUFFERED": "1", "WORKER_CONCURRENCY": "2",
        })
        env.update(extra_env)
        self.env = env
        self.web: subprocess.Popen | None = None
        self.worker: subprocess.Popen | None = None
        self.client: httpx.Client | None = None
        self._logs: list = []

    def _popen(self, mod: str, name: str, env: dict) -> subprocess.Popen:
        log = open(self.base / f"{name}.log", "ab")
        self._logs.append(log)
        # new session: the process group = this process + its subprocesses (targeted kill possible)
        return subprocess.Popen([sys.executable, "-m", mod], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)

    def start(self) -> "Stack":
        self.data.mkdir(parents=True, exist_ok=True)
        self.web = self._popen("app.server.web", "web", self.env)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=120)
        end = time.time() + 60
        while True:
            try:
                if self.client.get("/api/health").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert self.web.poll() is None, "web stopped at startup:\n" + self.tail("web")
            assert time.time() < end, "web not responding:\n" + self.tail("web")
            time.sleep(0.2)
        r = self.client.post("/api/login", json={"password": ADMIN})
        assert r.status_code == 200, r.text
        self.start_worker()
        return self

    def start_worker(self, **extra_env: str) -> subprocess.Popen:
        assert self.worker is None or self.worker.poll() is not None
        self.worker = self._popen("app.server.worker", "worker", {**self.env, **extra_env})
        return self.worker

    def stop_worker(self) -> None:
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(self.worker.pid, signal.SIGKILL)
                self.worker.wait()

    def close(self) -> None:
        self.stop_worker()
        if self.client:
            self.client.close()
        for p in (self.web,):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        for f in self._logs:
            f.close()

    def tail(self, name: str, n: int = 60) -> str:
        try:
            lines = (self.base / f"{name}.log").read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])

    # ------------------------------------------------------------ API
    def api(self, method: str, url: str, expect: int | tuple = 200, **kw):
        r = self.client.request(method, url, **kw)
        exp = (expect,) if isinstance(expect, int) else expect
        assert r.status_code in exp, f"{method} {url} -> {r.status_code} {r.text[:500]}"
        return r

    def job_log(self, jid: int) -> str:
        lines = self.client.get(f"/api/jobs/{jid}/log").json()["lines"]
        return "\n".join(f"[{l['level']}] {l['msg']}" for l in lines[-80:])

    def wait_job(self, jid: int, statuses=TERMINAL, timeout: float = 300, cond=None) -> dict:
        end = time.time() + timeout
        j = None
        while time.time() < end:
            j = self.client.get(f"/api/jobs/{jid}").json()
            if j["status"] in statuses and (cond is None or cond(j)):
                return j
            time.sleep(0.1)
        raise AssertionError(f"job {jid} stuck: {j}\n{self.job_log(jid)}\n--- worker\n{self.tail('worker')}")

    def create_server(self, **kw) -> dict:
        body = {"name": "e2e", "imap_host": IMAP_HOST, "imap_port": IMAP_PORT, "imap_security": "none",
                "verify_tls": False, "root_folder": None, "caldav_url": DAV_URL, "carddav_url": DAV_URL,
                "dav_enabled": True, "workers": 3, "notes": ""}
        body.update(kw)
        return self.api("POST", "/api/servers", 201, json=body).json()

    def create_account(self, server_id: int, email_addr: str, password: str) -> dict:
        return self.api("POST", "/api/accounts", 201,
                        json={"server_id": server_id, "email": email_addr, "password": password}).json()

    def upload(self, path: Path, bad_chunk: int | None = 0, name: str | None = None) -> dict:
        """Chunked upload via the API; `bad_chunk` is first sent with a wrong hash (rejected), then resent."""
        data = path.read_bytes()
        st = path.stat()
        up = self.api("POST", "/api/uploads", 200,
                      json={"filename": name or path.name, "size": len(data), "last_modified": int(st.st_mtime)}).json()
        cs = up["chunk_size"]
        assert cs == CHUNK, up
        n = (len(data) + cs - 1) // cs
        assert up["chunks"] == n
        for i in range(n):
            if i in up["received"]:
                continue
            c = data[i * cs:(i + 1) * cs]
            if i == bad_chunk:
                r = self.client.put(f"/api/uploads/{up['id']}/chunks/{i}", content=c,
                                    headers={"X-Chunk-Sha256": hashlib.sha256(c + b"x").hexdigest()})
                assert r.status_code == 422, r.text
                assert i not in self.api("GET", f"/api/uploads/{up['id']}").json()["received"]
            self.api("PUT", f"/api/uploads/{up['id']}/chunks/{i}", 200, content=c,
                     headers={"X-Chunk-Sha256": hashlib.sha256(c).hexdigest()})
        got = self.api("GET", f"/api/uploads/{up['id']}").json()
        assert sorted(got["received"]) == list(range(n)), got
        self.api("POST", f"/api/uploads/{up['id']}/complete")
        end = time.time() + 180
        while True:
            p = self.api("GET", f"/api/psts/{up['id']}").json()
            if p["status"] in ("valid", "invalid"):
                return p
            assert time.time() < end, f"validation stuck : {p}\n{self.tail('worker')}"
            time.sleep(0.2)


# ====================================================================== source (engine lu directement)
def attachments_of(msg) -> list[tuple]:
    """(name, decoded size | 'rfc822') of the message's named attachments/inline parts, in MIME order
    (without descending into attached messages, which count as one attachment)."""
    out = []

    def walk(part):
        ctype = part.get_content_type()
        fn = part.get_filename()
        disp = part.get_content_disposition()
        if ctype == "message/rfc822":
            out.append((fn, "rfc822"))
            return
        if part.is_multipart():
            for sub in part.get_payload():
                walk(sub)
            return
        if fn or disp == "attachment":
            out.append((fn, len(part.get_payload(decode=True) or b"")))

    walk(msg)
    return out


def fffd_problems(msg, where: str = "") -> list[str]:
    """U+FFFD in decoded headers or text/HTML bodies (including attached messages)."""
    bad = []
    for part in msg.walk():
        for k, v in part.items():
            if "\ufffd" in str(v):
                bad.append(f"{where} header {k}")
        if part.get_content_maintype() == "text" and not part.is_multipart():
            try:
                txt = part.get_content()
            except Exception as e:  # noqa: BLE001
                bad.append(f"{where} body {part.get_content_type()} unreadable : {e}")
                continue
            if "\ufffd" in txt:
                bad.append(f"{where} corps {part.get_content_type()}")
    return bad


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


_SOURCE_CACHE: dict = {}


def source_model(path: Path, pst_sha: str) -> dict:
    """Reference model built with the engine: mails per folder (import id, subject, attachments), DAV objects."""
    key = (str(path), pst_sha)
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    from app.engine import PstFile
    from app.server.runner import import_id_of
    # skipped: items the engine does not convert (e.g. deleted appointments filed in "Deleted Items")
    model = {"mail": {}, "calendar": {}, "contacts": {}, "errors": [], "skipped": {"mail": 0, "calendar": 0, "contacts": 0}}
    with PstFile(str(path)) as p:
        for f in p.folders():
            if f.kind not in ("mail", "calendar", "contacts"):
                continue
            items = model[f.kind].setdefault(tuple(f.path), [])
            for idx, k, h in p.iter_items(f):
                try:
                    if f.kind == "mail":
                        iid = import_id_of(pst_sha, k)
                        b = p.build_message(h, iid)
                        if b is None:
                            model["skipped"]["mail"] += 1
                            continue
                        items.append({"key": k, "idx": idx, "import_id": iid, "subject": b.subject,
                                      "attachments": attachments_of(parse(b.raw)), "n_att": b.attachments,
                                      "flags": set(b.flags or []), "internaldate": b.internaldate.timestamp(),
                                      "tz": b.internaldate.tzinfo})
                    else:
                        o = p.build_event(h) if f.kind == "calendar" else p.build_contact(h)
                        if o is None:
                            model["skipped"][f.kind] += 1
                            continue
                        items.append({"key": k, "idx": idx, "filename": o.filename, "summary": o.summary})
                except Exception as e:  # noqa: BLE001
                    model["errors"].append({"kind": f.kind, "folder": "/".join(f.path), "key": k, "error": repr(e)})
    _SOURCE_CACHE[key] = model
    return model


# ====================================================================== serveurs (lecture directe)
def _q(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')


class ImapView:
    """IMAP content of a mailbox: {import_id: [(mailbox, uid)]}, message count per mailbox."""

    def __init__(self, user: str, password: str = "pw"):
        self._cred = (user, password)
        self._connect()

    def _connect(self) -> None:
        self.M = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        self.M.login(*self._cred)

    def _reading(self, fn, *args):
        """Read-only: GreenMail sometimes closes the connection (EOF) under load → reconnect and retry."""
        for attempt in range(3):
            try:
                return fn(*args)
            except (imaplib.IMAP4.abort, OSError):
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
                self.close()
                self._connect()

    def close(self):
        try:
            self.M.logout()
        except Exception:  # noqa: BLE001
            pass

    def mailboxes(self) -> list[str]:
        typ, data = self.M.list()
        out = []
        for line in data:
            if isinstance(line, tuple):
                line = line[0] + b" " + _q(line[1].decode("utf-8")).encode()
            m = _LIST_RE.match(line)
            if not m or b"\\Noselect" in m.group("flags"):
                continue
            n = m.group("name").decode("utf-8")
            if n.startswith('"'):
                n = n[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            out.append(n)
        return out

    def ids(self) -> tuple[dict[str, list], dict[str, int]]:
        return self._reading(self._ids)

    def _ids(self) -> tuple[dict[str, list], dict[str, int]]:
        ids: dict[str, list] = {}
        counts: dict[str, int] = {}
        for mb in self.mailboxes():
            typ, d = self.M.select(_q(mb), readonly=True)
            assert typ == "OK", (mb, d)
            counts[mb] = int(d[0])
            if not counts[mb]:
                continue
            typ, data = self.M.uid("FETCH", "1:*", "(BODY.PEEK[HEADER.FIELDS (X-PST-Import-Id)])")
            for d in data:
                if isinstance(d, tuple):
                    uid = int(re.search(rb"UID (\d+)", d[0]).group(1))
                    v = email.message_from_bytes(d[1]).get("X-PST-Import-Id")
                    if v:
                        ids.setdefault(v.strip(), []).append((mb, uid))
        return ids, counts

    def fetch(self, mb: str, uid: int) -> tuple[bytes, set[str], float]:
        """(octets bruts, flags, INTERNALDATE en timestamp)."""
        return self._reading(self._fetch, mb, uid)

    def _fetch(self, mb: str, uid: int) -> tuple[bytes, set[str], float]:
        typ, _ = self.M.select(_q(mb), readonly=True)
        typ, data = self.M.uid("FETCH", str(uid), "(FLAGS INTERNALDATE BODY.PEEK[])")
        for d in data:
            if isinstance(d, tuple):
                meta = d[0] + b" " + b" ".join(x for x in data if isinstance(x, bytes))
                flags = set(re.search(rb"FLAGS \(([^)]*)\)", meta).group(1).decode().split()) - {"\\Recent"}
                idate = time.mktime(imaplib.Internaldate2tuple(meta))
                return d[1], flags, idate
        raise AssertionError(f"UID {uid} not found in {mb}")


def dav_names(url: str, user: str, password: str = "pw") -> set[str]:
    """Names of the resources (non-collections) of a DAV collection (PROPFIND Depth 1)."""
    from urllib.parse import unquote, urlparse
    body = b'<?xml version="1.0"?><propfind xmlns="DAV:"><prop><resourcetype/><getetag/></prop></propfind>'
    r = httpx.request("PROPFIND", url, content=body, auth=(user, password), timeout=60,
                      headers={"Depth": "1", "Content-Type": "application/xml"})
    assert r.status_code == 207, (r.status_code, r.text[:300])
    root = ET.fromstring(r.content)
    base = urlparse(url).path.rstrip("/")
    out = set()
    for resp in root.findall("{DAV:}response"):
        href = unquote(urlparse(resp.findtext("{DAV:}href")).path).rstrip("/")
        if href == unquote(base):
            continue
        if resp.find(".//{DAV:}resourcetype/{DAV:}collection") is not None:
            continue
        out.add(href.rsplit("/", 1)[-1])
    return out


def dav_get(url: str, name: str, user: str, password: str = "pw") -> str:
    from urllib.parse import quote
    r = httpx.get(url.rstrip("/") + "/" + quote(name, safe=""), auth=(user, password), timeout=60)
    assert r.status_code == 200, (r.status_code, name)
    return r.text


def verify_on_servers(stack, user: str, pst: dict, model: dict, report: dict, check_all: bool = True) -> dict:
    """Direct GreenMail/Radicale check vs source. Returns the imported {mail, calendar, contacts}."""
    sha = pst["pst_sha"]
    errors = report["errors"]
    err_keys = {(e["kind"], e["item_key"]) for e in errors}
    v = ImapView(user)
    try:
        ids, counts = v.ids()
        ours = {k: locs for k, locs in ids.items() if k.startswith(f"<{sha}-")}
        dups = {k: locs for k, locs in ours.items() if len(locs) != 1}
        assert not dups, f"doublons : {dups}"
        assert len(ours) == len(ids), "unexpected messages on the server"
        # count per folder = source − items in error (listed in the report)
        target_of = {tuple(f["path"]): f["target"] for f in report["folders"]}
        from app.server.imap import utf7_decode
        delim = report.get("imap_delimiter") or "."
        for pth, tgt in target_of.items():
            if tgt.upper() != "INBOX" and tgt not in ("Sent", "Drafts", "Trash", "Junk", "Outbox"):
                # folder name: accents intact (decoded modified UTF-7 = source name)
                assert utf7_decode(tgt).split(delim)[-1] == pth[-1].replace(delim, "_"), (pth, tgt)
        expected_ids = set()
        for path, items in model["mail"].items():
            exp = [it for it in items if ("mail", it["key"]) not in err_keys]
            expected_ids |= {it["import_id"] for it in exp}
            if not items:
                continue
            tgt = target_of.get(path)
            assert tgt, f"folder {path} missing from the report : {report['folders']}"
            got = sum(1 for locs in ours.values() if locs[0][0] == tgt)
            assert got == len(exp) == counts.get(tgt, 0), (path, tgt, got, len(exp), counts.get(tgt))
            fr = next(f for f in report["folders"] if f["target"] == tgt)
            assert fr["present"] == fr["sent"] == len(exp) and fr["ok"], fr
        assert set(ours) == expected_ids
        # content: attachments (names + sizes) identical to the engine output, no U+FFFD
        n_att = 0
        for path, items in model["mail"].items():
            for it in items:
                if it["import_id"] not in ours:
                    continue
                if not check_all and n_att > 30:
                    break
                mb, uid = ours[it["import_id"]][0]
                raw, flags, idate = v.fetch(mb, uid)
                msg = parse(raw)
                assert msg["X-PST-Import-Id"].strip() == it["import_id"]
                assert attachments_of(msg) == it["attachments"], (path, it["subject"])
                # all MAPI attachments of the source are present (named) in the MIME
                assert len(it["attachments"]) == it["n_att"], (path, it["subject"], it["attachments"], it["n_att"])
                assert all(n for n, _ in it["attachments"]), it["attachments"]
                assert flags == it["flags"], (path, it["subject"], flags, it["flags"])
                # INTERNALDATE = source date. GreenMail 2.1.2 quirk (not our client): a 12:xx hour
                # sent is read back as 00:xx (12-hour parsing) → a −12 h offset is tolerated in this one case.
                d = idate - it["internaldate"]
                noon = datetime.fromtimestamp(it["internaldate"], it["tz"]).hour == 12
                assert abs(d) < 2 or (noon and abs(d + 43200) < 2), (path, it["subject"], idate, it["internaldate"])
                bad = fffd_problems(msg, f"{'/'.join(path)} « {it['subject']} »")
                assert not bad, bad
                n_att += 1
    finally:
        v.close()
    out = {"mail": len(ours)}
    for phase in ("calendar", "contacts"):
        src = [it for items in model[phase].values() for it in items]
        kind = "event" if phase == "calendar" else "contact"
        src_ok = {it["filename"] for it in src if (kind, it["key"]) not in err_keys}
        d = report["dav"].get(phase) or {}
        if not src:
            out[phase] = 0
            continue
        names = dav_names(d["url"], user)
        assert names == src_ok, (phase, names, src_ok)
        for n in names:
            assert "\ufffd" not in dav_get(d["url"], n, user)
        assert d["present"] == d["sent"] == len(src_ok) and d["ok"], d
        out[phase] = len(names)
    return out


__all__ = ["Stack", "create_user", "source_model", "ImapView", "dav_names", "dav_get", "attachments_of",
           "fffd_problems", "parse", "FIXTURES", "TERMINAL", "verify_on_servers"]
