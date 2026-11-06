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
- **No Swagger UI**: `/docs`, `/redoc` and `/openapi.json` are disabled (the API reference
  is in [API](api.md)).
- **In-memory login rate limiting**: 5 failures / 5 min per IP, reset when `web` restarts.
  Behind a proxy, check that `X-Forwarded-For` is forwarded, otherwise all requests
  share the proxy's IP.
- **Mailbox passwords are stored encrypted** but can be decrypted by the application: protect
  `./data` and the `SECRET_KEY`.
- **Browser support.** JS libs are vendored (works offline), but the
  browser must support modern APIs (fetch, SSE, `File.slice`).
