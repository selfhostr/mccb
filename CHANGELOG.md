# Changelog

## Unreleased

### Fixed
- CalDAV / CardDAV targets: contacts, events and tasks an earlier run put in the collection of another folder of the source now go where they belong. Seen with a source holding "Contacts" and "Contacts suggérés": the cards of "Contacts" were in the target's "Contacts suggérés" and its own "Contacts" stayed empty. Since the collection was still in use by the other folder, nothing ever looked at it again: the clean-up only deletes collections no folder goes to, and the mirror took the cards as they were. A normal run, a final resync and the mirror pass started from Start now write each such item into the collection of its folder, read it back there, then delete the misplaced copy, keeping a copy of it under `mirror-trash/`. A copy is only deleted when mccb can show it is its own: the etag of its write, or a collection mccb created holding the object under the name mccb gave it after its UID, or the very object of the source. An object of that collection's own folders using the same resource is never deleted. Anything else is left where it is and the item is written from the source, said in the log (`misplaced: ...`). On a server that keeps one object per UID in the account (SmarterMail), the proven copy is moved first, and written back when the collection of its folder still refuses it. The log has one line per collection (`Contacts: 308 card(s) moved from 'Contacts suggérés' to 'Contacts' where they belong`) and the report lists them (`dav_misplaced`).
- An item given up because the source no longer listed it (`no longer at the source`) is written again when the source lists it again, in a normal run and in the mirror. Before, it stayed `skipped` for good. This also brings back the suggested contacts left out next to the misplaced cards above.

## 2.3.0
mccb 2.3.0 adds a mirror mode for server to server migrations, chosen in the Start dialog: a rerun first copies what is new at the source, then carries over its deletions and moves. It fixes what a real IMAP to IMAP migration showed: calendars and contacts of the source server are now migrated with the mail, they land in the address book and calendar the target already has (the duplicates earlier versions made are deleted by themselves), accents are kept, folders shared by other mailboxes are no longer copied, and mails read at the source arrive read. Each mailbox shows its size and the bytes imported.

