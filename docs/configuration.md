# Configuration

All variables are optional. Set them in `.env` (see [`.env.example`](https://github.com/selfhostr/mccb/blob/main/.env.example)); the `web` and `worker` services both read it.

| variable | default | role |
|----------|---------|------|
| `ADMIN_PASSWORD` | *(empty)* | UI password. **Optional**: if empty, a random password is generated on first start (see [Change or reset the admin password](admin-password.md)). When set, it always wins. |
| `SECRET_KEY` | *(empty)* | Key used to encrypt mailbox passwords and sign sessions. Empty: generated in `data/secret.key`. |
| `UPLOAD_CHUNK_SIZE` | `33554432` (32 MiB) | Upload chunk size, in bytes (16 to 64 MiB recommended). **Do not change it during a migration**, see [Pause, resume and retry](resume.md). |
| `UPLOAD_DISK_MARGIN` | `536870912` (512 MiB) | Free disk space margin required on top of the PST size. |
| `WORKER_CONCURRENCY` | auto | Number of mailboxes migrated simultaneously by the worker. Unset or `0`: auto, from the usable cores (cgroup-aware), the free memory, the disk and the queue (2 cores and 512 MB per job, at most 32, at most the jobs waiting, more while cores stay idle). See [Tune](tuning.md). |
| `REPAIR_BACKUP_FOLDER` | `_originaux_avant_reparation` | Folder of a repaired mailbox that keeps the originals, see [Repair a damaged mailbox](repair.md). Keep it constant: restore reads it. |
| `REPAIR_PARALLEL` | `2` | Mailboxes analysed or repaired at the same time. |
| `BUILD_PROCESSES` | per server tuning | When set, forces in both tuning modes the number of processes building MIME messages for one mailbox (each opens the PST read-only). A builder killed by a libpff crash marks its item "libpff crash" and is restarted; crashes count towards `MAX_CHILD_CRASHES`. `0` = build inside the job process (previous behaviour). Total CPU use is about `WORKER_CONCURRENCY` × `BUILD_PROCESSES`. |
| `AUTOTUNE_BENCH_SECONDS` | `0.25` | Length of the build benchmark run at the start of an auto-tuned job (`0` = none). |
| `AUTOTUNE_INTERVAL` | `5` | Seconds between two auto-tuning samples during the mail phase. |
| `AUTOTUNE_MAX_CONNECTIONS` | `32` | Most IMAP connections of one mailbox in auto mode. |
| `AUTOTUNE_MAX_SERVER_CONNECTIONS` | `64` | Most IMAP connections to one server across all its mailboxes in auto mode. |
| `BUILD_MAX_INFLIGHT_MB` | auto | Memory budget for built messages waiting for their APPEND, per mailbox. Unset: 256 MiB, or in auto mode a quarter of the free memory split between the running mailboxes, at most 4 GiB. |
| `MEM_GUARD_MB` | `512` | Below this much free memory, builds run one at a time. |
| `IMAP_SEARCH_TIMEOUT` | `900` | Seconds allowed for a `SEARCH HEADER`, which can read a whole folder on the server. |
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
| `M365_BOOTSTRAP_CLIENT_ID` | `14d82eec-204b-4c2f-b7e8-296a70dab67e` | Public client app used by "Connect with Microsoft" to create the Entra app of a Microsoft 365 tenant (device code sign-in). Empty: turned off, the app details are entered by hand. See [Connect with Microsoft](#connect-with-microsoft). |
| `PSTMIG_ENGINE` | `app.engine` | Engine module; used only by tests. |

## Connect with Microsoft

"Connect with Microsoft" signs in an administrator of the client tenant with the device code flow (no redirect URI, works
when mccb is only reachable on a LAN), then creates the tenant's Entra app with that delegated access. The sign-in goes
through a public client app named by `M365_BOOTSTRAP_CLIENT_ID`.

By default this is Microsoft's own **Microsoft Graph Command Line Tools** app (`14d82eec-204b-4c2f-b7e8-296a70dab67e`),
present in every tenant: the consent screen shows that name. mccb asks it for the delegated permissions
`Application.ReadWrite.All` and `AppRoleAssignment.ReadWrite.All` only. Microsoft does not officially intend this app for
third-party tools; it is used until selfhostr registers its own. To use your own bootstrap app instead, register in Entra:

1. a new app registration, supported account types **Accounts in any organizational directory (multitenant)**;
2. Authentication: **Allow public client flows: Yes**, no redirect URI;
3. API permissions: Microsoft Graph, **delegated** `Application.ReadWrite.All` and `AppRoleAssignment.ReadWrite.All`;
4. no client secret.

Then set `M365_BOOTSTRAP_CLIENT_ID` to its Application (client) ID. Set it to an empty string to turn the feature off.

`.env.example` only lists the common variables; `SESSION_MAX_AGE`, `WORKER_STALE_SECONDS`,
`CHILD_TERM_GRACE` and `PORT` are read by `app/server/config.py` and are optional.

Notification channels, the drop-folder delay and the automatic assignment are not environment variables: they are
set in the application (`GET/PUT /api/intake/settings`) and stored in `app.db`. See [Notifications](notifications.md)
and [Server-side intake](server-intake.md).
