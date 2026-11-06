<div align="center">

<a href="https://github.com/yetysw/mccb"><img src=".github/assets/banner.svg" alt="MCCB: migrate Outlook PST files to IMAP, CalDAV and CardDAV" width="100%"></a>

<h3>Bulk-migrate Outlook PST files to IMAP, CalDAV and CardDAV from a web dashboard.</h3>

[![Docker image](https://github.com/yetysw/mccb/actions/workflows/docker.yml/badge.svg)](https://github.com/yetysw/mccb/actions/workflows/docker.yml)
[![Tests](https://github.com/yetysw/mccb/actions/workflows/tests.yml/badge.svg)](https://github.com/yetysw/mccb/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

[Website](https://www.yety.ch) · [Documentation](docs/index.md) · [Releases](https://github.com/yetysw/mccb/releases)

## Overview

MCCB (Mail Contact Calendrier Bridge) imports Outlook PST (and OST) files into the mailboxes you host: mail over IMAP, appointments over CalDAV, contacts over CardDAV.

- **Built for batches.** Add a server, add accounts one by one or by CSV, attach a PST to each, run them all and read one report per mailbox.
- **Built for big files.** Resumable uploads of large files, with a preview of accents and attachments before anything is written.
- **Safe to interrupt.** Pause, stop, crash or restart: a resume checks the server and never duplicates a message.

Free, for good. MIT licence, no paid edition.

## Quick start

```bash
mkdir mccb && cd mccb
curl -fsSLO https://raw.githubusercontent.com/yetysw/mccb/main/docker-compose.yml
docker compose up -d
docker compose logs web | grep -A2 "Admin password"
```

1. Open `http://<host>:22362` and sign in with the printed password (shown once).
2. Add a server, then accounts, upload a PST, assign it, run.
3. Put it behind a reverse proxy with HTTPS: port 22362 speaks plain HTTP.

Back up `./data`, including `secret.key`: without it the stored mailbox passwords cannot be decrypted. Details: [install](docs/install.md), [backup](docs/backup.md), [upgrading](docs/upgrading.md).

## Features

- Mail to **IMAP**, calendars to **CalDAV**, contacts to **CardDAV**, with automatic DAV discovery.
- Every message is verified, and every language and charset is preserved.
- Newest-first import across all folders.
- Auto or manual tuning of message builders and IMAP connections per server.
- Live progress, log and JSON report per mailbox.
- Account import by CSV (`email;password[;login]`).

## Architecture

One image, two services: `web` (FastAPI and a static UI, port 22362) and `worker` (job queue). Every job and every PST analysis runs in its own subprocess, so a crash of the native PST library cannot take the worker down. State is SQLite and files in `./data`. See [specification](docs/specification.md).

## Limits

- One administrator and SQLite on a local disk: one host, no high availability.
- A PST that is corrupted can crash the native library; affected items are skipped and reported.
- Encrypted or password-protected PSTs have no dedicated handling.
- Items with no charset information may decode imperfectly in some languages.
- Results depend on the target IMAP and DAV server.

More in [known limitations](docs/limits.md).

## Status

Version 1.1.x. Every change is listed in the [changelog](CHANGELOG.md).

## Contributing

Bug reports and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) (Docker only, no host toolchain). Security issues: [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE). MCCB is provided "as is", without warranty of any kind. Validate a migration on a test mailbox before running it on production data.

## Support MCCB

MCCB is free. If it saves you time, you can support its development:

[Sponsor us on GitHub](https://github.com/sponsors/yetysw), recurring or one-off.