### Added
- Mirror mode for migrations from a mail server to a mail server (`POST /api/runs {mode: "mirror"}`): a pass after the migration adds what is new at the source, deletes on the target what disappeared from the source (mail to the trash of the target, marked `$MccbMirrorDeleted`; events, contacts and tasks with a copy kept under `mirror-trash/`), and updates what changed (mail flags and folder, events, contacts and tasks by their etag). It only runs on mailboxes with the new `mirror` option, it is a dry run unless `dry_run: false`, and it refuses to delete when a listing of the source failed or was incomplete, when the source is empty, when the keys were reset (UIDVALIDITY), or when more than `max_delete_percent` % (20 by default) of the migrated items would go. The source is only ever read, and only items mccb wrote itself are touched on the target. IMAP to IMAP and CalDAV/CardDAV to DAV in this version. See [Plan, filter and control](docs/planning.md#mirror).
- Mailbox sizes: the size of the mail at the source and the bytes already imported. An IMAP source is measured in the background as soon as it is set (`STATUS SIZE` when the server has it, else the sum of `RFC822.SIZE`, read only), the source test says it when it takes a few seconds at most, and every job refreshes it from its listing when the source gives the size of each message (IMAP, Google, JMAP, Exchange, Microsoft 365, files); a PST shows the size of its file, as an estimate. The migrations list shows "imported / total" under each progress bar, the page header the total across mailboxes, the mailbox panel the size, when it was measured and what was imported. Unknown stays "-", never 0. API: `source_size` on accounts, `bytes_ok` on jobs, `POST /api/accounts/{id}/size`, `size` in a source test.
- Connector interface for the mirror: `CAP_MIRROR`, `mirror_listing`, `exists`, `key_epoch` and `SourceItem.version` on sources, `CAP_INVENTORY`, `CAP_DELETE`, `CAP_UPDATE`, `CAP_SET_FLAGS` and `CAP_MOVE` on targets, `Changes.removed`. See [Connectors](docs/connectors.md#mirror-mode).
- Mirror safeguards: a deletion needs the source to confirm the item is gone, a mailbox is refused when more than `max_delete_percent` % or `min_delete_count` items would go (each block names its remedy), a folder that lists nothing is refused by itself unless `allow_empty_folders` names it, an item changed on the target since mccb wrote it is left alone (`target_modified`), shared DAV resources are never written over, every real mirror request is audited in the log, and the copies kept under `mirror-trash/` are private and purged after `MIRROR_TRASH_DAYS` days (30).

### Changed
- Mirror is now a choice of the Start dialog: for mailboxes that already ran, "Mirror: also carry over deletions and moves" starts one job per mailbox that first copies what is new at the source since the last run, then deletes and moves at the destination what was deleted or moved at the source, with the safety limit (percentage and minimum number of deletions) set right under it. Each job checks the limit by itself: a mailbox over it deletes nothing, still gets its new items, and says why in its log and its report (`too_many_deletes`, the job ends with errors). The mirror switch is turned on for the mailboxes that lack it, and "Compare first, without writing" opens the comparison.
- The mirror comparison no longer makes the whole selection wait: each mailbox is compared by its own request, four at a time, and shows as soon as it is answered ("5 of 17 mailboxes compared"); Apply runs the mailboxes already compared and ready, and "Start now" starts every selected mailbox at once without waiting (each job compares its own mailbox again). A comparison that fails is told and retried per mailbox. `POST /api/runs {mode: "mirror", dry_run: true}` with several mailboxes compares them in parallel too.
- Faster listings on IMAP, for the mirror and every pass: the messages of a folder are read in a few `UID FETCH` commands of up to 5000 messages each, written as exact UID ranges (`1:4980,4983,4990:9000`: never a message that was not asked), instead of one command per 500 (source) or 200 (mccb's copies on the target). The same light items as before (UID, flags, date, size, Message-ID), nothing marked read. Against GreenMail on a folder of 3000 mails: the source listing of a mirror pass 2.7 s -> 0.85 s, the inventory of mccb's copies on the target 5.9 s -> 0.57 s; servers that scan the folder for each command gain the most, and the gain grows with the folder.
- A mailbox whose first copy was stopped or failed half-way is no longer refused by the mirror (`no_prior_run`) when it already holds migrated items.

### Fixed
- Mirror logs of DAV calendars, contacts and tasks no longer carry hundreds of warnings on the first mirror pass after an earlier copy. Adopting the items an earlier (non-mirror) run copied is normal: the log now says it once per kind (`Mirror calendar: 412 item(s) copied by earlier runs adopted as they are on the target (first mirror pass)`), and a second line when some differ on the target (rewritten by the server, or edited there) and are left as they are. Items changed or gone at the source whose copy mccb cannot show it wrote ("no etag of its write") are likewise summarised in one line per kind, saying what to do. The per-item lines stay in the log at `DEBUG` level; the safety rules are unchanged (nothing mccb cannot prove it wrote is overwritten or deleted).
- CardDAV target based on MDaemon: contacts with a website (`URL`) are no longer refused. The server answers HTTP 500 to every vCard holding a `URL`, and mccb named one and the same contact of the address book as "already holding the UID" of each refused card, because that server answers an `addressbook-query` with every card of the collection, whatever its filter. A card the server refuses as it is is now also tried with its URLs without parameters, then with its URLs moved into its note (the website is kept in the note's text, photos and other fields too), before the minimal form; the item keeps a warning saying so. The form that worked is tried first, at once, for the next cards of the collection (one PUT more per card instead of seconds of retries and lookups). A UID conflict is only reported once the other object was read and has that very UID, and a collection that answers a query for a UID no object has is no longer queried. Once a few objects of a collection were refused in every form, the next ones no longer wait before their retry.
- A contact, event or task recorded as sent that its collection does not list (a server that answered 201 and did not keep it, or a copy deleted there since) is sent again by the next run, with a log line naming them (`recorded as sent are not on the target ... sent again`), instead of staying a `present < sent` verification error at every run.
- An IMAP source no longer copies the folders other mailboxes share with it into it. SmarterMail lists them in the same LIST as the account's own folders, under its other users' namespace (`Dossiers partagés/<owner>/Inbox`, `Shared Folders/...`), and its public folders under the shared one (`Dossiers publics/`): earlier versions copied them into each mailbox they were shared with as ordinary folders, so a mailbox that sees the inbox of a colleague got her unread mails in its own folders on the target ("999+ unread" there, while at the source they only show under the shared folders). They are now left out like the shared DAV collections, from the namespaces the server gives (`NAMESPACE`): each is migrated with the mailbox that owns it. The log says it once per folder and the report lists them (`folders_skipped`). `IMAP_INCLUDE_SHARED=1` copies them as before. The copies an earlier run made stay on the target: mccb never deletes mail there, the log says at every pass which target folder holds how many of them (`mail_left_on_target` in the report), to be deleted in the webmail.
- Read state on an IMAP target, again: the flags of the source are read again just before they are given to the copies when the listing of the pass is more than 5 minutes old, so a mail read at the source while a long pass ran arrives read too. A target whose `SEARCH HEADER` finds fewer of mccb's copies than expected (a server that does not index `X-PST-Import-Id`) is no longer trusted: every header of the folder is read and compared by mccb. The check is now said at the end of every pass, also when nothing was missing (`Read, answered and starred state: N of our mail(s) checked against the source in F target folder(s), M given the flags they lacked`), and the report gives `flags: {checked, fixed, not_kept, not_found, folders}`. A mail read at the source after the last pass is still only carried by the next one (a rerun or the final resync): run it once users stop working on the source.
- CalDAV / CardDAV targets: mccb now deletes by itself the collections earlier versions left behind, at the end of a normal run or of a final resync: the second "Contacts" (calendar, task list) next to the server's own, once each of its items is in the right one, and the copies of collections shared by another mailbox or public, which are now left to their owner (unless `DAV_INCLUDE_SHARED=1`). Rerunning the mailboxes is enough, nothing to delete by hand. Only a collection mccb created and holding nothing but those copies is deleted: anything else in it (a contact added in the webmail, an item not yet in its new collection, a listing that failed) keeps it, and the log says why (`collection_kept`); a collection of the server or of the user is never touched. On SmarterMail, which keeps one object per UID in the account, the items the old copy kept out of the right collection are moved first, and written back where they were if that collection still refuses them. A copy of every object is kept under `mirror-trash/` before anything is deleted; the log names each deleted collection with its number of items and the report lists them (`dav_removed`). Never in a mirror pass nor in a rerun of the errors only, and the source is only read.
- Accents in contacts, events and tasks: a CalDAV / CardDAV source is now read as the bytes it sends, never in the charset its answer names. A server declaring ISO-8859-1 (or another charset) for UTF-8 objects no longer turns "Loehlé" into "LoehlÃ©" on the target, and objects that are not UTF-8 (an old vCard 2.1 with `CHARSET=ISO-8859-1`, Windows-1252 text) are written in UTF-8 instead of losing their accents to "�". UTF-8 objects, the usual case, are still copied byte for byte. A name that already reads "LoehlÃ©" at the source is copied as it is: it is wrong at the source, and is to be fixed there.
- Read state on an IMAP target: mails that were read at the source could arrive unread. At the end of every pass (first pass, rerun, final resync, mirror), the mails mccb wrote are now given the read, answered, starred and draft flags their source has and they lack, with `UID STORE +FLAGS` on mccb's own copies only (exact `X-PST-Import-Id`), and the flags read back to warn when the target does not keep them. This repairs a target that ignores the flags of `APPEND`, a copy found on the target after an interruption instead of being written again, and a mail read or starred at the source after its copy, which reruns and the final resync never carried. Outside the mirror nothing is removed, keywords are not carried afterwards, and the source stays read-only (`EXAMINE`, `BODY.PEEK`, a light `UID FETCH (UID FLAGS)` on a delta pass). Already migrated mailboxes are repaired by a rerun or a final resync. The PST route records the flags it writes and repairs them the same way. The report gives `flags: {checked, fixed, not_kept}`.
- An IMAP source answering for one message in two FETCH responses (the flags apart) no longer gives a record without flags, and an unsolicited FETCH naming another message is ignored.
- CalDAV / CardDAV targets no longer get a second "Contacts" or "Calendar": the default address book, calendar or task list of the source goes to the one the target already has (same name whatever the case, else the one with a default name such as "Contacts", "Calendrier", "Kalender", else the first), the others to the collection of the same name, and lists such as SmarterMail's "Allowed Senders" / "Blocked Senders" to the target's own list under its local name ("Expéditeurs autorisés"...). A collection that grants no write (a global address list) is never chosen, a collection the server listed is never created over itself, and a name the target already has is never created a second time. The default collection of a DAV source is found by its name too, not only by being listed first. The job log says where each folder goes. After an earlier run that made a second collection, the next run copies those items into the right one (one already there is left as it is), deletes nothing, and names the extra collection in the log and the report (`collection_changed`, `dav_moved`) so it can be deleted by hand; the choice is then kept for the next runs. The PST path picks the default collection the same way when nothing was copied before.
- An IMAP source now brings the calendars, tasks and contacts of its server too, in the same job: when the IMAP host and port of the source are those of a server saved on the Servers page with CalDAV/CardDAV on, its CalDAV and CardDAV are read with the login and password of the source (same URL rules as for a destination: the URLs of that server, else discovery). Older mailboxes are matched by their host and port, nothing to set again. The mail keeps its keys and namespace: what was copied before is not copied again. A CalDAV or CardDAV that cannot be read does not stop the mail; the job ends with errors and says why. Mirror mode works on it as well.
- Calendars or contacts that are wanted but that the source cannot bring are no longer skipped in silence: the job log, the report and the plan say so and why (DAV off on the source server, no saved server for the IMAP address of the source, a source that only carries mail). The mailbox panel and the bulk edit say it too, with the fix (one click turns DAV on for the source server, all its mailboxes at once), and the bulk edit warns about a destination server with DAV off. The account API gives `source_carries` and `source_dav`.
- CalDAV / CardDAV errors seen migrating SmarterMail to SmarterMail. A 500 the target gives again for one object no longer costs a minute of retries per item (the run of the report took 7 hours for 410 contacts): the object is tried once more a moment later, then, while the collection still answers, it fails in a few seconds with its reason. When an object of the same UID is in another collection of the account (SmarterMail keeps one per UID: the second "Contacts" an earlier version made), the error names that collection and says to delete it then rerun; otherwise the object is written in a plainer form (vCard 3.0 with FN and N, CRLF, folded lines, an oversized photo left out; then without X- properties), which is logged and kept with the item. A 500 to `If-None-Match` over an existing resource is written over it as after a 412. A server in trouble (the PROPFIND fails too) is retried as before; a collection that refuses 20 objects in a row while it answers gets no more of them (one in 25 still tries), with one line for the collection.
- A CalDAV / CardDAV source no longer migrates the collections other mailboxes share with it nor the public ones of the domain (SmarterMail's `[compta@kmy.ch] - Calendrier`, `[Public] - kmy.ch/Contacts`, or what the server says: a `shared` mark, another `DAV:owner`): each is migrated with the mailbox that owns it. The log says it once per folder and the report lists them (`folders_skipped`). `DAV_INCLUDE_SHARED=1` migrates them.
- A DAV collection the target refuses to create is one folder error, said once with what to do (`create 'Tâches/24me' in the webmail then rerun`), its items in error: they no longer go to the default collection, nor give one error line each. A name with "/" (refused by SmarterMail with 409) is tried again as "Tâches - 24me", and a calendar refused by MKCALENDAR is tried with an extended MKCOL (RFC 5689).
- An empty source calendar or address book is no longer created on the target, and a collection where nothing was sent is no longer a verification error (`None present < 0 sent`).
- Two objects of one UID under two resource names in one collection are said when they are written; a target that keeps only one of them is no longer a verification error (`3688 present < 3689 sent`), and a collection missing some of our objects names them in the log. The count of our objects no longer depends on the case of their names.

## 2.2.0
mccb 2.2.0 simplifies the interface and makes it faster to work on many mailboxes at once: servers of any kind can be shared by mailboxes, lists of mailboxes can be imported and edited in bulk, and jobs can be paused, resumed or retried on a selection. Repair keeps untouched headers byte for byte, and the contacts of a PST are no longer dropped without a sign.

### Added
- A server of any kind (IMAP, Exchange, JMAP...) can be saved once on the Servers page and shared by mailboxes: it holds the address and the common settings, each mailbox keeps its own login and password. `/api/source-connections` accepts these kinds, and a connection lists the names of its stored secrets (`secret_names`).
- New migration takes several mailboxes at once, typed line by line or pasted as a list.
- List import in New migration: paste a list or drop a CSV file with a source and a destination column. A template can be downloaded.
- Bulk edit and bulk delete of mailboxes.
- Pause, Resume, Stop and Retry errors on a selection of jobs.
- Files: a "Migrate to..." button on each file, and bulk delete of files.
- The source of a mailbox, a file or a saved IMAP server, is picked in the mailbox panel.
- A warning before a migration that would skip calendars and contacts because CalDAV/CardDAV is turned off on the target server.
- Distribution lists of a PST are migrated as vCard groups.
- Duplicates also works on mailboxes added by hand, without a migration. The list is shared with Repair.
- Duplicates shows the date of the last analysis and a preview for each mailbox before anything is removed.
- Repair and the Repair report count messages without recipients, per folder and in total. They are only reported, never edited.
- The sidebar folds down to its icons, with Documentation and GitHub links.
- The interface is fully translated in nine languages.

### Changed
- Simpler interface: one "Add a server" picker for every kind of server, forms open in a side panel, page headers hold a title and icon buttons, and Settings is one page (notifications, automatic assignment and API tokens open in a panel).
- Repair and Duplicates are two tabs of one page, Maintenance.
- Duplicates: the action that removes messages is red, and Repair cannot start without an analysis.
- New migration is server to server only. Files are migrated from the Files page.
- CalDAV and CardDAV are turned on by default for new IMAP servers.
- Repair writes every header it does not change exactly as it was read, and checks every header of the repaired message. If one differs, the message is left alone and the error is recorded.
- The dark theme is more contrasted.
- Calm animations for pages, lists, toasts and dialogs. Reduced motion is respected.
- The "Live" label is gone: the interface only says when it is reconnecting.
- Short interface texts (toasts, empty states, status lines) no longer end with a period.

### Fixed
- Repair no longer re-encodes untouched headers: a To or Cc in raw UTF-8 or latin-1 was rewritten as `=?unknown-8bit?q?...?=`, and long address lists were folded differently.
- The Contacts folder of a PST is recognised even when one item is a distribution list or cannot be read. Before, its contacts were all ignored.
- A contact that fails (build, encoding or upload) is counted as an error and shown in the job, and the migration goes on with the next one.
- When CalDAV/CardDAV is turned off on the target server, a PST job says that contacts, tasks and notes were not migrated, instead of ending "done" with 0/0.
- Items of a PST that cannot be converted are skipped with their message class, summarised per class, and tried again on the next run.
- Control characters are removed from vCards, which SabreDAV refuses.
- Testing unsaved settings of a kind that has no directory answers 422 instead of 500.

## 2.1.0
mccb 2.1.0 migrates a whole Microsoft 365 tenant from one screen, after a two-click Connect with Microsoft, and adds a Duplicates tab. Calendars, address books and task lists now keep their own collections on CalDAV and CardDAV targets. The project moved to the selfhostr organisation: change `ghcr.io/yetysw/mccb` to `ghcr.io/selfhostr/mccb` in your `docker-compose.yml`.

### Added
- Microsoft 365 tenant as a source connection: one credential for many mailboxes, registered in Settings, Servers. See [Exchange](docs/exchange.md#migrate-a-microsoft-365-tenant).
- Listing of the mailboxes of a tenant, with their type (user, shared, room, equipment).
- Matching screen between the tenant and a target server: a state per mailbox (ready, linked, not on the server, no Microsoft 365 mailbox, other source, conflict), the coverage of mail, calendar, contacts and tasks for each, batch start and final resync from the same screen.
- On the matching screen, the destination of a tenant mailbox can be chosen by hand: another account of the server, or a new one typed with its password. The choice is kept until set back to automatic.
- The Test of a tenant names the missing permissions, required or optional.
- Mailboxes are linked by Entra object ID, so the final resync survives the removal of the domain from the tenant.
- API routes `/api/source-connections`.
- Duplicates tab: finds and removes duplicate messages, events and contacts in the mailboxes already known (migration accounts and repair mailboxes), on strict criteria, folder by folder, one copy of each kept. Analysis first (read-only), then a permanent removal after a confirmation; UID EXPUNGE only (refused without UIDPLUS), DELETE with If-Match for DAV. See [Remove duplicates](docs/duplicates.md). API routes `/api/dedup`.
- Connect with Microsoft: a Global Administrator signs in with a device code and mccb creates the "mccb migration" application, grants admin consent, adds a one-year secret and tests the tenant. See [Exchange](docs/exchange.md#connect-with-microsoft).

### Changed
- Calendars, address books and task lists to a CalDAV or CardDAV target: each source folder goes to its own collection (same name, or created), the default one to the default collection. Read-only calendars generated by Outlook or Google (holidays, birthdays) are no longer migrated. Events written to MDaemon are now found by the final check, which counts each target collection once. See [DAV](docs/dav.md).
- Listing a tenant needs the application permission `User.Read.All` (or `Directory.Read.All`), with admin consent. Migrating a single mailbox does not.
- The project moved to the selfhostr organisation: repository `github.com/selfhostr/mccb`, image `ghcr.io/selfhostr/mccb`, site and documentation on `mccb.selfhostr.io`. Old GitHub links redirect. To keep receiving updates, change `ghcr.io/yetysw/mccb` to `ghcr.io/selfhostr/mccb` in your `docker-compose.yml`.

### Fixed
- Final resync (delta) from Microsoft 365: Exchange Online refused the change query of mail (HTTP 400 "Parsing OData Select and Expand failed"). It now works after a full copy, including the first resync; the default contact folder, which has no change query, is listed in full.
- A CalDAV or CardDAV collection the server lists but will not open (seen on MDaemon) is no longer "created" again under the name "PST Import": the folder is reported with a clear error.
- A folder that could not be created on the target now counts in the final line and ends the job as done with errors, even when it holds no item.

## 2.0.0
mccb 2.0.0 grows from a PST migrator into a mailbox, calendar and contact migration tool: Exchange (EWS and Microsoft Graph), JMAP, Google and file sources join IMAP and CalDAV/CardDAV, with planning, a command line and a new web UI. A Repair tab detects and fixes damaged messages in mailboxes. It also scales much further on large machines. Read "Upgrading from 1.x" below before updating.

### Highlights
- Repair tab, see [Repair a damaged mailbox](docs/repair.md): its own list of mailboxes (by hand or CSV), an analysis that only reads, then a repair of the damage other migration tools leave (accents replaced by U+FFFD, mojibake, attachments without a real name, detached signature images). Each repaired message is checked before it is written, the originals are kept in the mailbox and can be restored.
- New web UI: three pages (Migrations, Files, Settings) instead of six, job details in a side panel with their own URL. Old links redirect.
- Exchange in both directions: `exchange_ews` for Exchange on premises, `m365_graph` for Exchange Online, with mail, calendar, contacts and tasks. Tested against simulated servers, not real Exchange: see docs/exchange.md.
- Planning (see [Plan, filter and control a migration](docs/planning.md)): dry run (`POST /api/runs/plan`), folder mapping with built-in names in nine languages, per-server and per-account policy (folder rules, excluded folders, date range, maximum size), allowed hours and a bandwidth limit per server, named batches with group actions, queue order, comparison of source and target (CSV, in the report), final resync (`mode: "delta"`) and a clear report of messages the target refuses for their size.
- API tokens and the `mccb` command line (upload with resume, run, watch, plan), see [Command line and API tokens](docs/cli.md).
- Files as a source: mbox, Maildir (zipped), `.eml`, `.msg`, `.ics`, `.vcf` and zip files, uploaded like a PST.
- JMAP (mail and contacts, as source and target) and Google (Gmail, Calendar, People, as source) connectors. Tested against simulated servers only, JMAP calendars are not carried: see [Protocols and file formats](docs/protocols.md).
- Server-side intake: a drop folder, and fetch over HTTP(S), SFTP or S3, see [Server-side intake](docs/server-intake.md). Notifications by ntfy, webhook or e-mail, see [Notifications](docs/notifications.md).
- Auto mode can use far more of a large machine: up to 32 mailboxes at once (more while cores stay idle), 32 builders and 32 connections per mailbox. `.env.example` no longer pins `WORKER_CONCURRENCY`: remove that line from an existing `.env` to get the auto mode. See [Tune](docs/tuning.md).

### Added
- Servers keep their last connection test (`last_test`, `last_test_result`) and show Verified or Check failed; each mailbox shows a check, a cross or a circle next to its address, with counters that filter the list. Mailboxes imported from a CSV are tested at once.
- Mailboxes CSV: optional columns (PST file name, content toggles, source settings) and an export that never contains a password. Old three-column files are read as before.
- One credential for many mailboxes: EWS impersonation, Graph app-only, Dovecot-style master user on IMAP. OAuth2 on IMAP (`imap_oauth2`).
- CalDAV and CardDAV can be sources. Tasks are carried to targets that can hold them.
- Exchange inbox rules converted to Sieve, best effort, uploaded over ManageSieve or offered as a script.
- Static demo of the interface, built with `npm run build:demo`: the real UI with a simulated API in the browser, fictional data only, nothing sent anywhere.
- README and documentation describe every source and target, not only PST files.

### Changed
- The interface, messages and documentation say "mailbox" everywhere. The API routes and fields and the `mccb accounts` command keep their names. CSV downloads are now named `mailboxes.csv` and `mailboxes-export.csv`.
- Live events update only what changed, at most once per frame. Rows are keyed and long lists are windowed, so clicks are not lost and open selects stay open during imports.
- Selection is explicit: what a filter hides is counted, shown and left out of any action. The selection bar is fixed and shifts nothing.
- Connection tests show a running state, cannot start twice, report IMAP, CalDAV and CardDAV, and stay visible after a reload.
- Upload queue: one file at a time by default (setting), order chosen when adding several files, reorder, pause, resume, cancel. Hashing no longer blocks the page.
- Dark and light themes, density, visible columns, mask addresses, nine languages, stored in the browser.
- Costs that grew with the mailbox: the progress is recounted less often on large mailboxes, the average size no longer reads the whole table at each update, item results are written in batches.
- Memory guard: builds go one at a time when memory is short. File cache no longer counts as used inside a container.
- Resume checks many items of a folder with one search. Final folder checks run once, no longer twice.
- Upload: `PUT` of a chunk no longer returns the list of received chunks, the `upload` event carries `received_count` instead, and hashing no longer blocks the web process.

### Fixed
- One network error no longer limits a job for good: the connection ceiling, the plateau and the fallback all recover with time.
- A server error now holds all the connections of the job and stops growth for every mailbox on that server. Servers answering "too many connections" with a plain NO are handled as overload. CalDAV and CardDAV honour `Retry-After`.

### Upgrading from 1.x
- Back up `./data` first, then `docker compose pull && docker compose up -d`, see [Upgrade mccb](docs/upgrading.md).
- `WORKER_CONCURRENCY` is no longer pinned in `.env.example`: remove that line from an existing `.env` to get the auto mode (up to 32 mailboxes at once).
- Servers created by earlier versions become Auto tuning when `workers` was a default; any other value stays Manual, see [Tune](docs/tuning.md).
- The web UI is reorganised: old links redirect, and view preferences (theme, density, columns, language) are stored in the browser.
- CSV downloads are now named `mailboxes.csv` and `mailboxes-export.csv`. Old three-column mailbox CSV files are read as before.
- API routes and fields and the `mccb accounts` command keep their names. `PUT` of an upload chunk no longer returns the list of received chunks: use `received_count` in the `upload` event.
- The session cookie was renamed in 1.1.2: if you come from 1.1.1 or earlier, everyone signs in again once.

### Dependencies
- cryptography 50.0.0, python-multipart 0.0.31.
- Tests: pytest 9.0.3.

## 1.1.2
- The product is now mccb (Mail Contact Calendrier Bridge): tab title, sidebar, login screen, logs, image title and the PRODID of exported vCards and events.
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
