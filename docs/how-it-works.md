# How an import behaves

This page describes the details of one import: authentication, uploads, folders, ordering, sending and checks.

- **Authentication.** A single admin password (`ADMIN_PASSWORD`, or the generated one stored hashed in
  `data/admin.json`). The session is a signed cookie (HttpOnly, SameSite=Lax). Mailbox passwords are
  encrypted with Fernet (key derived from `SECRET_KEY`).
- **Upload.** The file is pre-allocated to its final size (`fallocate`). Each chunk is read as a stream,
  written at its offset, then its SHA-256 is compared to the `X-Chunk-Sha256` header (422 if different). The
  list of received chunks is stored in the database. `complete` answers 409 if chunks are missing.
- **IMAP folders.** Names in modified UTF-7, server separator, NAMESPACE prefix honored. Sent,
  Drafts, Trash and Junk are mapped to the server's SPECIAL-USE folders (or found
  by name). With `root_folder`, the whole PST is imported beneath it, with no special mapping.
- **Import order: newest first.** At the start of the mail phase the job indexes the dates of the
  mails still to import (the mail date: the Date header only if it has a timezone, lies between 1980 and the delivery time
  (or now + 2 days) and is not more than 30 days after delivery; otherwise client submit, delivery, creation, then modification time.
  The same rule gives the IMAP INTERNALDATE, so order and INTERNALDATE always match; read without
  building the message, in the builder processes), then imports them **across all folders** from the
  newest to the oldest; all IMAP folders are created up front. Calendar: by start date, newest first;
  contacts: by last modification (else creation), newest first. Items without a date come last, ties are
  broken by item key. The UI shows `Indexing n / total` meanwhile; the log shows `Indexing mail items (newest first)…` then `Indexed N item(s) in X s`. Finished jobs show their average speed (`avg X items/s · Y MB/s`).
  The order is recomputed on each run (deterministic): a resume skips `ok` items, checks `inflight`
  ones on the server, and continues with the remaining items in the same order. With several IMAP
  connections, neighbouring messages may be stored a few positions apart (small inversions from parallel IMAP connections are expected). A libpff crash while reading
  a date only leaves that item without date (imported last); it is not counted as a crash.
- **Sending.** Parallel APPEND over N connections (server `workers`), reconnect and exponential
  backoff on temporary errors (`IMAP_MAX_RETRIES`). Permanent errors are recorded per
  item. Exceeding the quota fails the job.
- **Checks.** After the mail phase, for each folder, `STATUS MESSAGES` must be at least equal to the number of mails
  sent. At the end of the job, `VERIFY_SAMPLE` mails are re-read by header and compared by hash. DAV
  resources are counted.
- **Real time.** `GET /api/events` (SSE) relays changes, detected by SQLite triggers
  (the worker is another process).
