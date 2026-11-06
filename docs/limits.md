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
