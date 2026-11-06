# Changelog

## Unreleased
- Planning (see [Plan, filter and control a migration](docs/planning.md)): dry run (`POST /api/runs/plan`), folder mapping with built-in names in nine languages, per-server and per-account policy (folder rules, excluded folders, date range, maximum size), allowed hours and a bandwidth limit per server, named batches with group actions, queue order, comparison of source and target (CSV, in the report), final resync (`mode: "delta"`) and a clear report of messages the target refuses for their size.
- Accounts CSV: optional columns (PST file name, content toggles, source settings) and an export that never contains a password. Old three-column files are read as before.
- API tokens and the `mccb` command line (upload with resume, run, watch, plan), see [Command line and API tokens](docs/cli.md).
- Auto mode can use far more of a large machine: up to 32 mailboxes at once (more while cores stay idle), 32 builders and 32 connections per mailbox. `.env.example` no longer pins `WORKER_CONCURRENCY`: remove that line from an existing `.env` to get the auto mode. See [Tune](docs/tuning.md).
- Costs that grew with the mailbox: the progress is recounted less often on large mailboxes, the average size no longer reads the whole table at each update, item results are written in batches.
- One network error no longer limits a job for good: the connection ceiling, the plateau and the fallback all recover with time.
- A server error now holds all the connections of the job and stops growth for every mailbox on that server. Servers answering "too many connections" with a plain NO are handled as overload. CalDAV and CardDAV honour `Retry-After`.
- Memory guard: builds go one at a time when memory is short. File cache no longer counts as used inside a container.
- Resume checks many items of a folder with one search. Final folder checks run once, no longer twice.
- Upload: `PUT` of a chunk no longer returns the list of received chunks, the `upload` event carries `received_count` instead, and hashing no longer blocks the web process.
- New web UI: three pages (Migrations, Files, Settings) instead of six, job details in a side panel with their own URL. Old links redirect.
- Live events update only what changed, at most once per frame. Rows are keyed and long lists are windowed, so clicks are not lost and open selects stay open during imports.
- Selection is explicit: what a filter hides is counted, shown and left out of any action. The selection bar is fixed and shifts nothing.
- Connection tests show a running state, cannot start twice, report IMAP, CalDAV and CardDAV, and stay visible after a reload.
- Upload queue: one file at a time by default (setting), order chosen when adding several files, reorder, pause, resume, cancel. Hashing no longer blocks the page.
- Dark and light themes, density, visible columns, mask addresses, nine languages, stored in the browser.

## 1.1.2
- The product is now MCCB (Mail Contact Calendrier Bridge): tab title, sidebar, login screen, logs, image title and the PRODID of exported vCards and events.
- The session cookie is renamed: everyone signs in again once after the upgrade.
- README and documentation restructured per the yety guidelines (docs/ pages, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue templates).

## 1.1.1
- Mail order and IMAP INTERNALDATE now use the same date (`mail_date`): the Date header only if it has a timezone and is plausible, otherwise MAPI submit, delivery, creation or modification time.
- Finished jobs show the average speed (items/s and MB/s).
- Log line "Indexing mail items (newest first)…".

## 1.1.0
- Version shown in the UI and at `/api/version`.
- Newest-first import for mail, calendar and contacts.
- Auto tuning by default (machine probe, AIMD control of IMAP connections), with a Manual override.
- Parallel message builders; about 2-4x faster.

## 1.0.0
- Initial release.
