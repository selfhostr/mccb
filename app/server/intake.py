"""Server-side intake, run by the worker: drop folder, remote fetch (HTTP(S), SFTP, S3-compatible), auto chain.

A PST that arrives by any of these ways becomes a regular `psts` row and goes through the usual analysis
(status 'validating', then 'valid' / 'invalid'). While data is being brought in, the row has status 'fetching'
and `received_bytes` (progress); `size` is 0 until the total is known. Failed: 'fetch_failed'. Cancelling:
'cancelling' (the worker then removes the row and the partial file)."""
from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import io
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import db, intake_settings, netsafe, repo, uploads
from .config import settings
from .security import decrypt, encrypt

log = logging.getLogger("intake")

EXTS = (".pst", ".ost")
TEMP_SUFFIXES = (".part", ".partial", ".tmp", ".temp", ".crdownload", ".filepart", ".swp", ".!qb")
COPY_BUF = 4 * 1024 * 1024
PROGRESS_EVERY = 1.0
MAX_ATTEMPTS = 5
BACKOFF = (2.0, 5.0, 15.0, 30.0)
MAX_HOPS = 5
SHA_LEN = 16  # pst_sha is the first 16 hex digits, like uploads.complete


class FetchError(Exception):
    """Permanent failure (bad credentials, 404, no space...): no retry."""


class Cancelled(Exception):
    pass


# ====================================================================================== helpers
def is_candidate(name: str) -> bool:
    """A file the drop folder may pick up: .pst/.ost, not a dotfile (rsync temp names are `.name.XXXXXX`),
    not a known temporary suffix."""
    low = name.lower()
    if name.startswith((".", "~")) or low.endswith(TEMP_SUFFIXES):
        return False
    return low.endswith(EXTS)


def safe_filename(name: str, default: str = "download.pst") -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name[:200] or default


def stem_of(filename: str) -> str:
    low = filename.lower()
    for e in EXTS:
        if low.endswith(e):
            return filename[:-len(e)]
    return filename


