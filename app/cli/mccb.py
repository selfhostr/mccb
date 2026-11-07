#!/usr/bin/env python3
"""mccb: command line for the mccb API. Standard library only, one file: copy it to any machine with Python 3.8+
(a NAS, a server) and push PST files or start migrations from there.

    export MCCB_URL=https://mccb.example.org MCCB_TOKEN=mccb_xxxxxxxx_...
    mccb status
    mccb jobs --active
    mccb run 12 13 14 --name "wave 1" --watch
    mccb watch 57
    mccb upload /volume1/mail/jane.pst --account 12 --wait
    mccb plan 12 13            # dry run

The token is created in the web interface (Settings) or with POST /api/tokens, and shown once. Exit codes of
`watch` and `run --watch`: 0 done, 1 done with errors, 2 failed, 3 stopped, 130 interrupted. Other commands:
0 on success, 4 on an API or network error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:22362"
FINAL = {"done": 0, "done_with_errors": 1, "failed": 2, "stopped": 3}
RETRIES = 6


class CliError(Exception):
    pass


class Api:
    def __init__(self, url: str, token: str, insecure: bool = False, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def call(self, method: str, path: str, body=None, data: bytes | None = None, headers: dict | None = None):
        """JSON in and out (or raw bytes in with `data`). Raises CliError with the server's message."""
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        payload = data
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        elif data is not None:
            h["Content-Type"] = "application/octet-stream"
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, data=payload, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", "replace")).get("detail", "")
            except Exception:  # noqa: BLE001
                pass
            if isinstance(detail, (dict, list)):
                detail = json.dumps(detail)
            err = CliError(f"{method} {path}: HTTP {e.code} {detail}".strip())
            err.code = e.code  # type: ignore[attr-defined]
            raise err from None
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            err = CliError(f"{method} {path}: {getattr(e, 'reason', e)}")
            err.code = 0  # type: ignore[attr-defined]
            raise err from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")


