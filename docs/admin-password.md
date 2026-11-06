# Change or reset the admin password

By the end of this page you will know where the admin password comes from, and how to change or reset it.

- **`ADMIN_PASSWORD` empty or absent (default):** a random password is generated on first start, stored
  hashed in `data/admin.json` and printed once in the `web` container log (`docker compose logs web`) as a
  boxed banner containing `Admin password: XXXX`.
- **`ADMIN_PASSWORD` set:** the environment variable always wins over `data/admin.json`.

To change it:

- from the UI (**Settings** page, "Change admin password") or via `POST /api/password {current, new}` (minimum 8 characters). This answers 409 if
  `ADMIN_PASSWORD` is set in the environment. Other sessions are invalidated and the caller gets a fresh
  cookie;
- to reset a lost password: delete `data/admin.json` and restart (`docker compose restart web`); a new
  password is generated and printed in the log. Or set `ADMIN_PASSWORD` in `.env` and recreate the
  containers with `docker compose up -d` (a plain `restart` does not re-read `env_file`).

Changing the password **invalidates** existing sessions (the session cookie embeds a fingerprint of the
admin password): you must log in again with the new password.

After 5 failed logins within 5 minutes from the same IP, `POST /api/login` answers 429. Each failure
is delayed by 0.5 s. The counter lives in the memory of the `web` process: a restart resets it.
