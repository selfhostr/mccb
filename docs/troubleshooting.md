# Troubleshooting

Each entry gives a symptom and what to check. All of it comes from how MCCB behaves; see the linked pages for the details.

## I lost the admin password

Delete `data/admin.json` and run `docker compose restart web`: a new password is generated and printed in the log. Or set `ADMIN_PASSWORD` in `.env` and run `docker compose up -d` (a plain `restart` does not re-read `env_file`). See [Change or reset the admin password](admin-password.md).

## Login answers 429

Five failed logins within five minutes from one IP. Wait, or restart `web` (the counter is in memory).

## Mailbox passwords no longer work

The error "password cannot be decrypted (SECRET_KEY changed?)" means the key differs from the one used when they were stored. Restore the old `data/secret.key` or `SECRET_KEY`, or enter the passwords again.

## Progress does not update in the browser

Live updates use server-sent events on `/api/events`. Your proxy must not buffer them. See [Put MCCB behind a reverse proxy](reverse-proxy.md).

## Uploads are slow or fail

The proxy must not buffer request bodies, must allow bodies of at least `UPLOAD_CHUNK_SIZE`, and needs long timeouts. A chunk with a wrong checksum is refused with 422 and retried. A 507 answer means the disk is full.

## A PST is `invalid`

The reason is shown on the Files page. A file without the `!BDN` signature is refused, and a crash of the native library during the scan gives "libpff crash during analysis". Encrypted or password-protected PSTs have no dedicated handling.

## A job fails with "The subprocess crashed N times"

The native PST library crashed `MAX_CHILD_CRASHES` times on the same job (3 by default). The faulty items are marked "libpff crash" and skipped. Use **retry errors** once to try them again. See [Pause, resume and retry](resume.md).

## Mail is duplicated after re-uploading a PST

`UPLOAD_CHUNK_SIZE` changed between the two uploads, so the same file got a different fingerprint. Keep it constant during a whole migration. See [Known limitations](limits.md).

## `/api/health` says the worker is not alive

The worker heartbeat is older than 60 seconds. Check `docker compose logs worker` and `docker compose ps`. A job left `running` by a dead worker is requeued automatically when the worker starts, then every 15 seconds.
