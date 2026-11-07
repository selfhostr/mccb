# Specification

Design notes and contracts of mccb: purpose, checks, resume logic, architecture and the engine contract. The HTTP API is in [API](api.md).

## Purpose
Web tool (Docker, port **22362**) to mass-migrate POP/Outlook clients to our servers:
for each mailbox, we provide the address + password + the client's PST; a "Run" imports
**mails (IMAP)**, **appointments (CalDAV)**, **contacts (CardDAV)**, mailbox by mailbox, with
checks everywhere, and pause/stop/resume without duplicates. Modern web UI.

## Non-goals (failure even if "it works")
- Broken accents (U+FFFD, "Ã©" mojibake) in subject, text body, HTML body, attachment names, folder names.
- Attachments renamed `piece_jointe_N`, typed `application/octet-stream` while the type is known, truncated, or signature images shown as attachments instead of being embedded (cid).
- Duplicates after a resume. Crash on a 50 GB PST. A 50 GB upload that fails without being able to resume.

## Lessons from the old `mig.py` script (do NOT reproduce)
1. `html_body` was decoded as UTF-8 while it is in the message's codepage → U+FFFD.
   Correct: read `PR_INTERNET_CPID` (0x3FDE) / `PR_MESSAGE_CODEPAGE` (0x3FFD), decode with that codepage
   (fallback: charset of the HTML `<meta>`, then cp1252); MAPI PT_UNICODE (0x001F) strings are UTF-16-LE,
   PT_STRING8 (0x001E) are in the codepage. Emit everything as UTF-8 (`charset=utf-8`, QP/base64).
2. If there is no HTML or text: decompress `PR_RTF_COMPRESSED` (0x1009); if the RTF encapsulates HTML
   (`\fromhtml1`) → extract the HTML; otherwise convert RTF → text.
3. `a.name` does not exist in pypff → names lost. Correct: `PR_ATTACH_LONG_FILENAME` 0x3707,
   fallback `PR_ATTACH_FILENAME` 0x3704, `PR_DISPLAY_NAME` 0x3001; MIME `PR_ATTACH_MIME_TAG` 0x370E;
   `PR_ATTACH_CONTENT_ID` 0x3712; `PR_ATTACH_FLAGS` 0x3714 / `PR_ATTACHMENT_HIDDEN` 0x7FFE;
   `PR_ATTACH_METHOD` 0x3705 (5 = embedded message → attach it as a recursively built `message/rfc822`).
   Missing type → content-based detection (see `detecte()` in the repair script) + `mimetypes` by extension.
   Images referenced by `cid:` in the HTML → inline `multipart/related` with Content-ID.
   Non-ASCII file names encoded per RFC 2231 (handled by `email.message.EmailMessage`).
4. Original transport headers: keep them (From, To, Cc, Date, Message-ID, In-Reply-To, References,
   Subject…) but **decode/re-encode them properly** (RFC 2047); if missing (POP mails without headers,
   sent mails), rebuild from MAPI: subject, sender name + address (`PR_SENDER_EMAIL_ADDRESS`,
   SMTP if EX via `PR_SENDER_SMTP_ADDRESS` 0x5D01), recipients from the recipient table, dates.
