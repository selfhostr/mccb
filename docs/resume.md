# Pause, resume and retry

By the end of this page you will know what happens when a job is paused, stopped or interrupted, and why a resume never duplicates mail.

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

**Other sources.** The same states and the same marker apply to every route (see [Connectors](connectors.md)): the header holds
`<namespace-item_key>`, where the namespace identifies the source mailbox. Items are kept per source, so changing a password
keeps the state, and going back to a source already migrated sends nothing twice. For an IMAP source, `item_key` comes from the
folder, UIDVALIDITY and UID; if UIDVALIDITY changes, messages are recognized by Message-ID instead of being copied again.

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
