# Development

By the end of this page you will have mccb running from source and its test suite passing, all in Docker.

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

Web UI (`web/`): type check, translations, unit tests and build, in a Node container. The build goes to `app/web/`, generated and not committed:

```bash
docker run --rm -v "$PWD":/w -w /w/web node:26-slim sh -c "npm ci && npm run check && npm test && npm run build"
```

Browser tests (`tests/ui/`) drive that build against a mock API (`tests/ui/mock-api.mjs`, password `admin`). They need Playwright's Chromium: `npx playwright install --with-deps chromium`, then `npm run test:ui`. For UI work, `npm run mock` and `MCCB_DEV_API=http://127.0.0.1:4173 npm run dev` run without the backend.

The static demo (`/demo/` on the site) is the same UI built with `npm run build:demo` into `web/dist-demo/`. The mock API of the tests (`web/src/demo/mock-core.mjs`) runs in the visitor's browser instead of behind HTTP: `fetch` and `EventSource` are replaced, so no request leaves the page. Its headers are in `web/demo-headers`, and `npm run test:demo` serves the build with them and fails on a request to another origin or a policy violation.

Coverage: CRUD, authentication, SSE, CSV import (`;` and `,`, optional header, UTF-8, cp1252, BOM),
resumable upload (chunk with wrong hash → 422, resume, identical final file, disk space
check), full import, root folder, pause and resume, stop then retry without duplicates, simulated
crash with `inflight` items and an orphaned job, simulated libpff crash (`PSTMIG_TEST_CRASH`),
retry of errors only. Without `TEST_IMAP_HOST` or `TEST_DAV_URL`, integration tests are skipped.
