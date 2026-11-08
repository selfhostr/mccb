<div align="center">
<a href="https://mccb.selfhostr.io"><img src=".github/assets/banner.svg" alt="mccb: migrate mail, calendars and contacts between servers" width="100%"></a>

**Migrate mailboxes with their calendars and contacts between servers, in bulk, from a web dashboard.**

[Site](https://mccb.selfhostr.io) · [Docs](https://mccb.selfhostr.io/docs/) · [Demo](https://mccb.selfhostr.io/demo/) · [Changelog](CHANGELOG.md)

[![CI](https://github.com/selfhostr/mccb/actions/workflows/tests.yml/badge.svg)](https://github.com/selfhostr/mccb/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
</div>

## Overview

mccb (Mail Contact Calendrier Bridge) moves mail, calendars, contacts and tasks from a source to a target, one mailbox at a time, and runs as many mailboxes in parallel as the machine and the servers allow. It started as a PST importer and now reads and writes the usual protocols. Free, for good: MIT licence, no paid edition.

## Quick start

```bash
mkdir mccb && cd mccb
curl -fsSLO https://raw.githubusercontent.com/selfhostr/mccb/main/docker-compose.yml
docker compose up -d
docker compose logs web | grep -A2 "Admin password"
```

Open `http://<host>:22362` and sign in with the printed password (shown once). Port 22362 speaks plain HTTP: put it behind a reverse proxy with HTTPS. Back up `./data`, including `secret.key`: without it the stored mailbox passwords cannot be decrypted. More in [install](docs/install.md).

## Features

- **Many sources.** PST and OST, mbox, Maildir, `.eml`, `.msg`, `.ics`, `.vcf`, IMAP, CalDAV, CardDAV, Exchange, Microsoft 365, JMAP, Google: [connectors](docs/connectors.md).
- **Built for batches.** Add mailboxes one by one or by CSV, run them all, read one report per mailbox.
- **Built for big sources.** Resumable uploads, a drop folder, server-side fetch, preview and dry run.
- **Safe to interrupt.** Pause, stop, crash or restart: a resume checks the target and never duplicates an item.
- **Plan and control.** Folder mapping, date and size filters, allowed hours, bandwidth limit, final resync: [planning](docs/planning.md).
- **Command line.** `mccb` uploads with resume, plans, runs and watches with API tokens: [cli](docs/cli.md).
- **Notifications.** ntfy, webhook or e-mail when a migration finishes: [notifications](docs/notifications.md).

## Architecture

One image, two services: `web` (FastAPI and a static UI, port 22362) and `worker` (job queue). Every job and every PST analysis runs in its own subprocess, so a crash of the native PST library cannot take the worker down. State is SQLite and files in `./data`. See [how it works](docs/how-it-works.md).

## Limits

- Exchange, Microsoft 365 and Google were tested against simulated servers only, never the real services. Try one mailbox first.
- JMAP calendars are not carried: use CalDAV with the same provider.
- One administrator and SQLite on a local disk: one host, no high availability.
- A source is read in one pass, not synchronised: later changes at the source are not carried over.
- Results depend on the target IMAP and DAV server. More in [known limitations](docs/limits.md).

## Contributing

Issues and pull requests are welcome: [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). Provided "as is", without warranty of any kind.

## Support

mccb is free and has no company behind it. If it saves you time: [sponsor us on GitHub](https://github.com/sponsors/selfhostr).
