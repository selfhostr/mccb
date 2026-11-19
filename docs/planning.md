# Plan, filter and control a migration

By the end of this page you will know how to check a migration before it starts, choose what is migrated and where it lands, run many mailboxes in waves, keep to the hours a server allows, compare the result with the source, and finish with a last pass at cutover.

Everything here is optional. A mailbox and a server with nothing configured behave as before.

## Dry run

`POST /api/runs/plan {account_ids: [...]}` answers at once (`202`) with a plan `{id, status: "queued"}`. The worker fills it in; read it with `GET /api/plans/{id}` until `status` is `done` (or follow `plan` events on `/api/events`). A dry run connects, lists and counts. It writes nothing to any target, creates no job and changes no mailbox.

For each mailbox the result holds:

| field | content |
|---|---|
| `status` | `ok`, `warning` (something to read) or `error` (a connection or the source failed) |
| `source` | `{kind, ok, msg}`, the connection test of the source |
| `targets` | per kind (`mail`, `calendar`, `contacts`) `{ok, msg, quota?, append_limit?}` |
| `kinds` | per kind: `folders`, `items`, `bytes`, `done` (already migrated), `to_migrate`, `to_migrate_bytes`, `filtered` (per reason), `oversized`, `oversized_bytes`, `folders_to_create`, `excluded_folders`, `excluded_items` |
| `folders` | per source folder: `path`, `items`, `bytes`, `to_migrate`, `target` (the folder it will land in), `create` (true if it does not exist yet), `excluded`, `filtered`, `oversized` |
| `quota` | `{used, limit, free, need, enough}` when the target reports a quota |
| `target_limit` | the largest message the target accepts, when it says so (IMAP `APPENDLIMIT`) |
| `warnings` | what to read before starting: quota too small, messages over the limit of the target |
| `notes` | what to know: a PST has estimated sizes, its filters are judged during the run, excluded folders |

`totals` sums the mailboxes. Sizes of a live source (IMAP) are exact. For a PST they are estimated from the file size and its folder counts, and the date and size filters are not applied in the counts, because a PST has no item index before the run. A target that cannot say whether a folder exists (DAV) leaves `create` null.

## Policy: mapping, filters, hours, bandwidth