def sha16_of_file(path: str, cancel=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(COPY_BUF)
            if not b:
                break
            h.update(b)
            if cancel:
                cancel()
    return h.hexdigest()[:SHA_LEN]


def _touch_progress(pid: int, received: int, total: int | None) -> None:
    """Persist progress; raises Cancelled when the row vanished or is being cancelled."""
    r = db.q1("SELECT status FROM psts WHERE id=?", (pid,))
    if r is None or r["status"] != "fetching":
        raise Cancelled()
    db.ex("UPDATE psts SET received_bytes=?, size=CASE WHEN ?>0 THEN ? ELSE size END, updated_at=? "
          "WHERE id=? AND status='fetching'", (received, total or 0, total or 0, time.time(), pid))


class _Progress:
    def __init__(self, pid: int):
        self.pid, self.last = pid, 0.0

    def __call__(self, received: int, total: int | None, force: bool = False) -> None:
        now = time.time()
        if force or now - self.last >= PROGRESS_EVERY:
            self.last = now
            _touch_progress(self.pid, received, total)


def _check_space(remaining: int) -> None:
    from fastapi import HTTPException
    try:
        uploads._check_space(max(0, remaining), None)
    except HTTPException as e:
        raise FetchError(str(e.detail))


def _fail(pid: int, msg: str) -> None:
    log.warning("PST %s: %s", pid, msg)
    db.ex("UPDATE psts SET status='fetch_failed', error=?, updated_at=? WHERE id=? AND status='fetching'",
          (msg[:500], time.time(), pid))


def _finish_download(pid: int, path: str, cancel=None) -> None:
    """File complete at `path`: fingerprint, then hand over to the analysis."""
    size = os.path.getsize(path)
    sha = sha16_of_file(path, cancel)
    cur = db.ex("UPDATE psts SET status='validating', error=NULL, size=?, received_bytes=?, pst_sha=?, "
                "updated_at=? WHERE id=? AND status='fetching'", (size, size, sha, time.time(), pid))
    if not cur.rowcount:
        raise Cancelled()


def _drop_partial(pid: int) -> None:
    for suffix in (".pst", ".pst.part"):
        try:
            os.unlink(settings.pst_dir / f"{pid}{suffix}")
        except OSError:
            pass


# ====================================================================================== drop folder
class Inbox:
    """Remembers what it saw: a file is stable when size and mtime did not change for `settle` seconds and it
    has been seen at least twice."""

    def __init__(self) -> None:
        self.seen: dict[str, tuple[tuple[int, int], float]] = {}
        self.ignored: set[tuple[str, int, int]] = set()

    def files(self) -> list[tuple[str, os.stat_result]]:
        root = settings.inbox_dir
        out = []
        if not root.is_dir():
            return out
        for dirpath, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for n in names:
                if is_candidate(n):
                    p = os.path.join(dirpath, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if os.path.isfile(p) and st.st_size > 0:
                        out.append((p, st))
        return out

    def stable(self, settle: float, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        found = self.files()
        live = {p for p, _ in found}
        for p in list(self.seen):
            if p not in live:
                del self.seen[p]
        ready = []
        for p, st in found:
            key = (st.st_size, st.st_mtime_ns)
            if (p, *key) in self.ignored:
                continue
            prev = self.seen.get(p)
            if prev is None or prev[0] != key:
                self.seen[p] = (key, now)  # new or still changing
                continue
            if now - prev[1] >= settle:
                ready.append(p)
        return ready

    def ignore(self, path: str) -> None:
        try:
            st = os.stat(path)
            self.ignored.add((path, st.st_size, st.st_mtime_ns))
        except OSError:
            pass

    def listing(self) -> list[dict]:
        now = time.time()
        out = []
        for p, st in self.files():
            key = (st.st_size, st.st_mtime_ns)
            prev = self.seen.get(p)
            out.append({"name": os.path.relpath(p, settings.inbox_dir), "size": st.st_size,
                        "stable_for": round(now - prev[1], 1) if prev and prev[0] == key else 0.0,
                        "skipped": (p, *key) in self.ignored})
        return out


def _inbox_row(pid: int):
    return db.q1("SELECT * FROM psts WHERE id=?", (pid,))


def register_inbox_file(path: str, cancel=None) -> int | None:
    """Create the PST row for a stable file of the drop folder, then move it into pst_dir. Returns the PST id,
    or None when the file is a duplicate of a PST already known (it is left in place, never deleted)."""
    st = os.stat(path)
    sha = sha16_of_file(path, cancel)
    dup = db.q1("SELECT id FROM psts WHERE pst_sha=? AND status NOT IN ('invalid','fetch_failed','cancelling')",
                (sha,))
    if dup:
        log.info("inbox: %s is identical to PST %s: left in place", path, dup["id"])
        return None
    now = time.time()
    name = safe_filename(os.path.basename(path), "inbox.pst")
    spec = encrypt(db.jdump({"kind": "inbox", "origin": path}))
    with db.tx() as c:
        # re-check under the write lock: two scans must not create two rows
        if c.execute("SELECT 1 FROM psts WHERE pst_sha=? AND status NOT IN ('invalid','fetch_failed','cancelling')",
                     (sha,)).fetchone():
            return None
        cur = c.execute(
            "INSERT INTO psts(filename,size,last_modified,chunk_size,status,created_at,updated_at,pst_sha,"
            "source,origin,received_bytes,fetch_spec) VALUES (?,?,?,?, 'fetching', ?, ?, ?, 'inbox', ?, 0, ?)",
            (name, st.st_size, int(st.st_mtime * 1000), settings.chunk_size, now, now, sha,
             "inbox/" + os.path.relpath(path, settings.inbox_dir), spec))
        pid = cur.lastrowid
        c.execute("UPDATE psts SET path=? WHERE id=?", (str(settings.pst_dir / f"{pid}.pst"), pid))
    try:
        move_into_place(pid, path, cancel)
    except Cancelled:
        raise
    except Exception as e:
        _fail(pid, f"{type(e).__name__}: {e}")
    return pid


def move_into_place(pid: int, origin: str, cancel=None) -> None:
    """Atomic rename when inbox and pst_dir share a filesystem, otherwise copy to `.part`, rename, remove the
    source. Safe to repeat after a restart: if the destination is already complete only the hand-over is done."""
    row = _inbox_row(pid)
    if row is None:
        raise Cancelled()
    dest = row["path"]
    size = row["size"]
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        pass  # moved before the restart; the source (copy case) may still exist
    elif os.path.exists(origin):
        settings.ensure_dirs()
        try:
            os.rename(origin, dest)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            _check_space(size)
            part = dest + ".part"
            prog = _Progress(pid)
            done = 0
            with open(origin, "rb") as src, open(part, "wb") as dst:
                while True:
                    b = src.read(COPY_BUF)
                    if not b:
                        break
                    dst.write(b)
                    done += len(b)
                    prog(done, size)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(part, dest)
    else:
        raise FetchError("source file vanished from the drop folder")
    if os.path.exists(origin) and os.path.exists(dest):
        try:
            if os.path.samefile(origin, dest):
                pass
            elif os.path.getsize(origin) == os.path.getsize(dest):
                os.unlink(origin)  # copied: the inbox no longer holds it
        except OSError:
            pass
    cur = db.ex("UPDATE psts SET status='validating', error=NULL, received_bytes=?, updated_at=? "
                "WHERE id=? AND status='fetching'", (os.path.getsize(dest), time.time(), pid))
    if not cur.rowcount:
        raise Cancelled()


# ====================================================================================== remote fetch
def _label(kind: str, spec: dict) -> str:
    if kind == "sftp":
        return f"sftp://{spec['host']}{spec['path']}"
    if kind == "s3":
        return f"s3://{spec['bucket']}/{spec['key']}"
    u = urlsplit(spec["url"])
    return f"{u.scheme}://{u.hostname}{':' + str(u.port) if u.port else ''}{u.path}"


def _req(p: dict, name: str, what: str | None = None) -> str:
    v = p.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{what or name} is required")
    return v.strip()


def build_spec(p: dict) -> tuple[dict, str, str]:
    """Validate a fetch request. Returns (spec incl. credentials, display label, file name). ValueError =
    message for the client. Also applies the address policy (BlockedAddress is a ValueError)."""
    kind = p.get("kind") or ("http" if p.get("url") else None)
    spec: dict = {"kind": kind}
    if kind == "http":
        url = _req(p, "url")
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("url must be an http(s) URL")
        if u.username:  # credentials in the URL: moved to the (encrypted) basic auth fields
            p = {**p, "username": p.get("username") or u.username, "password": p.get("password") or u.password}
            url = u._replace(netloc=u.netloc.rsplit("@", 1)[1]).geturl()
        spec["url"] = url
        if p.get("token"):
            spec["token"] = str(p["token"])
        elif p.get("username"):
            spec["username"], spec["password"] = str(p["username"]), str(p.get("password") or "")
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        name = os.path.basename(u.path)
    elif kind == "s3":
        ep = _req(p, "endpoint").rstrip("/")
        u = urlsplit(ep)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("endpoint must be an http(s) URL")
        spec.update(bucket=_req(p, "bucket"), key=_req(p, "key").lstrip("/"), region=(p.get("region") or "us-east-1"),
                    access_key=_req(p, "access_key"), secret_key=_req(p, "secret_key"))
        spec["url"] = f"{ep}/{spec['bucket']}/{quote(spec['key'], safe='/~')}"
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        name = os.path.basename(spec["key"])
    elif kind == "sftp":
        host = _req(p, "host")
        port = p.get("port") or 22
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError("port must be between 1 and 65535")
        spec.update(host=host, port=port, path=_req(p, "path"), username=_req(p, "username"))
        if p.get("private_key"):
            spec["private_key"], spec["passphrase"] = str(p["private_key"]), str(p.get("passphrase") or "")
        elif p.get("password"):
            spec["password"] = str(p["password"])
        else:
            raise ValueError("password or private_key is required")
        if p.get("host_key_sha256"):
            spec["host_key_sha256"] = str(p["host_key_sha256"]).strip()
        name = os.path.basename(spec["path"])
    else:
        raise ValueError("kind must be http, sftp or s3")
    netsafe.resolve(host, port)
    filename = safe_filename(str(p.get("filename") or name))
    return spec, _label(kind, spec), filename


def create_fetch(p: dict) -> int:
    spec, label, filename = build_spec(p)
    _check_space(0)
    now = time.time()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO psts(filename,size,last_modified,chunk_size,status,created_at,updated_at,source,origin,"
            "received_bytes,fetch_spec) VALUES (?,0,NULL,?, 'fetching', ?, ?, 'fetch', ?, 0, ?)",
            (filename, settings.chunk_size, now, now, label, encrypt(db.jdump(spec))))
        pid = cur.lastrowid
        c.execute("UPDATE psts SET path=? WHERE id=?", (str(settings.pst_dir / f"{pid}.pst"), pid))
    return pid


def cancel_fetch(pid: int) -> str:
    """'cancelling' when a transfer is running (the worker cleans up), 'removed' otherwise."""
    r = db.q1("SELECT status, source FROM psts WHERE id=?", (pid,))
    if r is None:
        raise LookupError("unknown PST")
    if r["source"] not in ("fetch", "inbox") or r["status"] not in ("fetching", "fetch_failed", "cancelling"):
        raise ValueError("this PST is not being fetched")
    if r["status"] == "fetching":
        db.ex("UPDATE psts SET status='cancelling', updated_at=? WHERE id=? AND status='fetching'",
              (time.time(), pid))
        return "cancelling"
    _remove_row(pid)
    return "removed"


def retry_fetch(pid: int) -> None:
    r = db.q1("SELECT status, source FROM psts WHERE id=?", (pid,))
    if r is None:
        raise LookupError("unknown PST")
    if r["status"] != "fetch_failed":
        raise ValueError("only a failed fetch can be retried")
    db.ex("UPDATE psts SET status='fetching', error=NULL, updated_at=? WHERE id=?", (time.time(), pid))


def _remove_row(pid: int) -> None:
    _drop_partial(pid)
    db.ex("DELETE FROM psts WHERE id=?", (pid,))


def _sigv4(spec: dict, url: str, host_header: str, rng: str | None, now: float | None = None) -> dict:
    t = time.gmtime(now if now is not None else time.time())
    amz_date, date = time.strftime("%Y%m%dT%H%M%SZ", t), time.strftime("%Y%m%d", t)
    payload_hash = hashlib.sha256(b"").hexdigest()
    hdrs = {"host": host_header, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    if rng:
        hdrs["range"] = rng
    names = sorted(hdrs)
    canonical = "\n".join(["GET", urlsplit(url).path or "/", urlsplit(url).query,
                           "".join(f"{n}:{hdrs[n]}\n" for n in names), ";".join(names), payload_hash])
    scope = f"{date}/{spec['region']}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def h(k: bytes, m: str) -> bytes:
        return hmac.new(k, m.encode(), hashlib.sha256).digest()

    key = h(h(h(h(("AWS4" + spec["secret_key"]).encode(), date), spec["region"]), "s3"), "aws4_request")
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"x-amz-date": amz_date, "x-amz-content-sha256": payload_hash,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={spec['access_key']}/{scope}, "
                             f"SignedHeaders={';'.join(names)}, Signature={sig}"}


def _http_download(spec: dict, dest: str, prog: _Progress) -> None:
    """GET with Range resume, manual redirects (each hop re-checked), connection pinned to the checked IP."""
    url = spec["url"]
    first_host = urlsplit(url).hostname
    restarted = False
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False, trust_env=False) as client:
        hop = 0
        while True:
            u = urlsplit(url)
            port = u.port or (443 if u.scheme == "https" else 80)
            ip = netsafe.resolve(u.hostname, port)[0]
            offset = os.path.getsize(dest) if os.path.exists(dest) else 0
            host_header = u.netloc.rsplit("@", 1)[-1]
            headers = {"Host": host_header, "User-Agent": "mccb", "Accept-Encoding": "identity"}
            rng = f"bytes={offset}-" if offset else None
            if rng:
                headers["Range"] = rng
            if spec["kind"] == "s3":
                headers.update(_sigv4(spec, url, host_header, rng))
            elif u.hostname == first_host:  # credentials never follow a redirect to another host
                if spec.get("token"):
                    headers["Authorization"] = f"Bearer {spec['token']}"
                elif spec.get("username"):
                    raw = f"{spec['username']}:{spec.get('password', '')}".encode()
                    headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
            netloc = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
            target = u._replace(netloc=netloc).geturl()
            ext = {"sni_hostname": u.hostname} if u.scheme == "https" else {}
            with client.stream("GET", target, headers=headers, extensions=ext) as r:
                code = r.status_code
                if code in (301, 302, 303, 307, 308):
                    if spec["kind"] == "s3" or hop >= MAX_HOPS or not r.headers.get("location"):
                        raise FetchError(f"unexpected redirect (HTTP {code})")
                    url = urljoin(url, r.headers["location"])
                    if urlsplit(url).scheme not in ("http", "https"):
                        raise FetchError("redirect to a non-HTTP URL refused")
                    hop += 1
                    continue
                if code == 416 and offset:
                    cr = r.headers.get("content-range", "")  # "bytes */total"
                    if cr.endswith("/" + str(offset)):
                        prog(offset, offset, force=True)
                        return  # already complete
                    if restarted:
                        raise FetchError("HTTP 416 after restart")
                    restarted = True
                    os.unlink(dest)  # the remote file changed or shrank: start over
                    continue
                if code not in (200, 206):
                    if 400 <= code < 500 and code not in (408, 429):
                        raise FetchError(f"HTTP {code}")
                    raise OSError(f"HTTP {code}")
                if code == 206:
                    m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", r.headers.get("content-range", ""))
                    if not m or int(m.group(1)) != offset:
                        raise OSError("unexpected Content-Range")
                    total = int(m.group(2)) if m.group(2) != "*" else None
                    mode = "ab"
                else:
                    offset, mode = 0, "wb"  # server ignored Range (or fresh download)
                    cl = r.headers.get("content-length")
                    total = int(cl) if cl and cl.isdigit() else None
                if total:
                    _check_space(total - offset)
                got = offset
                prog(got, total, force=True)
                with open(dest, mode) as f:
                    for chunk in r.iter_raw(COPY_BUF):
                        f.write(chunk)
                        got += len(chunk)
                        prog(got, total)
                    f.flush()
                    os.fsync(f.fileno())
                if total and got != total:
                    raise OSError(f"incomplete transfer ({got}/{total} bytes)")
                prog(got, total or got, force=True)
                return


def _host_key_forms(blob: bytes) -> set[str]:
    d = hashlib.sha256(blob).digest()
    return {d.hex(), "SHA256:" + base64.b64encode(d).decode().rstrip("=")}


def _sftp_download(spec: dict, dest: str, prog: _Progress, save_spec) -> None:
    try:
        import paramiko
    except ImportError:
        raise FetchError("SFTP support is not installed (paramiko)")
    import socket
    ip = netsafe.resolve(spec["host"], spec["port"])[0]
    sock = socket.create_connection((ip, spec["port"]), timeout=20)
    t = paramiko.Transport(sock)
    try:
        t.banner_timeout = t.auth_timeout = 20
        t.start_client(timeout=20)
        blob = t.get_remote_server_key().asbytes()
        forms = _host_key_forms(blob)
        pinned = spec.get("host_key_sha256")
        if pinned:
            if pinned.strip() not in forms and pinned.strip().lower() not in forms:
                raise FetchError("SFTP host key does not match host_key_sha256")
        else:  # trust on first use: remembered for every later attempt
            spec["host_key_sha256"] = sorted(forms)[0]
            save_spec(spec)
        try:
            if spec.get("private_key"):
                key = None
                for cls in ("Ed25519Key", "ECDSAKey", "RSAKey"):
                    try:
                        key = getattr(paramiko, cls).from_private_key(io.StringIO(spec["private_key"]),
                                                                       spec.get("passphrase") or None)
                        break
                    except (paramiko.SSHException, ValueError, AttributeError):
                        continue
                if key is None:
                    raise FetchError("private key not readable (format or passphrase)")
                t.auth_publickey(spec["username"], key)
            else:
                t.auth_password(spec["username"], spec["password"])
        except paramiko.AuthenticationException:
            raise FetchError("SFTP authentication failed")
        sftp = paramiko.SFTPClient.from_transport(t)
        try:
            total = sftp.stat(spec["path"]).st_size
        except IOError as e:
            raise FetchError(f"remote file not found or unreadable: {e}")
        offset = os.path.getsize(dest) if os.path.exists(dest) else 0
        if offset > total:
            offset = 0
        _check_space(total - offset)
        prog(offset, total, force=True)
        with sftp.open(spec["path"], "rb") as rf, open(dest, "ab" if offset else "wb") as f:
            if offset:
                rf.seek(offset)
            rf.set_pipelined(True)
            got = offset
            while got < total:
                b = rf.read(min(COPY_BUF, total - got))
                if not b:
                    break
                f.write(b)
                got += len(b)
                prog(got, total)
            f.flush()
            os.fsync(f.fileno())
        if got != total:
            raise OSError(f"incomplete transfer ({got}/{total} bytes)")
        prog(got, total, force=True)
    finally:
        t.close()


def run_fetch(pid: int, stop: threading.Event | None = None) -> None:
    """Whole life of a fetch: attempts with backoff (resuming), then hand-over to the analysis."""
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None or row["status"] != "fetching":
        return
    prog = _Progress(pid)
    try:
        spec = db.jload(decrypt(row["fetch_spec"]), None)
        if not spec:
            raise FetchError("fetch parameters missing")
    except ValueError as e:
        _fail(pid, str(e))
        return

    def save_spec(s: dict) -> None:
        db.ex("UPDATE psts SET fetch_spec=? WHERE id=?", (encrypt(db.jdump(s)), pid))

    settings.ensure_dirs()
    dest = row["path"]
    attempt = 0
    try:
        while True:
            try:
                if spec["kind"] == "sftp":
                    _sftp_download(spec, dest, prog, save_spec)
                else:
                    _http_download(spec, dest, prog)
                _finish_download(pid, dest, cancel=lambda: _touch_progress(pid, os.path.getsize(dest), None)
                                 if time.time() - prog.last >= PROGRESS_EVERY else None)
                return
            except (Cancelled, FetchError):
                raise
            except netsafe.BlockedAddress as e:
                raise FetchError(str(e))
            except Exception as e:  # network, timeouts, short transfer: retry, resuming
                attempt += 1
                log.warning("PST %s: attempt %d failed: %s", pid, attempt, e)
                if attempt >= MAX_ATTEMPTS:
                    raise FetchError(f"{type(e).__name__}: {e} (after {attempt} attempts)")
                db.ex("UPDATE psts SET error=?, updated_at=? WHERE id=? AND status='fetching'",
                      (f"retrying: {type(e).__name__}: {e}"[:300], time.time(), pid))
                if (stop or threading.Event()).wait(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]):
                    return  # worker stopping: the row stays 'fetching' and resumes at the next start
                _touch_progress(pid, os.path.getsize(dest) if os.path.exists(dest) else 0, None)
    except Cancelled:
        r = db.q1("SELECT status FROM psts WHERE id=?", (pid,))
        if r is None or r["status"] == "cancelling":
            _remove_row(pid)
    except FetchError as e:
        _fail(pid, str(e))
    except Exception as e:
        _fail(pid, f"{type(e).__name__}: {e}")


def resume_inbox(pid: int) -> None:
    row = db.q1("SELECT * FROM psts WHERE id=?", (pid,))
    if row is None or row["status"] != "fetching":
        return
    try:
        spec = db.jload(decrypt(row["fetch_spec"]), None) or {}
        move_into_place(pid, spec.get("origin", ""))
    except Cancelled:
        r = db.q1("SELECT status FROM psts WHERE id=?", (pid,))
        if r is None or r["status"] == "cancelling":
            _remove_row(pid)
    except Exception as e:
        _fail(pid, f"{type(e).__name__}: {e}")


# ====================================================================================== auto chain
def queue_job(aid: int) -> int | None:
    """Same effect as POST /api/runs for one account (None when a job is already active)."""
    if db.q1("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('queued','running','paused','stopping')", (aid,)):
        return None
    cur = db.ex("INSERT INTO jobs(account_id, status, phase, progress, created_at) "
                "VALUES (?, 'queued', 'precheck', ?, ?)", (aid, db.jdump(repo.empty_progress()), time.time()))
    repo.log(cur.lastrowid, "Job queued (auto chain)")
    return cur.lastrowid


def match_accounts(filename: str, domains: dict[str, str]) -> tuple[list, str]:
    """Accounts whose address matches the file name. Returns (rows, how)."""
    stem = stem_of(filename).strip().lower()
    if not stem:
        return [], "empty name"
    if "@" in stem:
        rows = db.q("SELECT * FROM accounts WHERE lower(email)=? OR lower(login)=?", (stem, stem))
        return list(rows), "address in file name"
    if not domains:
        return [], "no default domain configured"
    rows = []
    for sid, dom in domains.items():
        rows += db.q("SELECT * FROM accounts WHERE server_id=? AND lower(email)=?", (int(sid), f"{stem}@{dom}"))
    return rows, "default domain"


def chain_one(pst, cfg: dict) -> dict:
    if db.q1("SELECT id FROM accounts WHERE pst_id=?", (pst["id"],)):
        return {"result": "already_assigned"}
    rows, how = match_accounts(pst["filename"], cfg["default_domains"])
    if not rows:
        return {"result": "no_match", "detail": how}
    if len(rows) > 1:
        return {"result": "ambiguous", "account_ids": [r["id"] for r in rows]}
    a = rows[0]
    if a["pst_id"]:
        return {"result": "account_has_pst", "account_id": a["id"]}  # never overwrite
    cur = db.ex("UPDATE accounts SET pst_id=? WHERE id=? AND pst_id IS NULL", (pst["id"], a["id"]))
    if not cur.rowcount:
        return {"result": "account_has_pst", "account_id": a["id"]}
    out = {"result": "assigned", "account_id": a["id"], "started": False, "job_id": None}
    if cfg["start_import"]:
        if not a["password_enc"]:
            out["detail"] = "assigned, not started: no password on the account"
        else:
            jid = queue_job(a["id"])
            out.update(started=jid is not None, job_id=jid)
    return out


def run_chain() -> None:
    cfg = intake_settings.load()["auto_chain"]
    if not cfg["enabled"]:
        return
    for r in db.q("SELECT * FROM psts WHERE status='valid' AND updated_at>=? ORDER BY id", (cfg["enabled_at"],)):
        key = f"chain:{r['id']}"
        if db.kv_get(key) is not None:
            continue
        try:
            res = chain_one(r, cfg)
        except Exception as e:
            log.exception("auto chain PST %s", r["id"])
            res = {"result": "error", "detail": f"{type(e).__name__}: {e}"}
        db.kv_set(key, db.jdump(res))
        db.ex("UPDATE psts SET updated_at=? WHERE id=?", (time.time(), r["id"]))  # pushes the SSE event
        log.info("auto chain PST %s (%s): %s", r["id"], r["filename"], res)


# ====================================================================================== worker side
class Intake:
    """Driven by Worker.tick(): starts transfer threads, scans the drop folder, runs the auto chain."""

    scan_interval = 5.0

    def __init__(self, stop_event: threading.Event):
        self.stop = stop_event
        self.inbox = Inbox()
        self.threads: dict[str, threading.Thread] = {}
        self._scan_t = 0.0
        self._chain_t = 0.0

    def _spawn(self, key: str, fn, *args) -> None:
        t = threading.Thread(target=self._guard, args=(fn, *args), daemon=True, name=key)
        self.threads[key] = t
        t.start()

    @staticmethod
    def _guard(fn, *args) -> None:
        try:
            fn(*args)
        except Cancelled:
            pass
        except Exception:
            log.exception("intake %s", getattr(fn, "__name__", fn))

    def _free_slots(self) -> int:
        return settings.fetch_parallel - len(self.threads)

    def tick(self) -> None:
        for k, t in list(self.threads.items()):
            if not t.is_alive():
                del self.threads[k]
        # cancelled transfers without a running thread (worker restarted meanwhile)
        for r in db.q("SELECT id FROM psts WHERE status='cancelling'"):
            if f"pst-{r['id']}" not in self.threads:
                _remove_row(r["id"])
        # transfers to start or resume (also after a worker restart)
        for r in db.q("SELECT id, source FROM psts WHERE status='fetching' ORDER BY id"):
            key = f"pst-{r['id']}"
            if key in self.threads or self._free_slots() <= 0:
                continue
            if r["source"] == "inbox":
                self._spawn(key, resume_inbox, r["id"])
            elif r["source"] == "fetch":
                self._spawn(key, run_fetch, r["id"], self.stop)
        now = time.time()
        if now - self._scan_t >= self.scan_interval:
            self._scan_t = now
            self.scan_inbox()
        if now - self._chain_t >= 3.0:
            self._chain_t = now
            try:
                run_chain()
            except Exception:
                log.exception("auto chain")

    def scan_inbox(self) -> None:
        cfg = intake_settings.load()["inbox"]
        if not cfg["enabled"]:
            return
        try:
            settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("inbox %s unusable: %s", settings.inbox_dir, e)
            return
        for path in self.inbox.stable(cfg["settle_seconds"]):
            key = f"inbox-{path}"
            if key in self.threads or self._free_slots() <= 0:
                continue
            self._spawn(key, self._register, path)

    def _register(self, path: str) -> None:
        try:
            pid = register_inbox_file(path)
            failed = pid is not None and (db.q1("SELECT status FROM psts WHERE id=?", (pid,)) or {"status": ""})[
                "status"] == "fetch_failed"
            if pid is None or failed:  # duplicate, or failed (retry through the API): do not loop on it
                self.inbox.ignore(path)
        except Cancelled:
            pass
        except Exception as e:
            log.warning("inbox: %s not registered: %s", path, e)
            self.inbox.ignore(path)

    def join(self, timeout: float = 5.0) -> None:
        for t in list(self.threads.values()):
            t.join(timeout)
