# mccb

mccb (Mail Contact Calendrier Bridge) migrates mail, calendars, contacts and tasks from one place to another, from a web dashboard. It is built for batches of mailboxes and for very large sources (resumable uploads, a drop folder, a command line).

- **Sources:** Outlook PST and OST files, mbox, Maildir, `.eml`, `.msg`, `.ics`, `.vcf` and zip files, IMAP, CalDAV, CardDAV, Exchange on premises, Exchange Online (Microsoft 365), JMAP and Google.
- **Targets:** IMAP, CalDAV, CardDAV, Exchange on premises, Exchange Online and JMAP. By default a mailbox writes to the IMAP and DAV server it belongs to.

Start here:

- [Install mccb](install.md) with Docker Compose and sign in.
- [Migrate a mailbox](migrate.md): add a server, add mailboxes, pick a source, run.
- [Connectors](connectors.md) and [Protocols and file formats](protocols.md): what each source and target moves.
- [Pause, resume and retry](resume.md): how an interrupted import continues without duplicates.
- [Repair a damaged mailbox](repair.md): broken accents, attachments and signature images left by another tool.
- [Command line and API tokens](cli.md), [Server-side intake](server-intake.md) and [Notifications](notifications.md): work without watching a browser.

mccb is MIT licensed, with no paid edition. Exchange, Microsoft 365 and Google were tested against simulated servers only. Read [Known limitations](limits.md) before relying on it.
