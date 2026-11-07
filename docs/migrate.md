# Migrate a mailbox

By the end of this page a mailbox, its calendar and its contacts will be copied from a source to a destination server, with a report.

The source can be a file (PST, OST, mbox, Maildir in a zip, EML, MSG, ICS, VCF) or a live server (IMAP, Exchange on premises, Microsoft 365, Google Workspace, JMAP, CalDAV, CardDAV). Server to server needs no file at all: IMAP to IMAP, or Microsoft 365 to any IMAP server, follows the same steps. Each source is described in [Protocols and file formats](protocols.md), and how a source and a destination are put together in [Connectors](connectors.md).

The interface has four parts, in the sidebar (the top bar on a phone): **Migrations**, **Files**, **Servers** and **Settings**. On an empty install, the Migrations page lists the three steps below with the button of the next one.

1. **Add the destination server (Servers).** Pick the provider (Gmail, Microsoft 365, iCloud, Fastmail, Infomaniak, or another IMAP server): it fills the host, the port and the security, and says what the provider asks for, such as an app password. Adjust if needed: TLS verification (can be disabled), optional root folder (`root_folder`, e.g. `PST Import`), CalDAV and CardDAV URLs (empty = auto-discovery, see [DAV discovery](dav.md)), tuning. The server is checked right after it is saved, and the list shows **Verified**, **Check failed** or **Not verified**. Microsoft refuses plain passwords over IMAP: to write into Microsoft 365, choose Exchange Online as the destination of each mailbox instead (see [Exchange and Microsoft 365](exchange.md)).
2. **Create a migration (Migrations, New migration).** One form in two parts:
   - **From**: a file already on the server, a file to send now, or a live server picked from the tiles (Exchange, Microsoft 365, Google Workspace, IMAP, JMAP, CalDAV, CardDAV). A server source is configured in the same form and has its own test button. **Choose later** is allowed: the mailbox then waits for a source.
   - **To**: the destination server, the address of the mailbox there, its password and an optional login (empty = the address).

   Many mailboxes at once: **Import a CSV file** in the same dialog, see [Import mailboxes](import-accounts.md). The login to the destination is checked right after saving.
3. **Start it (the mailbox's panel).** Saving opens the mailbox's panel. It reads **From**, **To** (with the result of the login check) and **What to copy** (mail, calendar, contacts), each marked ready, to do or needs attention, and ends with **Start migration**. While it cannot start, a sentence says what is missing (no source yet, no password). For several mailboxes, select them in the list, then use the selection bar (`POST /api/runs`). Before the run, a [dry run](planning.md) shows counts, sizes, quota and the folders that will be created, and a [policy](planning.md) can map folders, leave some out and set allowed hours.
4. **Follow and control.** One job per mailbox, queued. The worker runs `WORKER_CONCURRENCY` at a time (auto by default). Phases: `precheck` (source, destination login, DAV, quota), `mail`, `calendar`, `contacts`, `verify`. The job panel shows progress per phase, current folder, rate, time left, the live log and the errors, and can pause, resume, stop, retry everything or retry the errors. Commands take effect between two items.
5. **Read the report.** At the end of the job, a JSON report (`GET /api/jobs/{id}/report`, also in `data/reports/`): sent, present, errors, warnings, skipped. A job is `done` if everything passed, `done_with_errors` otherwise (errors, failed verification or unfinished items). **Run again** later copies only what is new: items already migrated are skipped.

## Files as a source

Files arrive on the **Files** page, from the New migration form, from the drop folder or by a server-side fetch (see [Server-side intake](server-intake.md)).

- **Upload.** Large files are split into chunks (32 MiB by default), sent 3 in parallel with automatic retry, each verified by SHA-256 server-side. The upload is **resumable**: after a refresh or a network drop, sending the same file again (key `name + size + modification date`) resumes at the missing chunks. Disk space is checked first (507 if there is not enough room). Several files queue one at a time by default, in the order of your choice.
- **Analysis.** When the upload finishes, the worker checks the file (the `!BDN` signature for a PST) and counts mails, appointments and contacts per folder, in a subprocess. The file becomes `valid` or `invalid`, with the reason.
- **Preview (.zip).** On a `valid` PST, `GET /api/psts/{id}/preview?limit=20` (limit from 1 to 200) gives a zip of an evenly spread sample of mails (`.eml`), appointments (`.ics`) and contacts (`.vcf`), by folder. Open it **before starting** to check accents (subjects, bodies, folder names), attachment names and types, and embedded images. Generated in a subprocess, 120 s maximum; 409 if the PST is not valid, 500 if libpff crashes.
- **Link.** A valid file is linked to its mailbox in the mailbox's panel (**From**), or by its name when automatic assignment is on.

## Server to server, and a final resync

A live source can be copied while people keep working. When they switch, a **final resync** (`mode: "delta"`, offered in the start dialog once the mailbox already ran) copies only what arrived since, without duplicates. Run it as often as needed. A migration copies in one direction only, and a message deleted at the source after its copy stays at the destination: see [Limits](limits.md).
