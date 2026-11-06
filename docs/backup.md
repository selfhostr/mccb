# Back up and restore

By the end of this page you will know what to copy to keep a migration safe, and what a restore brings back.

## What to back up

Everything MCCB stores is under `./data` (see [Install MCCB](install.md#where-data-lives)). Back up the whole directory, and in particular:

- `app.db`: servers, accounts, uploads, jobs and the state of every item (`pending`, `inflight`, `ok`, `error`, `skipped`), which is what makes a resume possible;
- `secret.key`, or your `SECRET_KEY` value if you set one: without it, stored mailbox passwords can no longer be decrypted and must be entered again.

`app.db` is SQLite in WAL mode, shared by `web` and `worker`. Stop the stack before copying the directory (`docker compose stop`) so that the copy is consistent. This is a precaution, not something the code enforces.

## Restore

1. Stop the stack.
2. Put the saved `data` directory back, with the same `secret.key` or the same `SECRET_KEY` in `.env`.
3. Start it again: `docker compose up -d`.

Items already imported are recognised, so a job continues where it stopped (see [Pause, resume and retry](resume.md)). If the PST files are restored too, nothing has to be uploaded again.

> **Warning.** Keep `UPLOAD_CHUNK_SIZE` unchanged between backup and restore. A different value changes the fingerprint of the same PST file, which is then treated as a new one.
