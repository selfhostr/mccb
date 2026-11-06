# Changelog

## Unreleased
- Exchange in both directions: `exchange_ews` for Exchange on premises, `m365_graph` for Exchange Online, with mail, calendar, contacts and tasks. Tested against simulated servers, not real Exchange: see docs/exchange.md.
- One credential for many mailboxes: EWS impersonation, Graph app-only, Dovecot-style master user on IMAP. OAuth2 on IMAP (`imap_oauth2`).
- CalDAV and CardDAV can be sources. Tasks are carried to targets that can hold them.
- Exchange inbox rules converted to Sieve, best effort, uploaded over ManageSieve or offered as a script.
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
