<div align="center">

<a href="https://mccb.open.yety.ch"><img src=".github/assets/banner.svg" alt="mccb: migrate mail, calendars and contacts between servers" width="100%"></a>

<h3>Migrate mailboxes with their calendars and contacts between servers, in bulk, from a web dashboard.</h3>


[![CI](https://github.com/yetysw/mccb/actions/workflows/tests.yml/badge.svg)](https://github.com/yetysw/mccb/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

Screenshots: [screenshots/](screenshots/)

## Overview

mccb (Mail Contact Calendrier Bridge) moves mail, calendars, contacts and tasks from a source to a target, one mailbox at a time, and runs as many mailboxes in parallel as the machine and the servers allow. It started as a PST importer and now reads and writes the usual protocols.

- **Built for batches.** Add servers, add mailboxes one by one or by CSV, choose a source and a target for each, run them all and read one report per mailbox.
- **Built for big sources.** Resumable uploads, a drop folder and server-side fetch for large files, a preview and a dry run before anything is written.
- **Safe to interrupt.** Pause, stop, crash or restart: a resume checks the target and never duplicates an item.

More than 500 GB of mail already migrated without errors, with calendars and contacts.

Free, for good. MIT licence, no paid edition.

Site: [mccb.open.yety.ch](https://mccb.open.yety.ch). Documentation: [mccb.open.yety.ch/docs](https://mccb.open.yety.ch/docs/). Demo in your browser, with fictional data: [mccb.open.yety.ch/demo](https://mccb.open.yety.ch/demo/).

## Quick start

```bash
mkdir mccb && cd mccb
curl -fsSLO https://raw.githubusercontent.com/yetysw/mccb/main/docker-compose.yml
docker compose up -d
docker compose logs web | grep -A2 "Admin password"
```

1. Open `http://<host>:22362` and sign in with the printed password (shown once).
2. On **Servers**, add the destination server. Then **New migration**: pick the source (a PST or another file, or a live server such as IMAP or Microsoft 365) and the mailbox it goes to, and start it from its panel.
3. Put it behind a reverse proxy with HTTPS: port 22362 speaks plain HTTP.

Back up `./data`, including `secret.key`: without it the stored mailbox passwords cannot be decrypted. Details: [install](docs/install.md), [backup](docs/backup.md), [upgrading](docs/upgrading.md).

## What it does

| Kind | Source | Target | Items |
|---|---|---|---|
| `pst` (PST and OST) | yes | | mail, calendar, contacts |
| `files` (mbox, Maildir, `.eml`, `.msg`, `.ics`, `.vcf`, `.zip`) | yes | | mail, calendar, contacts |
| `imap`, `imap_oauth2` | yes | yes | mail |
| `caldav`, `carddav` | yes | yes | calendar and tasks, contacts |
| `exchange_ews` (on premises), `m365_graph` (Exchange Online) | yes | yes | mail, calendar, contacts, tasks |
| `jmap` | yes | yes | mail, contacts |
| `google` (Gmail, Calendar, People) | yes | | mail, calendar, contacts |

Details in [Connectors](docs/connectors.md) and [Protocols and file formats](docs/protocols.md).

- **Command line.** `mccb` uploads with resume, plans, runs and watches from a NAS or a server, with API tokens: [Command line](docs/cli.md).
- **Drop folder.** Copy files into `data/inbox`, or have the server fetch them over HTTP(S), SFTP or S3: [Server-side intake](docs/server-intake.md).
- **Notifications.** ntfy, webhook or e-mail when uploads, analyses and migrations finish: [Notifications](docs/notifications.md).
- **Plan and control.** Dry run, folder mapping, date and size filters, allowed hours, bandwidth limit, named batches, comparison of source and target, final resync: [Plan, filter and control](docs/planning.md).
- **Safe to repeat.** Every item is verified, every language and charset is preserved, newest first.
- **Tuning.** Auto or manual, per server: message builders and connections.

## Architecture

One image, two services: `web` (FastAPI and a static UI, port 22362) and `worker` (job queue). Every job and every PST analysis runs in its own subprocess, so a crash of the native PST library cannot take the worker down. State is SQLite and files in `./data`. See [How it works](docs/how-it-works.md).

## Limits

- **Exchange, Microsoft 365 and Google were tested against simulated servers only**, written from the vendors' documentation, never against the real services. Try one mailbox with few items first: [Exchange](docs/exchange.md), [Protocols](docs/protocols.md).
- **Graph (Microsoft 365).** Mail goes in through a beta API, or as drafts through the stable one, and the received date becomes the date of the import. Graph mails every attendee of an event it creates, so attendees are written as text by default. Archive mailboxes are not reachable. Upload limits apply: about 150 MB per mailbox per 5 minutes.
- **JMAP calendars are not carried.** JMAP Calendars is still an Internet-Draft. Use CalDAV with the same provider.
- **Google.** A mail with several labels is copied once, in one folder. Contacts are the saved contacts only.
- One administrator and SQLite on a local disk: one host, no high availability.
- A corrupted PST can crash the native library; affected items are skipped and reported. Encrypted or password-protected PSTs have no dedicated handling.
- A source is read in one pass, not synchronised: a change made at the source after its copy is not carried over.
- Results depend on the target IMAP and DAV server.

More in [known limitations](docs/limits.md).

## Status

Version 2.0.x. Every change is listed in the [changelog](CHANGELOG.md).

## Contributing

Bug reports and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) (Docker only, no host toolchain). Translations of the interface are in `web/messages`. Security issues: [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). mccb is provided "as is", without warranty of any kind. Validate a migration on a test mailbox before running it on production data.

## Support mccb

mccb is free. If it saves you time, you can support its development:

[Sponsor us on GitHub](https://github.com/sponsors/yetysw), recurring or one-off.
