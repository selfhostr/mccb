# Configuration

All variables are optional. Set them in `.env` (see [`.env.example`](https://github.com/yetysw/mccb/blob/main/.env.example)); the `web` and `worker` services both read it.

| variable | default | role |
|----------|---------|------|
| `ADMIN_PASSWORD` | *(empty)* | UI password. **Optional**: if empty, a random password is generated on first start (see [Change or reset the admin password](admin-password.md)). When set, it always wins. |
| `SECRET_KEY` | *(empty)* | Key used to encrypt mailbox passwords and sign sessions. Empty: generated in `data/secret.key`. |
| `UPLOAD_CHUNK_SIZE` | `33554432` (32 MiB) | Upload chunk size, in bytes (16 to 64 MiB recommended). **Do not change it during a migration**, see [Pause, resume and retry](resume.md). |
| `UPLOAD_DISK_MARGIN` | `536870912` (512 MiB) | Free disk space margin required on top of the PST size. |
| `WORKER_CONCURRENCY` | auto | Number of mailboxes migrated simultaneously by the worker. Unset or `0`: chosen from the usable cores (cgroup-aware) and the free memory (2 cores and 512 MB per job, at most 4, at most the jobs waiting). |
| `BUILD_PROCESSES` | per server tuning | When set, forces in both tuning modes the number of processes building MIME messages for one mailbox (each opens the PST read-only). A builder killed by a libpff crash marks its item "libpff crash" and is restarted; crashes count towards `MAX_CHILD_CRASHES`. `0` = build inside the job process (previous behaviour). Total CPU use is about `WORKER_CONCURRENCY` × `BUILD_PROCESSES`. |
| `AUTOTUNE_BENCH_SECONDS` | `1.5` | Length of the build benchmark run at the start of an auto-tuned job (`0` = none). |
| `AUTOTUNE_INTERVAL` | `5` | Seconds between two auto-tuning samples during the mail phase. |
| `BUILD_MAX_INFLIGHT_MB` | `256` | Memory budget for built messages waiting for their APPEND. |
| `VERIFY_SAMPLE` | `20` | Number of mails re-read for the sample check at the end of a job. |
| `IMAP_MAX_RETRIES` | `5` | Attempts on temporary IMAP errors (exponential backoff). |
| `MAX_CHILD_CRASHES` | `3` | Subprocess (libpff) crashes tolerated per job before it is marked `failed` (minimum 1). |
| `CHILD_TERM_GRACE` | `20` | Seconds given to subprocesses to stop on SIGTERM before SIGKILL. |
| `COOKIE_SECURE` | `0` | `1` = `Secure` session cookie (HTTPS behind a reverse proxy). The cookie is also `Secure` if the request arrives over HTTPS. |
| `SESSION_MAX_AGE` | `604800` (7 days) | Session lifetime, in seconds. |
| `WORKER_STALE_SECONDS` | `60` | A `running` job whose heartbeat is older is considered orphaned and requeued. |
| `PORT` | `22362` | Listening port of the `web` service inside the container (the compose file publishes `22362`). |
| `DATA_DIR` | `/data` | Data directory (set by the compose file). |
| `INBOX_DIR` | `DATA_DIR/inbox` | Drop folder watched by the worker (can be a mount). See [Server-side intake](server-intake.md). |
| `FETCH_PARALLEL` | `2` | Simultaneous server-side transfers (drop folder registrations and downloads). |
| `FETCH_BLOCK_PRIVATE` | `0` | `1` = also refuse private ranges and loopback as fetch sources. Link-local and metadata addresses are always refused. |
| `FETCH_ALLOW_LINK_LOCAL` | `0` | `1` = allow link-local addresses as fetch sources. Not recommended. |
| `PSTMIG_ENGINE` | `app.engine` | Engine module; used only by tests. |

`.env.example` only lists the common variables; `SESSION_MAX_AGE`, `WORKER_STALE_SECONDS`,
`CHILD_TERM_GRACE` and `PORT` are read by `app/server/config.py` and are optional.

Notification channels, the drop-folder delay and the automatic assignment are not environment variables: they are
set in the application (`GET/PUT /api/intake/settings`) and stored in `app.db`. See [Notifications](notifications.md)
and [Server-side intake](server-intake.md).
