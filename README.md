# PST → IMAP / CalDAV / CardDAV

Web tool (Docker) to mass-migrate Outlook/POP mailboxes (PST files) to an IMAP server
(mails), CalDAV (appointments) and CardDAV (contacts). Migrations run mailbox by mailbox, with
checks at every step. You can pause, stop and resume without creating
duplicates. Full specification: `docs/SPEC.md`.

## Table of contents

1. [Quick start](#quick-start)
   - [Build from source](#build-from-source)
2. [Admin password](#admin-password)
3. [Environment variables](#environment-variables)
4. [Account import CSV format](#account-import-csv-format)
5. [Workflow](#workflow)
6. [How resuming works](#how-resuming-works)
7. [General behavior](#general-behavior)
8. [Behind a reverse proxy](#behind-a-reverse-proxy)
9. [Known limitations](#known-limitations)
10. [Tests](#tests)

## Quick start

A prebuilt image is published at `ghcr.io/yetysw/psttoimap` (linux/amd64). No configuration is
required: no `.env`, no build.

```bash
mkdir psttoimap && cd psttoimap
curl -fsSLO https://raw.githubusercontent.com/yetysw/psttoimap/main/docker-compose.yml
docker compose up -d
docker compose logs web | grep -A2 "Admin password"
```

Then open **http://<host>:22362** and sign in with the printed password. You can change it later in
the UI under **Settings**.

### Build from source

```bash
git clone https://github.com/yetysw/psttoimap.git
cd psttoimap
docker compose up -d --build
docker compose logs web      # read the generated admin password
curl http://localhost:22362/api/health
```

The `.env` file is **optional** in both cases.

On first start, a random admin password is generated and printed once in the `web` container log as a
boxed banner containing `Admin password: XXXX`. To use your own settings instead, copy `.env.example`
to `.env` and edit it (`ADMIN_PASSWORD` and `SECRET_KEY` are both optional).

Generate a `SECRET_KEY`:

```bash
openssl rand -base64 32
# or: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The UI is served at **http://localhost:22362/** (or `http://<host>:22362/`). `GET /api/health`
is public and also reports whether the worker is alive (heartbeat less than 60 s old).

The compose file runs two services with the same image:

| service  | role |
|----------|------|
| `web`    | FastAPI API + static UI (`app/web/`), port 22362 |
| `worker` | job queue (one mailbox = one job) and validation of uploaded PSTs |

All data lives in `./data` (mounted on `/data`):

- `app.db`: SQLite database (WAL);
- `pst/`: PST files;
- `reports/job-N.json`: reports;
- `logs/job-N.log`: logs;
- `admin.json`: hashed admin password, generated on first start if `ADMIN_PASSWORD` is not set;
- `secret.key`: key generated if `SECRET_KEY` is empty. **Back it up**: without it (or without the same
  `SECRET_KEY`), stored mailbox passwords can no longer be decrypted.

If `SECRET_KEY` is set, it takes precedence over `data/secret.key`. Changing it makes already stored
mailbox passwords undecryptable (they must be re-entered) and invalidates sessions.

## Admin password

- **`ADMIN_PASSWORD` empty or absent (default):** a random password is generated on first start, stored
  hashed in `data/admin.json` and printed once in the `web` container log (`docker compose logs web`) as a
  boxed banner containing `Admin password: XXXX`.
- **`ADMIN_PASSWORD` set:** the environment variable always wins over `data/admin.json`.

To change it:

- from the UI (**Settings** page, "Change admin password") or via `POST /api/password {current, new}` (minimum 8 characters). This answers 409 if
  `ADMIN_PASSWORD` is set in the environment. Other sessions are invalidated and the caller gets a fresh
  cookie;
- to reset a lost password: delete `data/admin.json` and restart (`docker compose restart web`); a new
  password is generated and printed in the log. Or set `ADMIN_PASSWORD` in `.env` and recreate the
  containers with `docker compose up -d` (a plain `restart` does not re-read `env_file`).

Changing the password **invalidates** existing sessions (the session cookie embeds a fingerprint of the
admin password): you must log in again with the new password.

After 5 failed logins within 5 minutes from the same IP, `POST /api/login` answers 429. Each failure
is delayed by 0.5 s. The counter lives in the memory of the `web` process: a restart resets it.

## Environment variables

They are defined in `.env` (read by the `web` and `worker` services). The file itself is optional.

| variable | default | role |
|----------|---------|------|
| `ADMIN_PASSWORD` | *(empty)* | UI password. **Optional**: if empty, a random password is generated on first start (see [Admin password](#admin-password)). When set, it always wins. |
| `SECRET_KEY` | *(empty)* | Key used to encrypt mailbox passwords and sign sessions. Empty: generated in `data/secret.key`. |
| `UPLOAD_CHUNK_SIZE` | `33554432` (32 MiB) | Upload chunk size, in bytes (16 to 64 MiB recommended). **Do not change it during a migration**, see [resuming](#how-resuming-works). |
| `UPLOAD_DISK_MARGIN` | `536870912` (512 MiB) | Free disk space margin required on top of the PST size. |
| `WORKER_CONCURRENCY` | `2` | Number of mailboxes migrated simultaneously by the worker. |
| `VERIFY_SAMPLE` | `20` | Number of mails re-read for the sample check at the end of a job. |
| `IMAP_MAX_RETRIES` | `5` | Attempts on temporary IMAP errors (exponential backoff). |
| `MAX_CHILD_CRASHES` | `3` | Subprocess (libpff) crashes tolerated per job before it is marked `failed` (minimum 1). |
| `CHILD_TERM_GRACE` | `20` | Seconds given to subprocesses to stop on SIGTERM before SIGKILL. |
| `COOKIE_SECURE` | `0` | `1` = `Secure` session cookie (HTTPS behind a reverse proxy). The cookie is also `Secure` if the request arrives over HTTPS. |
| `SESSION_MAX_AGE` | `604800` (7 days) | Session lifetime, in seconds. |
| `WORKER_STALE_SECONDS` | `60` | A `running` job whose heartbeat is older is considered orphaned and requeued. |
| `PORT` | `22362` | Listening port of the `web` service inside the container (the compose file publishes `22362`). |
| `DATA_DIR` | `/data` | Data directory (set by the compose file). |
| `PSTMIG_ENGINE` | `app.engine` | Engine module; used only by tests. |

`.env.example` only lists the common variables; `SESSION_MAX_AGE`, `WORKER_STALE_SECONDS`,
`CHILD_TERM_GRACE` and `PORT` are read by `app/server/config.py` and are optional.

## Account import CSV format

Import through the UI (Accounts page) or `POST /api/accounts/import-csv` (multipart: `file` +
`server_id`). A template can be downloaded via `GET /api/accounts/csv-template`.

| column | required | content |
|--------|----------|---------|
| `email` | yes | mailbox address (must look like `x@domain.tld`) |
| `password` | yes | mailbox password (non-empty) |
| `login` | no | IMAP/DAV login if it differs from the address; empty = the address |

- **Separator**: `;` or `,`, detected on the first non-empty line.
- **Header**: optional. Recognized if the first line contains names such as `email`, `e-mail`,
  `mail`, `adresse`, `courriel`, `password`, `mot de passe`, `mdp`, `pass`, `login`, `identifiant`;
  columns are then located by name. Without a header, the order is `email;password;login`.
- **Encoding**: UTF-8 (with or without BOM), UTF-16 (with BOM) or, failing that, cp1252.
- Empty lines are ignored. Invalid lines (invalid address, missing password) are
  rejected one by one and reported with their line number; the others are imported.
- Response: `{created, updated, errors:[{line,msg}]}` (an address already present on that server is
  updated).

Example:

```csv
email;password;login
jean.dupont@example.com;Password1;
marie.martin@example.com;S3cret;mmartin
```

A password containing the separator must be enclosed in quotes (`"a;b"`).

## Workflow

1. **Servers.** Create a server: IMAP host and port, security (`ssl`, `starttls`, `none`), TLS
   verification (can be disabled), optional root folder (`root_folder`, e.g. `PST Import`), CalDAV/CardDAV URL
   (empty = auto-discovery, see [DAV](#dav)), number of parallel connections (`workers`). A test button
   checks the connection.
2. **Accounts.** Add mailboxes one by one or via [CSV](#account-import-csv-format). The IMAP login is
   tested when added, then again before the run (`precheck`). For each account you can enable or
   disable mail, calendar and contacts.
3. **PST upload.** Drag and drop the file onto the Uploads page. It is split into chunks
   (32 MiB by default), sent 3 in parallel with automatic retry. Each chunk is verified by
   SHA-256 server-side. The upload is **resumable**: after a refresh or a network drop, sending
   the same file again (key `name + size + modification date`) resumes at the missing chunks. Disk
   space is checked before the upload (507 if there is not enough room).
4. **Validation / scan.** When the upload finishes, the server checks the size, then the worker verifies the
   `!BDN` signature and analyzes the whole PST (counting mails, appointments and contacts per folder)
   in a subprocess. The PST becomes `valid` or `invalid` (with the reason).
5. **Preview (.zip).** On a `valid` PST, download `GET /api/psts/{id}/preview?limit=20` (limit from 1 to
   200): a zip of an evenly spread sample of mails (`.eml`), appointments (`.ics`) and contacts (`.vcf`),
   organized by folder. Open it **before launching** to check accents (subjects, bodies, folder
   names), attachment names and types, and embedded images. Generated in a
   subprocess, 120 s maximum; 409 error if the PST is not valid, 500 if libpff crashes.
6. **Association.** Attach a valid PST to each account.
7. **Runs.** Select accounts then "Run" (`POST /api/runs`): one job per mailbox, queued.
   The worker runs `WORKER_CONCURRENCY` at a time. Phases: `precheck` (IMAP, DAV, quota), `mail`,
   `calendar`, `contacts`, `verify`.
8. **Pause / resume / stop.** From the job detail: pause, resume, stop, full retry, or
   "retry errors". Commands take effect between two items.
9. **Monitoring and report.** Progress per phase, current folder, rate, ETA, live log (SSE),
   error list. At the end of the job, a JSON report (`GET /api/jobs/{id}/report`, also in
   `data/reports/`): sent, present, errors, warnings, skipped. A job is `done` if everything
   passed, `done_with_errors` otherwise (errors, failed verification or unfinished items).

## How resuming works

**Item states.** Each mail, contact or appointment has a row in the `items` table:
`pending` → `inflight` (marked just before the APPEND) → `ok` or `error` (or `skipped`).

**`X-PST-Import-Id` marker.** Each imported mail carries the header `X-PST-Import-Id: <pst_sha-item_key>`.
The value is delimited by `<` and `>`: no identifier is a prefix of another. On resume:

- `ok` items are skipped;
- `inflight` items (and `error` items already attempted) are first looked up on the server
  (`UID SEARCH HEADER X-PST-Import-Id`, then re-reading the header and an **exact comparison**); the mail
  is resent only if absent. A temporary error during an APPEND (drop after sending) follows
  the same rule: reconnect, exact search, resend only if absent;
- on the DAV side, iCal/vCard UIDs are deterministic and the `PUT` overwrites the existing resource.

A job is never `done` if `pending` or `inflight` items remain.

**Re-uploading the same PST.** Items are tied to the PST fingerprint (`pst_sha`, computed from the
SHA-256 of its chunks and its size), not to its internal id. Deleting a PST then re-uploading
the same file therefore recovers the already migrated state, without duplicates. **This fingerprint depends on
`UPLOAD_CHUNK_SIZE`**: with a different chunk size, the same file has a different fingerprint and would be
treated as a new PST. Keep the value constant for the whole migration of a batch.

**Pause, stop.** APPENDs not yet started are cancelled (back to `pending`), those in progress run to completion.

**Worker restart or brutal stop.** On `SIGTERM`, subprocesses receive SIGTERM
(SIGKILL after `CHILD_TERM_GRACE` s) and jobs go back to the queue. A job left `running` while its
worker is dead (kill, crash, container restart, heartbeat older than `WORKER_STALE_SECONDS`)
is requeued automatically when the worker starts, then every 15 s. A brutal stop is
not counted as a crash.

**libpff crash isolation.** Each job and each PST analysis runs in a subprocess.
Before reading an item, it records the current item in the database. If it dies (SIGSEGV, code ≠ 0):

- the faulty item is marked `error` "libpff crash" (never read again, except by "retry
  errors"); a crash during iteration skips the index concerned;
- the job is requeued and resumes after that item;
- after `MAX_CHILD_CRASHES` crashes (default 3) of the same job, it becomes `failed`
  ("The subprocess crashed N times");
- a crash during analysis makes the PST `invalid` ("libpff crash during analysis").

**Retry errors.** `POST /api/jobs/{id}/retry-errors` only reprocesses `error` items
(with a prior check on the server to avoid duplicating). "Retry" (`retry`) does a full
pass again, skipping what is already `ok`.

## General behavior

- **Authentication.** A single admin password (`ADMIN_PASSWORD`, or the generated one stored hashed in
  `data/admin.json`). The session is a signed cookie (HttpOnly, SameSite=Lax). Mailbox passwords are
  encrypted with Fernet (key derived from `SECRET_KEY`).
- **Upload.** The file is pre-allocated to its final size (`fallocate`). Each chunk is read as a stream,
  written at its offset, then its SHA-256 is compared to the `X-Chunk-Sha256` header (422 if different). The
  list of received chunks is stored in the database. `complete` answers 409 if chunks are missing.
- **IMAP folders.** Names in modified UTF-7, server separator, NAMESPACE prefix honored. Sent,
  Drafts, Trash and Junk are mapped to the server's SPECIAL-USE folders (or found
  by name). With `root_folder`, the whole PST is imported beneath it, with no special mapping.
- **Sending.** Parallel APPEND over N connections (server `workers`), reconnect and exponential
  backoff on temporary errors (`IMAP_MAX_RETRIES`). Permanent errors are recorded per
  item. Exceeding the quota fails the job.
- **Checks.** After each folder, `STATUS MESSAGES` must be at least equal to the number of mails
  sent. At the end of the job, `VERIFY_SAMPLE` mails are re-read by header and compared by hash. DAV
  resources are counted.
- **Real time.** `GET /api/events` (SSE) relays changes, detected by SQLite triggers
  (the worker is another process).

### DAV

`caldav_url` / `carddav_url` fields of the server:

- empty: auto-discovery via `https://<IMAP host>/.well-known/caldav|carddav`, then via the domain
  of the address;
- otherwise: template URL (`{email}`, `{user}` = local part, `{domain}`, `{login}`). It can
  point directly to a collection, or to an entry point from which we go up to the principal then the
  home-set.

The first VEVENT calendar or address book found is used. If there is none, an
"PST Import" collection is created (MKCALENDAR or extended MKCOL).

## Behind a reverse proxy

The `web` service listens on plain HTTP on 22362 and trusts `X-Forwarded-*` headers
(uvicorn `proxy_headers`, all proxies accepted): the client IP for login rate limiting and the
HTTPS scheme for the cookie. **Do not expose it directly to the Internet in addition to the proxy**: filter
port 22362 (for example `127.0.0.1:22362:22362` in the compose file) so that only the proxy can reach it.

Essential points:

- **No request buffering**: the 32 MiB chunks are streamed to the application;
  otherwise the proxy first stores them on disk (slow, and consumes space).
- **No response buffering** for `/api/events` (SSE), otherwise updates do not arrive.
- **Max body size** greater than or equal to `UPLOAD_CHUNK_SIZE` (32 MiB by default: 64 MiB leaves
  headroom).
- **Generous timeouts** (e.g. 3600 s): the SSE stream stays open, and the preview or a slow chunk can take a while.
- **`COOKIE_SECURE=1`** over HTTPS, and forward `X-Forwarded-For` / `X-Forwarded-Proto` / `Host`.

nginx example:

```nginx
server {
    listen 443 ssl http2;
    server_name migration.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;

    client_max_body_size 64m;          # >= UPLOAD_CHUNK_SIZE
    proxy_request_buffering off;       # chunks streamed, not stored
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:22362;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/events {             # SSE
        proxy_pass http://127.0.0.1:22362;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

Caddy: `reverse_proxy 127.0.0.1:22362` works almost as is (automatic HTTPS, no body limit by
default, `X-Forwarded-*` headers added). For SSE, add `flush_interval -1` inside the
`reverse_proxy` block, and do not reduce the timeouts.
Traefik: no body limit by default (do not enable the `buffering` middleware with
a too-low `maxRequestBodyBytes` or a `memRequestBodyBytes` that buffers), increase if needed the
`respondingTimeouts` (`readTimeout`, `writeTimeout`, `idleTimeout`) of the entrypoint, and forward the
`X-Forwarded-*` headers (enabled by default).

## Known limitations

- **A single administrator**: one shared password, no accounts or roles.
- **SQLite** (WAL) on the `./data` volume: a single host, no high availability. Web and worker
  share the same file, which must stay on a local disk (no NFS).
- **PST fingerprint tied to `UPLOAD_CHUNK_SIZE`**: changing this value after a first import
  loses recognition of the same file (see [resuming](#how-resuming-works)), with a risk
  of duplicates if the PST is re-uploaded.
- **libpff**: a corrupted PST can crash the native library. The affected items are
  skipped ("libpff crash" error); beyond `MAX_CHILD_CRASHES` crashes, the job fails. The counter
  is **per job** and reset at the end of the job. A badly damaged PST can be rejected at analysis.
- **Encrypted / password-protected PSTs**: no dedicated handling in the code; a PST whose
  content is encrypted will give errors or be `invalid`. Do not rely on it.
- **Depends on the quality of the target server**: APPEND, SPECIAL-USE, quota (`GETQUOTAROOT` if available)
  and DAV discovery vary between servers.
- **No Swagger UI**: `/docs`, `/redoc` and `/openapi.json` are disabled (the API reference
  is in `docs/SPEC.md`).
- **In-memory login rate limiting**: 5 failures / 5 min per IP, reset when `web` restarts.
  Behind a proxy, check that `X-Forwarded-For` is forwarded, otherwise all requests
  share the proxy's IP.
- **Mailbox passwords are stored encrypted** but can be decrypted by the application: protect
  `./data` and the `SECRET_KEY`.
- **UI in French only.** JS libs are vendored (works offline), but the
  browser must support modern APIs (fetch, SSE, `File.slice`).

## Tests

Tests run in containers: GreenMail (IMAP), Radicale (CalDAV/CardDAV) and the
application image with pytest (engine, server and e2e suites). They use a fake engine
(`tests/server/fake_engine.py`) that honors the ENGINE contract for server tests.

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v      # cleanup
```

Unit test of the web code (chunked sending), with nothing to install:

```bash
docker run --rm -v "$PWD":/w -w /w node:22-slim node --test 'tests/web/*.test.mjs'
```

Coverage: CRUD, authentication, SSE, CSV import (`;` and `,`, optional header, UTF-8, cp1252, BOM),
resumable upload (chunk with wrong hash → 422, resume, identical final file, disk space
check), full import, root folder, pause and resume, stop then retry without duplicates, simulated
crash with `inflight` items and an orphaned job, simulated libpff crash (`PSTMIG_TEST_CRASH`),
retry of errors only. Without `TEST_IMAP_HOST` or `TEST_DAV_URL`, integration tests are skipped.

## License

MIT, see [LICENSE](LICENSE).