5. IMAP flags: `\Seen` if `PR_MESSAGE_FLAGS` & 1; `\Flagged` if follow-up flagged; `\Answered` if replied; drafts `\Draft`.
6. Folders: IMAP modified UTF-7 names; honor the server's separator; map special folders
   (Inbox→INBOX, Sent Items→the server's \Sent folder via SPECIAL-USE, etc.); option
   "import under a root folder" (e.g. `PST Import`). Non-mail folders (contacts, calendar, tasks…)
   detected by `PR_CONTAINER_CLASS` (IPF.Note / IPF.Contact / IPF.Appointment / IPF.Task…), not by name.

## Mandatory checks
- **Upload**: SHA-256 of each chunk verified server-side; final size; PST signature `!BDN`;
  pypff open + full scan (count of mails/contacts/appointments per folder) → "valid PST" status.
- **Account**: IMAP login (test at creation + before run), DAV access, quota (GETQUOTAROOT if available) vs estimated size.
- **Every built mail**: MIME re-parse; no U+FFFD added by us; attachment count + size = source;
  every `cid:` in the HTML resolved; if rich construction fails → simplified fallback **always UTF-8 with named attachments**, flagged as a warning.
- **After each folder**: server `STATUS MESSAGES` ≥ sent; **after each mailbox**: re-read a sample
  (N mails) by `X-PST-Import-Id` header and compare the hash; DAV: resource count.
- **Report** per mailbox (JSON + display): sent / present / errors / warnings / skipped, downloadable.

## Idempotence / resume
Each item has a stable `item_key` = pypff node identifier (`msg.identifier`) + folder (fallback key
`<fnid>-x<index>` if the item is unreadable during iteration: on re-run, if it becomes readable, it is processed under its
real key and the fallback row is deleted). Each mail carries the header `X-PST-Import-Id: <pst_sha-item_key>`
(value **delimited by `<>`**: no id is a prefix of another). `items` table (account_id, **pst_key**, kind,
item_key, status pending|inflight|ok|error|skipped, error, …): `pst_key` = `psts.pst_sha` (block fingerprint +
size), **not the PST id**, deleting then re-uploading the same file recovers the state (automatic migration of
old `pst_id` rows at startup; `kv` `pst:<pst_sha>:crash_skips`). Folder key: path joined by `/`,
`/` and `\` in names escaped; unique target IMAP names (deterministic suffix ` (2)`, ` (3)`… on collision
after sanitizing). Mail lookup: `UID SEARCH HEADER X-PST-Import-Id <sha-key>` (substring) then
`UID FETCH BODY.PEEK[HEADER.FIELDS (X-PST-Import-Id)]` and **exact comparison** (the historical non-delimited form
is also recognized). On resume: `ok` skipped; `inflight` and `error` already attempted (known hash) → exact search
before any resend. A temporary error during an APPEND (timeout, drop after the literal is sent) → reconnect +
exact search, resend only if absent. Pause/stop: APPENDs not yet started are cancelled (`pending`), those in
progress complete, all threads are joined. A job is never `done` if `pending`/`inflight` items remain
(→ `done_with_errors` "unfinished"). A refused CREATE only errors the items of
that folder. "Present ≥ sent" check: only our messages count (SEARCH for `<pst_sha>-` per folder; DAV:
resource names = our UIDs). DAV: deterministic iCal/vCard UIDs → idempotent `PUT`.
**Native crash (libpff)**: before each libpff read of an item, the job's subprocess commits an
"item in progress" marker (`kv` `job:<id>:current`: folder, index, item_key). If the subprocess dies (signal, code ≠ 0),
the worker marks that item `error` "libpff crash" (never an `inflight`, always resolved by `UID SEARCH`) and requeues
the job: the resume skips the item (a crash during `iter_items` skips the index, `kv` `pst:<id>:crash_skips`).
"libpff crash" items are no longer re-read (except "retry errors", once). After `MAX_CHILD_CRASHES`
(default 3) crashes of the same job → `failed` "The subprocess crashed N times (probable libpff crash)". A crash during
analysis → PST `invalid` "libpff crash during analysis". A brutal container stop is not counted as a crash
(usual resume through `recover()`). Test hook: `PSTMIG_TEST_CRASH` (see `app/server/crash.py`).

## Architecture
- A single `docker-compose.yml`, one image, two services: `web` (FastAPI/uvicorn, port 22362) and `worker`
  (job supervisor). The worker runs **every job and every PST analysis in a subprocess**
  (`python -m app.server.child job|validate`): a libpff SIGSEGV only kills that subprocess. The parent keeps the
  heartbeat; pause/stop go through the DB; on worker SIGTERM, subprocesses receive SIGTERM (SIGKILL after
  `CHILD_TERM_GRACE` s) and jobs go back to `queued`. Subprocesses die with the parent (PR_SET_PDEATHSIG). Volume `./data`: SQLite (WAL) `data/app.db`, PSTs `data/pst/`, reports `data/reports/`, logs.
- Python 3.12, `libpff-python` (pypff), FastAPI, SQLAlchemy 2 (or direct sqlite3), `cryptography` (Fernet), `httpx`,
  `icalendar`/`vobject`. IMAP via `imaplib` (IMAP4_SSL / STARTTLS / plain, configurable; TLS verification can be disabled per server).
- Dashboard auth: a single admin password, signed session cookie. Precedence: `ADMIN_PASSWORD` (env var, always wins) →
  otherwise a password generated on first start, stored hashed in `data/admin.json` and printed once in the `web` container
  log as a boxed banner containing `Admin password: XXXX`. Reset: delete `data/admin.json` and restart, or set
  `ADMIN_PASSWORD`. Mailbox passwords are encrypted (Fernet, key = `SECRET_KEY` env var, or generated in `data/secret.key` if empty).
- Front-end in `web/` (Svelte 5, Vite, Tailwind 4, Paraglide), built in a Node stage of the Docker image and served statically by FastAPI from `app/web/`. No CDN and no third party at run time (works offline).

## Layout
```
docker-compose.yml  Dockerfile  .env.example  README.md
app/engine/   # ENGINE agent: PST reading → EML / vCard / ICS, checks (pure, no network)
app/server/   # SERVER agent: API, DB, upload, IMAP, DAV, runner/worker
web/          # WEB agent   : UI sources; the build goes to app/web/ (generated)
tests/        # pytest tests; tests/fixtures/ sample PSTs; docker-compose.test.yml (dovecot/greenmail + radicale)
```

## ENGINE contract (`app/engine/__init__.py` exports exactly this)
```python
@dataclass
class FolderInfo:
    path: list[str]          # Unicode names from the PST root (root "Top of Personal Folders…" excluded)
    kind: str                # 'mail' | 'contacts' | 'calendar' | 'tasks' | 'notes' | 'journal' | 'other'
    container_class: str     # e.g. 'IPF.Note'
    special: str | None      # 'inbox'|'sent'|'drafts'|'trash'|'junk'|'outbox'|None
    count: int               # number of items
    _folder: object          # pypff handle

@dataclass
class BuiltMessage:
    raw: bytes               # full RFC 5322, clean UTF-8, contains X-PST-Import-Id
    internaldate: datetime   # tz-aware, = mail_date() (also the newest-first sort key)
    flags: list[str]         # e.g. ['\\Seen']
    subject: str
    attachments: int
    size: int
    warnings: list[str]      # e.g. 'fallback', 'rtf-converted', 'cid-unresolved:image001.png'

@dataclass
class DavObject:
    uid: str                 # deterministic
    filename: str            # f"{uid}.ics" / f"{uid}.vcf"
    content: str             # full VCALENDAR / VCARD 3.0
    summary: str

class PstFile:  # context manager
    def __init__(self, path: str): ...
    def folders(self) -> list[FolderInfo]: ...           # full traversal, stable order
    def iter_items(self, folder: FolderInfo, start: int = 0) -> Iterator[tuple[int, str, object]]:
        """(index, item_key, handle) ; item_key stable across two openings"""
    def build_message(self, handle, import_id: str) -> BuiltMessage | None   # None = not a mail
    def build_contact(self, handle) -> DavObject | None
    def build_event(self, handle) -> DavObject | None    # appointment, recurrence if possible (RRULE), attendees, reminder
    def scan(self) -> dict    # {'folders':[{path,kind,count}], 'totals':{'mail':n,'contacts':n,'calendar':n}, 'errors':[...]}
    def item_date(self, handle, kind: str) -> datetime | None  # optional: sort date, cheap (no MIME build);
        # kind 'mail' (mail_date(): same date as internaldate, see [How an import behaves](how-it-works.md)) | 'event' (start) | 'contact' (last modification > creation)

def verify_message(built: BuiltMessage, handle) -> list[str]   # blocking errors (empty = OK)
def check_pst_signature(path: str) -> bool
```
Clean exceptions, never a process crash on a corrupted item (the error bubbles up per item).
Bounded memory: never load the whole PST; read attachments in blocks if possible.

## UI
Pages: Dashboard (running jobs, global stats) · Servers · Accounts (table: email, test state ok/failed,
associated PST + status, mail/calendar/contacts options, progress, actions) · Uploads · Job detail (progress per phase,
current folder, rate, ETA, live log, errors, report). Multi-select + "Run". PST drag-and-drop.
Resumable upload after a refresh (key `filename+size+lastModified`), 3 parallel chunks, auto retry.
