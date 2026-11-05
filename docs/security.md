# Security

This page lists what mccb protects, and what it does not.

## What it does

- **One admin password.** Stored as a scrypt hash in `data/admin.json` (file mode 600), or taken from `ADMIN_PASSWORD`. A generated password is printed once.
- **Sessions.** A signed cookie, `HttpOnly`, `SameSite=Lax`, and `Secure` when `COOKIE_SECURE=1` or the request arrives over HTTPS. Lifetime is `SESSION_MAX_AGE` (7 days by default). Changing the password invalidates every other session.
- **Login throttling.** After 5 failed logins within 5 minutes from the same IP, `POST /api/login` answers 429, and each failure is delayed by 0.5 s. The counter is in memory and resets when `web` restarts.
- **Mailbox passwords** are encrypted with Fernet. The key comes from `SECRET_KEY`, or from `data/secret.key` when it is empty.
- **Uploads.** Each chunk is verified by SHA-256 on the server, and the PST signature is checked before the scan.
- **Isolation.** Every job and every PST analysis runs in a subprocess, so a crash of the native PST library kills only that subprocess.

## What it does not do

- There are no accounts or roles: anyone with the admin password can do everything.
- Mailbox passwords can be decrypted by the application. Protect `./data` and `SECRET_KEY`.
- The server speaks plain HTTP. HTTPS is the job of your [reverse proxy](reverse-proxy.md), which also must filter port 22362.
- TLS verification of an IMAP server can be turned off per server. Do that only for a server you control.
- The login throttle is per IP and in memory. Behind a proxy that does not forward `X-Forwarded-For`, all requests share the proxy's IP.

## Report a vulnerability

See [SECURITY.md](https://github.com/yetysw/mccb/blob/main/SECURITY.md).
