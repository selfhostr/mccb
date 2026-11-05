# Migrate a mailbox

By the end of this page a mailbox, its calendar and its contacts will be imported from a PST file, with a report.

1. **Servers.** Create a server: IMAP host and port, security (`ssl`, `starttls`, `none`), TLS
   verification (can be disabled), optional root folder (`root_folder`, e.g. `PST Import`), CalDAV/CardDAV URL
   (empty = auto-discovery, see [DAV discovery](dav.md)), number of parallel connections (`workers`). A test button
   checks the connection.
2. **Accounts.** Add mailboxes one by one or via [CSV](import-accounts.md). The IMAP login is
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
