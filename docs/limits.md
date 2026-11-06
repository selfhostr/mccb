# Known limitations

- **A single administrator**: one shared password, no accounts or roles.
- **SQLite** (WAL) on the `./data` volume: a single host, no high availability. Web and worker
  share the same file, which must stay on a local disk (no NFS).
- **PST fingerprint tied to `UPLOAD_CHUNK_SIZE`**: changing this value after a first import
  loses recognition of the same file (see [Pause, resume and retry](resume.md)), with a risk
  of duplicates if the PST is re-uploaded.
- **libpff**: a corrupted PST can crash the native library. The affected items are
  skipped ("libpff crash" error); beyond `MAX_CHILD_CRASHES` crashes, the job fails. The counter
  is **per job** and reset at the end of the job. A badly damaged PST can be rejected at analysis.
- **Encrypted / password-protected PSTs**: no dedicated handling in the code; a PST whose
  content is encrypted will give errors or be `invalid`. Do not rely on it.
- **Depends on the quality of the target server**: APPEND, SPECIAL-USE, quota (`GETQUOTAROOT` if available)
  and DAV discovery vary between servers.
- **Routes other than PST to the server** (see [Connectors](connectors.md)):
  - Items are read one at a time in the job process, over one source connection: only the writes are parallel, so a slow source
    is the limit whatever the tuning. A PST sent to another target has no builder processes either.
  - One whole message is held in memory while it is sent; `BUILD_MAX_INFLIGHT_MB` bounds the total, not a single message.
  - An account still needs a server, even with its own target: the tuning settings come from it.
  - Tasks are listed by the interface, but no route migrates them.
  - Every calendar or address book of a source goes to one DAV collection.
- **IMAP as a source**:
  - One pass, not a sync: a flag changed or a message deleted at the source after its copy is not carried over. A rerun lists every
    folder again and copies what is new. `changed_since` exists in the interface, but jobs do not use it yet.
  - Each copied mail gains an `X-PST-Import-Id` header at the top; the rest of the message is unchanged. The target may refuse
    keywords (`$Forwarded`...): the message is then copied with its system flags only.
  - Messages flagged `\Deleted` are not copied, nor are virtual folders (`\All`, `\Flagged`, `\Important`), so nothing is copied
    twice from Gmail-like servers.
  - Renaming a source folder between two runs copies its messages again, under the new name.
  - After a UIDVALIDITY change, messages are recognized by Message-ID (date and size without one), in the same folder only. Two
    messages with no Message-ID and the same date and size can be confused.
  - Without SPECIAL-USE, special folders are recognized by their usual names only.
- **Scale.**
  - One message is built and sent whole, in memory: several times its size in its builder (source, encoding, verification, sending to the job process), then its size in the job process. Admission is by total, and the size of a message is not known before it is built, so several huge messages at once can still exceed the budget. The memory guard and the out-of-memory retry are what is left then.
  - SQLite has one writer at a time, shared by all the mailboxes and the web process. The limit of 32 mailboxes in parallel has not been measured against it on a large machine.
  - The index holds a few hundred bytes per item still to import in the job process, for the whole phase.
  - Every run reads the key and the date of every item of the PST, already imported or not: a resume costs that first. On millions of items, count minutes.
  - On a large mailbox the progress is recounted less often (a recount takes at most 2 % of the time, at least every 30 s), so the numbers shown move in steps.
  - Calendar and contact items are built and sent one at a time, on one connection.
  - After 2000 log lines about single items in a run, the next ones are only in the report (5000 at most) and the errors list.
  - `SEARCH HEADER` on a large folder can be slow on servers without a header index, and each resume or final check runs one per folder.
- **Auto-tuning signals.** A connection limit is learned by hitting it: the job then gets a few errors from the server, which it retries. Without `/proc/pressure/io` and without a block device behind the PST folder (network mount, for example), a saturated disk is not seen. The connection budget is counted per server entry: two entries pointing at one real server have one budget each.
- **No Swagger UI**: `/docs`, `/redoc` and `/openapi.json` are disabled (the API reference
  is in [API](api.md)).
- **In-memory login rate limiting**: 5 failures / 5 min per IP, reset when `web` restarts.
  Behind a proxy, check that `X-Forwarded-For` is forwarded, otherwise all requests
  share the proxy's IP.
- **Mailbox passwords are stored encrypted** but can be decrypted by the application: protect
  `./data` and the `SECRET_KEY`.
- **Browser support.** JS libs are vendored (works offline), but the
  browser must support modern APIs (fetch, SSE, `File.slice`).
- **Server-side intake.** A file in the drop folder is picked up once its size and modification time have not changed
  for `settle_seconds` (60 by default). A writer that pauses longer than that without touching the file can be picked up
  too early. A duplicate (same content as a PST already known) is left in the folder, never deleted.
  Resuming an HTTP download assumes the remote file did not change. SMB is not supported directly: mount the share
  on the server and point `INBOX_DIR` at it. `size` of a PST is 0 until the remote size is known.
- **Fetch disk check.** The free-space check does not reserve space between several transfers running at once.
- **Notifications.** A public ntfy.sh topic can be read by anyone who knows or guesses its name: use a long random
  topic, a self-hosted server with an access token, or enable "mask addresses". Delivery is best effort: three attempts
  with a short timeout, then the message is dropped and a line is logged. Events are detected by the worker every
  second, so a notification can arrive a few seconds late, and are not replayed after a long worker outage. Nothing is
  sent for events that happened before notifications were enabled.
