# Development

By the end of this page you will have MCCB running from source and its test suite passing, all in Docker.

```bash
git clone https://github.com/yetysw/mccb.git && cd mccb
docker compose up -d --build
docker compose -f docker-compose.test.yml run --rm --build tests
```

Tests run in containers: GreenMail (IMAP), Radicale (CalDAV/CardDAV) and the
application image with pytest (engine, server and e2e suites). They use a fake engine
(`tests/server/fake_engine.py`) that honors the ENGINE contract for server tests.

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v      # cleanup
```

Unit test of the web code (chunked sending), with nothing to install:

```bash
docker run --rm -v "$PWD":/w -w /w node:22-slim node --test 'tests/web/*.test.mjs'
```

Coverage: CRUD, authentication, SSE, CSV import (`;` and `,`, optional header, UTF-8, cp1252, BOM),
resumable upload (chunk with wrong hash → 422, resume, identical final file, disk space
check), full import, root folder, pause and resume, stop then retry without duplicates, simulated
crash with `inflight` items and an orphaned job, simulated libpff crash (`PSTMIG_TEST_CRASH`),
retry of errors only. Without `TEST_IMAP_HOST` or `TEST_DAV_URL`, integration tests are skipped.