def human(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def progress_line(job: dict) -> str:
    parts = []
    for k, p in (job.get("progress") or {}).items():
        if p.get("total"):
            parts.append(f"{k} {p.get('done', 0)}/{p['total']}" + (f" ({p['errors']} errors)" if p.get("errors") else ""))
    extra = ""
    if job.get("rate"):
        extra = f", {job['rate']:.1f} items/s"
        if job.get("rate_bytes"):
            extra += f", {human(job['rate_bytes'])}/s"
    state = job["status"] + (f" ({job['pause_reason']})" if job.get("pause_reason") else "")
    return f"job {job['id']}: {state}, {job.get('phase')}: " + (", ".join(parts) or "no items yet") + extra


# ------------------------------------------------------------------ commands
def cmd_status(api: Api, a) -> int:
    h = api.call("GET", "/api/health")
    w = h.get("worker", {})
    print(f"server {api.url}: version {h.get('version')}, worker {'alive' if w.get('alive') else 'NOT seen'}")
    jobs = api.call("GET", "/api/jobs?active=1&limit=500")
    by: dict[str, int] = {}
    for j in jobs:
        by[j["status"]] = by.get(j["status"], 0) + 1
    print("active jobs: " + (", ".join(f"{n} {s}" for s, n in sorted(by.items())) or "none"))
    return 0


def cmd_jobs(api: Api, a) -> int:
    jobs = api.call("GET", f"/api/jobs?limit={a.limit}" + ("&active=1" if a.active else ""))
    if a.json:
        print(json.dumps(jobs))
        return 0
    for j in reversed(jobs):
        print(progress_line(j))
    if not jobs:
        print("no jobs")
    return 0


def cmd_accounts(api: Api, a) -> int:
    rows = api.call("GET", "/api/accounts" + (f"?server_id={a.server}" if a.server else ""))
    for r in rows:
        job = r.get("job") or {}
        print(f"{r['id']}\t{r['email']}\t{r['status']}\t{job.get('status', '-')}")
    return 0


def watch(api: Api, jid: int, interval: float = 2.0, logs: bool = False) -> int:
    last, after = "", 0
    try:
        while True:
            job = api.call("GET", f"/api/jobs/{jid}")
            line = progress_line(job)
            if logs:
                got = api.call("GET", f"/api/jobs/{jid}/log?after={after}&limit=500")
                for x in got["lines"]:
                    print(f"  [{x['level']}] {x['msg']}")
                after = got["next"]
            if line != last:
                print(line, flush=True)
                last = line
            if job["status"] in FINAL:
                if job.get("error"):
                    print(f"error: {job['error']}", file=sys.stderr)
                return FINAL[job["status"]]
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130


def cmd_watch(api: Api, a) -> int:
    return watch(api, a.job, a.interval, a.logs)


def cmd_run(api: Api, a) -> int:
    body = {"account_ids": a.accounts, "mode": "delta" if a.delta else "full", "priority": a.priority}
    if a.name:
        body["name"] = a.name
    jobs = api.call("POST", "/api/runs", body)
    for j in jobs:
        print(f"job {j['id']} for mailbox {j['account_id']}: {j['status']}")
    if not a.watch:
        return 0
    worst = 0
    for j in jobs:
        worst = max(worst, watch(api, j["id"], a.interval))
    return worst


def cmd_plan(api: Api, a) -> int:
    p = api.call("POST", "/api/runs/plan", {"account_ids": a.accounts})
    while p["status"] in ("queued", "running"):
        time.sleep(1.0)
        p = api.call("GET", f"/api/plans/{p['id']}")
    if a.json:
        print(json.dumps(p))
        return 0 if p["status"] == "done" else 4
    if p["status"] != "done":
        print(f"plan {p['id']}: {p['status']} {p.get('error') or ''}", file=sys.stderr)
        return 4
    for acc in p["result"]["accounts"]:
        print(f"{acc.get('email') or acc['account_id']}: {acc['status']}")
        for kind, k in (acc.get("kinds") or {}).items():
            print(f"  {kind}: {k['to_migrate']} to migrate of {k['items']} ({human(k['to_migrate_bytes'])}), "
                  f"{sum(k['filtered'].values())} filtered, {k['oversized']} over the target limit, "
                  f"{k['folders_to_create']} folder(s) to create")
        for w in acc.get("warnings", []):
            print(f"  warning: {w}")
        if acc.get("error"):
            print(f"  error: {acc['error']}")
    t = p["result"]["totals"]
    print(f"total: {t['to_migrate']} items, {human(t['to_migrate_bytes'])}, {t['ok']} ok, {t['warning']} with "
          f"warnings, {t['error']} in error")
    return 0 if not t["error"] else 4


def chunk_sha(path: str, n: int, size: int) -> tuple[bytes, str]:
    with open(path, "rb") as f:
        f.seek(n * size)
        data = f.read(size)
    return data, hashlib.sha256(data).hexdigest()


def cmd_upload(api: Api, a) -> int:
    """Resumable: the same file (name, size, modification time) continues the same upload, whoever sends it."""
    path = a.file
    st = os.stat(path)
    up = api.call("POST", "/api/uploads", {"filename": os.path.basename(path), "size": st.st_size,
                                           "last_modified": int(st.st_mtime * 1000)})
    pid, csize = up["id"], up["chunk_size"]
    total = up.get("chunks") or -(-st.st_size // csize)
    have = set(up.get("received", []))
    if up.get("status") != "uploading":
        print(f"already on the server as PST {pid} ({up.get('status')}): nothing to send")
    else:
        print(f"PST {pid}: {len(have)}/{total} chunk(s) already there" if have else f"PST {pid}: {total} chunk(s)")
        t0, sent = time.time(), 0
        for n in range(total):
            if n in have:
                continue
            data, sha = chunk_sha(path, n, csize)
            for attempt in range(RETRIES):
                try:
                    api.call("PUT", f"/api/uploads/{pid}/chunks/{n}", data=data, headers={"X-Chunk-Sha256": sha})
                    break
                except CliError as e:
                    if attempt == RETRIES - 1 or getattr(e, "code", 0) in (400, 401, 403, 404, 507):
                        raise
                    wait = min(30.0, 2.0 ** attempt)
                    print(f"  chunk {n}: {e}; retrying in {wait:.0f} s", file=sys.stderr)
                    time.sleep(wait)
            sent += len(data)
            print(f"  chunk {n + 1}/{total}  {human(sent / max(time.time() - t0, 0.001))}/s", end="\r", flush=True)
        print()
        api.call("POST", f"/api/uploads/{pid}/complete")
        print(f"PST {pid}: upload complete, analysis started")
    if a.wait or a.account:
        while True:
            pst = api.call("GET", f"/api/psts/{pid}")
            if pst["status"] in ("valid", "invalid"):
                break
            time.sleep(2.0)
        print(f"PST {pid}: {pst['status']}" + (f" ({pst['error']})" if pst.get("error") else ""))
        if pst["status"] != "valid":
            return 4
    if a.account:
        api.call("PUT", f"/api/accounts/{a.account}/pst", {"pst_id": pid})
        print(f"PST {pid} linked to mailbox {a.account}")
    return 0


# ------------------------------------------------------------------ entry point
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mccb", description="Command line for the mccb API.")
    p.add_argument("--url", default=os.environ.get("MCCB_URL", DEFAULT_URL), help="server URL (MCCB_URL)")
    p.add_argument("--token", default=os.environ.get("MCCB_TOKEN", ""), help="API token (MCCB_TOKEN)")
    p.add_argument("--token-file", help="file holding the token (keeps it out of the process list)")
    p.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="server, worker and active jobs").set_defaults(fn=cmd_status)
    j = sub.add_parser("jobs", help="list jobs")
    j.add_argument("--active", action="store_true")
    j.add_argument("--limit", type=int, default=50)
    j.add_argument("--json", action="store_true")
    j.set_defaults(fn=cmd_jobs)
    ac = sub.add_parser("accounts", help="list mailboxes")
    ac.add_argument("--server", type=int)
    ac.set_defaults(fn=cmd_accounts)
    r = sub.add_parser("run", help="start migrations")
    r.add_argument("accounts", type=int, nargs="+", metavar="ACCOUNT_ID")
    r.add_argument("--name", help="name of the batch these jobs form")
    r.add_argument("--delta", action="store_true", help="final resync: only what arrived since the previous pass")
    r.add_argument("--priority", type=int, default=0)
    r.add_argument("--watch", action="store_true", help="wait for the jobs; exit code of the worst one")
    r.add_argument("--interval", type=float, default=2.0)
    r.set_defaults(fn=cmd_run)
    w = sub.add_parser("watch", help="follow a job until it ends")
    w.add_argument("job", type=int, metavar="JOB_ID")
    w.add_argument("--interval", type=float, default=2.0)
    w.add_argument("--logs", action="store_true", help="also print the job log")
    w.set_defaults(fn=cmd_watch)
    pl = sub.add_parser("plan", help="dry run: counts, sizes, quota, folders to create")
    pl.add_argument("accounts", type=int, nargs="+", metavar="ACCOUNT_ID")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(fn=cmd_plan)
    u = sub.add_parser("upload", help="send a PST file (resumable); run it again to continue")
    u.add_argument("file")
    u.add_argument("--account", type=int, help="link the PST to this mailbox once it is valid")
    u.add_argument("--wait", action="store_true", help="wait for the analysis")
    u.set_defaults(fn=cmd_upload)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    token = a.token
    if a.token_file:
        with open(a.token_file, encoding="utf-8") as f:
            token = f.read().strip()
    if not token:
        print("mccb: no token (--token, --token-file or MCCB_TOKEN)", file=sys.stderr)
        return 4
    try:
        return a.fn(Api(a.url, token, a.insecure), a)
    except CliError as e:
        print(f"mccb: {e}", file=sys.stderr)
        return 4
    except OSError as e:
        print(f"mccb: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
