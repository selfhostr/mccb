# Command line and API tokens

By the end of this page you will push a PST file and start migrations from a NAS or a server, with no browser.

## API tokens

A token is a bearer credential for scripts. It works next to the session cookie, with the same rights, except that a token cannot create or revoke tokens. There is still a single administrator.

- `POST /api/tokens {name}` (from a logged-in session) returns the token once: `mccb_<8 hex>_<43 characters>`. Only its SHA-256 and its short prefix (`mccb_xxxxxxxx`) are stored. Keep it like a password.
- `GET /api/tokens` lists `{id, name, prefix, created_at, last_used_at, revoked_at}`.
- `DELETE /api/tokens/{id}` revokes it at once.
- Use it as `Authorization: Bearer <token>` on any `/api` route.

Changing the admin password does not revoke tokens: revoke them yourself.

## The `mccb` command

`mccb` is one Python file that uses the standard library only (Python 3.8 or later). It is in the image:

```bash
docker compose exec web mccb status
```

Copy it to any machine to push from there:

```bash
docker compose cp web:/app/app/cli/mccb.py ./mccb && chmod +x mccb
export MCCB_URL=https://mccb.example.org MCCB_TOKEN=mccb_xxxxxxxx_...
./mccb upload /volume1/mail/jane.pst --account 12 --wait
```

| command | what it does |
|---|---|
| `mccb status` | server version, worker alive or not, active jobs |
| `mccb accounts [--server ID]` | mailboxes with their status and last job |
| `mccb jobs [--active] [--limit N] [--json]` | jobs with progress |
| `mccb run ID... [--name N] [--delta] [--priority N] [--watch]` | start migrations, as a named batch if `--name` |
| `mccb watch JOB [--logs] [--interval S]` | follow a job until it ends |
| `mccb plan ID... [--json]` | dry run: counts, sizes, quota, folders to create |
| `mccb upload FILE [--account ID] [--wait]` | send a PST, resumable |

Options before the command: `--url` (or `MCCB_URL`), `--token` (or `MCCB_TOKEN`), `--token-file` (keeps the token out of the process list), `--insecure` (no TLS verification).

**Upload with resume.** The file is sent in the chunks the server asks for, each with its SHA-256, retried with a growing pause. Run the same command again after an interruption: the server knows the file by name, size and modification time and only the missing chunks are sent. A file already on the server sends nothing. With `--account` the PST is linked to the mailbox once it is valid; `--wait` waits for the analysis.

**Exit codes.** `watch` and `run --watch`: 0 done, 1 done with errors, 2 failed, 3 stopped, 130 interrupted. Other commands: 0 on success, 4 on an API, network or argument error. A script can therefore chain `mccb upload ... && mccb run ... --watch`.
