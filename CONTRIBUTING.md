# Contributing to MCCB

Bug reports, fixes and improvements are welcome. If you are not sure something is wanted, open an issue first: a paragraph is enough.

## Development setup

Everything runs in Docker. No Python toolchain is needed on the host.

```bash
git clone https://github.com/yetysw/mccb.git && cd mccb
docker compose up -d --build        # UI on http://localhost:22362
docker compose logs web             # the generated admin password
```

## Tests

The suites (engine, server, end to end) run in containers with GreenMail (IMAP) and Radicale (CalDAV and CardDAV):

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v      # cleanup
```

The unit test of the web upload code needs nothing installed:

```bash
docker run --rm -v "$PWD":/w -w /w node:22-slim node --test 'tests/web/*.test.mjs'
```

CI (`.github/workflows/tests.yml`) runs the first command on every pull request and every push to `main`. Run it before opening a pull request. See [Development](docs/development.md) and [Specification](docs/specification.md).

## Branches and commits

- Branch from `main` with `feat/...`, `fix/...`, `docs/...` or `chore/...`, and open a pull request against `main`. Never push to `main` directly.
- Conventional Commits for commit messages and pull request titles, for example `fix(runner): requeue orphaned jobs`.
- One topic per pull request.
- New behaviour and bug fixes come with tests; a bug fix with a regression test that fails without the fix.
- Update `docs/` and `CHANGELOG.md` when behaviour changes.

## Conventions

- Python code follows the existing style; the engine (`app/engine/`) stays pure, with no network access.
- The web UI is static (`app/web/`), with vendored JavaScript: no Node build step.
- Never log or return a mailbox password.
- Pull requests that change the UI include before and after screenshots.

## Developer Certificate of Origin

Contributions are accepted under the project licence (MIT) and must be signed off to certify the [Developer Certificate of Origin](https://developercertificate.org/): that you wrote the change or have the right to submit it under that licence. Add a `Signed-off-by: Your Name <you@example.com>` line to each commit with `git commit -s`.

## Keep the repository clean

Never commit secrets, captured data, PST files with real content, real hostnames or IP addresses. Use the documentation ranges (`192.0.2.0/24`, `example.com`). Test fixtures must be public files; their sources are listed in `tests/fixtures/SOURCES.txt`.

## Security

Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).