A policy is a JSON document on a server (`GET/PUT /api/servers/{id}/policy`) or on a mailbox (`GET/PUT /api/accounts/{id}/policy`). `PUT` replaces it; `null` or `{}` removes it. The mailbox is applied on top of the server: single values of the mailbox win, the lists are added (the mailbox's rules first).

```json
{
  "folder_map": [{"source": "Old mail", "target": "Archive/Old"}, {"source": "Mes envois", "target": "@sent"}],
  "exclude_folders": ["Junk*", "Deleted Items"],
  "date_from": "2020-01-01", "date_to": "2023-12-31",
  "max_size_mb": 25,
  "window": {"days": [0, 1, 2, 3, 4], "start": "20:00", "end": "06:00", "tz": "Europe/Paris"},
  "rate_mb_s": 5
}
```

`window` and `rate_mb_s` are accepted on a server only: they protect a target server. A running job keeps the policy it started with, except for the hours and the bandwidth limit, which it reads again. `GET /api/accounts/{id}/policy` also returns `effective`, the merged result.

### Folder mapping

The target folder of a source folder is decided in this order:

1. The first matching rule of `folder_map` (mailbox rules, then server rules). `source` is a folder path with `/` between levels, case-insensitive, or a pattern with `*` or `?`. Without a wildcard a rule covers the folder and its sub-folders. `target` is a path (`Archive/Old`; the sub-folders follow) or a role: `@inbox`, `@sent`, `@drafts`, `@trash`, `@junk`, which lands the folder in the special-use folder of the target (found by its SPECIAL-USE flag or its usual name, created with the flag if missing).
2. Built-in names, for a top-level folder (or one directly under the Inbox) that the source did not flag. Sent Items, Deleted Items, Junk E-mail, Drafts and their translations in the nine interface locales (de, en, es, fr, it, pt, pt-BR, ru, zh-Hans) land in the Sent, Trash, Junk and Drafts folders of the target. A role goes to the first folder that claims it; a second folder with a known name keeps its own name.
3. The special folders the source declared (SPECIAL-USE, PST).

Everything else keeps its path, under the `root_folder` of the server if there is one.

**What a change of mapping does to items already imported.** The mapping only chooses the name of the target folder; an item is identified by its source folder, not by where it was written. Items already imported (`ok`) are never sent again, so changing a mapping creates no duplicate and does not move them: they stay in the folder they were written to. Only the items not yet imported (pending, in error, new at the source) follow the new mapping. To move what is already there, move it on the target. The verification at the end of a job counts per folder actually written to, so it stays correct.

### Filters

`exclude_folders`, `date_from`/`date_to` (inclusive, UTC days) and `max_size_mb` leave items out. An item left out is not an error:

- it gets the status `filtered` with the reason (`date`, `size`) and counts as done in the progress;
- the report has `totals.<kind>.filtered`, and a `filtered` section with the total, the count per reason and per folder; `GET /api/jobs/{id}/filtered` lists the items;
- an excluded folder is not listed at all; the report gives it under `filtered.folders` with its item count.

Filters are judged again at every run. If you widen a filter and rerun the job, the items it had left out are migrated, once. Items without a date are never left out by the date filter. The date filter applies to mail and calendar items, not to contacts. The size filter applies to mail; for sources that do not know a size before reading it, the message is read and then left out.

### Messages the target refuses for their size

When the target answers that a message is too big (IMAP `[TOOBIG]` and the usual wordings), the item gets one clear entry: status `error`, message starting with `[oversize]` and the size. It is not retried by itself: a rerun of the whole job leaves it alone, and only "retry errors" tries it again, once per request. When the target announces its limit (`APPENDLIMIT`), a message above it is not even sent. The report has an `oversized` section (`count`, `bytes`, `largest`, and the items, largest first); `GET /api/jobs/{id}/oversized` lists them. Raise the limit on the target, then use "retry errors".

### Allowed hours and bandwidth

With a `window`, queued jobs of that server wait outside the hours and start when they open. A running job pauses by itself when the window closes, between two items, and the worker resumes it when it opens again. Such a pause has `pause_reason: "window"` in the job, a line in the job log and the status `paused`. It is not a failure, no notification is sent, and the administrator's own pauses (no reason) are never resumed by the window. `days` are 0 (Monday) to 6 (Sunday); a window whose end is before its start runs past midnight and belongs to the day it starts.

`rate_mb_s` limits the data sent to a server, in MB/s, across its jobs. Every job runs in its own process, so each running job of the server gets an equal share, recounted every few seconds. The limit is applied just before a message is sent. The auto-tuner is told: while the limit is what holds the transfer back, it does not open more connections, which would only queue data behind the limit.

## Batches and queue order

`POST /api/runs {account_ids, name?, mode?, priority?}` with a `name` creates a batch: the jobs started together share a `batch_id`. A mailbox that already has an active job keeps it and joins no batch.

- `GET /api/batches` lists them with `jobs`, `by_status`, `state` (`queued`, `running`, `paused`, `finished`), `items` (done, total, errors per kind), `failed`, `with_errors`; `GET /api/batches/{id}` adds `job_list`.
- `POST /api/batches/{id}/pause`, `resume`, `stop`, `retry-failed` act on every job that allows it and answer with the batch plus `changed` and `skipped`. `retry-failed` reruns the jobs in `failed` (full rerun) and `done_with_errors` (errors only).
- When all the jobs of a named batch are over, the `batch_finished` notification names the batch and lists the jobs with problems. It replaces the generic "queue idle" summary for those jobs.

The worker starts queued jobs by priority (highest first), then by age. `POST /api/jobs/reorder {job_ids: [...]}` puts the listed queued jobs first, in that order, and keeps the others behind them; it answers `{queue: [...]}`. `PUT /api/jobs/{id}/priority {priority}` sets one value. Jobs already running are not interrupted.

## Compare source and target

`POST /api/jobs/{id}/compare {detail?}` queues a comparison, run by the worker; `GET /api/jobs/{id}/compare` returns the latest (`status` `queued`, `running`, `done`, `failed`, or `none`); `GET /api/jobs/{id}/compare?run=1` queues a new one. For each target folder it gives `expected` (what the job meant to put there: sent, in error and pending), `on_target` (messages carrying this migration's marker, found on the target), `missing`, `extra`, and the counts `sent`, `errors`, `pending`, `filtered`, `skipped`. Items left out by a filter have their own lines with no target. With `detail`, the mails recorded as sent to a folder that holds fewer are looked up one by one (500 per folder at most) and listed in `missing_items`.

`GET /api/jobs/{id}/compare/csv` downloads it; the latest finished comparison is also in the job report under `compare`.

## Final resync and cutover

A first pass copies the bulk while users keep working. A final resync picks up what arrived since, without duplicates. Start it with `POST /api/runs {account_ids, mode: "delta"}` or, for a finished job, `POST /api/jobs/{id}/delta`.

- A source that can list changes (IMAP) is asked only for what is new since the previous delta pass of each folder. The first delta pass has no cursor and lists everything, skipping what is already migrated.
- A source that cannot (the others) is listed in full, and the items already migrated are skipped. The result is the same, only slower.
- The cursor of a folder moves only when everything listed is recorded. A job that stops half way starts again from the old cursor, and the items already done are skipped. Changing the target of a mailbox forgets its cursors, with its items.
- A PST is a snapshot: a delta pass over the same PST finds nothing new. For mail that arrived after the export, send a new PST holding only the new items, or migrate those mailboxes from IMAP.

Cutover, step by step:

1. Run the dry run and fix what it shows (quota, folders, messages over the limit).
2. Migrate the bulk in named batches, inside the allowed hours of the server.
3. Compare each job; rerun the errors.
4. On the day: point the mail flow to the new server, so nothing new reaches the old one.
5. Run the final resync (`mode: "delta"`) for every mailbox, then compare again.
6. Check the reports (`totals`, `oversized`, `filtered`), then retire the old mailboxes yourself. mccb never deletes anything at the source.

A delta pass only adds. A mail deleted at the source stays on the target, and a contact changed at the source keeps its old version. When the users keep working on the old server for a while, use the mirror instead.

## Mirror

A mirror pass makes the target follow the source again, after a first migration of the mailbox. Between two mail servers, it:

- adds what is new at the source, like a delta pass;
- deletes on the target what disappeared from the source: mail, events, contacts and tasks;
- updates what changed at the source: the flags of a mail (read, starred, answered, draft, keywords), the folder of a mail moved to another folder, and events, contacts and tasks whose version (etag) changed.

It never writes to the source: no deletion, no flag, no mark of any kind. The source is reached through a wrapper that only lets reads through. A PST is a snapshot and is not mirrored.

Mirror today: IMAP to IMAP (mail), CalDAV to CalDAV (events and tasks), CardDAV to CardDAV (contacts). The other connectors take part as they declare the capabilities of [Connectors](connectors.md#mirror-mode); a source that does not is refused with `source_unlistable`.

### Turn it on and run it

1. Enable the option on the mailbox: `PUT /api/accounts/{id} {"options": {"mirror": true}}`. `GET /api/accounts/{id}` returns it in `options` (and as `mirror`). A mailbox without it is refused with `mirror_off`.
2. Look at what a pass would do: `POST /api/runs {account_ids, mode: "mirror"}`. This is a dry run, the default: it connects, lists and compares, and writes nothing anywhere and creates no job.
3. Run it: the same request with `"dry_run": false`. One job per mailbox is created, with the mode `mirror`.

| field | default | meaning |
|---|---|---|
| `dry_run` | `true` | compute only |
| `max_delete_percent` | `20` | refuse the deletions of a kind when more than this share of its migrated items would be deleted |
| `allow_large_delete` | `false` | lift `too_many_deletes` and `source_empty` |

The dry run answers per mailbox:

```json
{"accounts": [{"account_id": 4, "email": "ana@example.org", "ready": true,
  "to_add": {"mail": 12, "calendar": 0, "contacts": 1, "tasks": 0},
  "to_update": {"mail": 30, "calendar": 2, "contacts": 0, "tasks": 0},
  "to_delete": {"mail": 3, "calendar": 1, "contacts": 0, "tasks": 0},
  "blocked": [],
  "sample": [{"kind": "mail", "action": "delete", "detail": "delete", "folder": "INBOX", "subject": "Lunch", "date": ""}]}]}
```

`sample` holds 20 actions at most, deletions first, subjects cut to 80 characters. In it, a move and a change of flags are `update`, and `detail` says which. With `dry_run: false` the answer is `{"accounts": [{account_id, email, ready, blocked, job_id?}], "jobs": [...]}`, and only the codes known without connecting are checked there. The job checks every rule again when it runs.

The job, `GET /api/jobs/{id}`, has `mirror: {options, blocked, mail: {...}, calendar: {...}, contacts: {...}, tasks: {...}}`, each kind with `added`, `updated`, `deleted`, `skipped_by_safety` and `errors`. The report has the same `mirror` section, and `totals.<kind>.mirror`. The log has one line per action, with the key, the identifier on the target, the folder and the result. It never has a subject or any content.

### Safety rules

| code | when | what happens |
|---|---|---|
| `mirror_off` | the mailbox does not have the option | nothing |
| `no_prior_run` | the mailbox has never finished a job | nothing: run a first full pass |
| `target_unreachable` | a target cannot be reached or listed | nothing |
| `source_unlistable` | the source cannot be mirrored, a folder could not be listed, a listing was incomplete, the source could not confirm a deletion, or a folder is no longer at the source | no deletion at all in this pass; additions and updates go on |
| `uidvalidity_changed` | the keys of a folder were reset (IMAP UIDVALIDITY, or an epoch marked `renamed-or-moved` / `unverified`) | that folder is left alone: run a full pass |
| `source_empty` | the source lists nothing for a kind while items of it were migrated | no deletion of that kind, unless `allow_large_delete` |
| `too_many_deletes` | more than `max_delete_percent` % of the migrated items of a kind would be deleted | no deletion of that kind, unless `allow_large_delete` |

`ready` is true when nothing is blocked. A job that runs with a non-fatal code ends `done_with_errors`, with the code in `error`, and the deletions held back are counted in `skipped_by_safety`.

Other rules:

- **Only what mccb wrote is touched.** The mirror works from the `items` table: an item is deleted or updated only if mccb migrated it (status `ok`), and it is found on the target by its import id (mail) or its resource name (DAV). The content the target had before the migration, and mail that arrived on the target directly, are never in that list.
- **A deletion needs proof.** The folder must have been listed completely and without error in this pass, and the source is asked once more, item by item, just before deleting. A folder that is no longer listed (deleted or renamed at the source) deletes nothing.
- **Mail goes to the trash.** A deleted mail is moved to the trash folder of the target and marked `$MccbMirrorDeleted`, so that it can be found and restored. Without a trash folder, or in the trash itself, it is flagged `\Deleted` and marked, and never expunged by mccb. DAV has no trash: before deleting an event, a contact or a task, mccb keeps a copy under `<data>/mirror-trash/job-<id>/`, and deletes nothing it could not copy. The DAV deletion uses `If-Match`: an item changed on the target since the listing is not deleted.
- **Idempotent.** A pass right after another finds nothing to do and writes nothing. An item deleted at the source is recorded as `deleted`; if it comes back at the source, the next pass copies it again.
- **What is compared.** For mail, the flags at the source against those recorded when it was last written; items migrated before this version have no record, and their flags are compared once with the target. A mail moved to another folder at the source is moved on the target (same Message-ID, gone from one folder, new in the other), and keeps its import id. For events, contacts and tasks, the etag at the source; an item whose etag is unknown is never updated. Changes made on the target to our items are overwritten by the next change at the source.

Changing the target of a mailbox forgets its items: the next mirror pass then sees everything as new and deletes nothing.

## Mailboxes as CSV

See [Import mailboxes](import-accounts.md): the file can carry the PST file name, the content toggles and the settings of a source, and `GET /api/accounts/export-csv` writes the same columns back, never a password.

## Tests

`tests/server/test_planning.py` runs these tools against in-memory connectors; `tests/server/test_planning_imap.py` runs the IMAP cases against GreenMail.
