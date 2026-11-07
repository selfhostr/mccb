# Security

This page lists what mccb protects, and what it does not.

## What it does

- **One admin password.** Stored as a scrypt hash in `data/admin.json` (file mode 600), or taken from `ADMIN_PASSWORD`. A generated password is printed once.
- **Sessions.** A signed cookie, `HttpOnly`, `SameSite=Lax`, and `Secure` when `COOKIE_SECURE=1` or the request arrives over HTTPS. Lifetime is `SESSION_MAX_AGE` (7 days by default). Changing the password invalidates every other session.
- **API tokens.** Bearer tokens for scripts (see [Command line and API tokens](cli.md)): 256 random bits, only the SHA-256 is stored, shown once, revocable. A token has the rights of the administrator but cannot create or revoke tokens.
- **Login throttling.** After 5 failed logins within 5 minutes from the same IP, `POST /api/login` answers 429, and each failure is delayed by 0.5 s. The counter is in memory and resets when `web` restarts.
- **Mailbox passwords** are encrypted with Fernet. The key comes from `SECRET_KEY`, or from `data/secret.key` when it is empty.
- **Uploads.** Each chunk is verified by SHA-256 on the server, and the PST signature is checked before the scan.
- **Isolation.** Every job and every PST analysis runs in a subprocess, so a crash of the native PST library kills only that subprocess.
- **Server-side fetch.** The server refuses link-local addresses (169.254.0.0/16, fe80::/10, so the cloud metadata
  service), well-known metadata addresses, unspecified, multicast and reserved ranges. Names are resolved once, every
  address is checked, the connection goes to the checked address, and every redirect is checked again. Credentials are
  not sent to another host after a redirect. Private ranges and loopback are allowed because mccb is self-hosted and sources
  are often on the LAN; set `FETCH_BLOCK_PRIVATE=1` to refuse them too. Fetch credentials and notification secrets are
  encrypted like mailbox passwords and never returned by the API.
- **Notification targets** follow the same address policy (metadata and link-local refused).

## What it does not do

- There are no accounts or roles: anyone with the admin password, or with an API token, can do everything. Tokens do not expire and failed token attempts are not throttled (the secret is 256 random bits).
- Mailbox passwords can be decrypted by the application. Protect `./data` and `SECRET_KEY`.
- The server speaks plain HTTP. HTTPS is the job of your [reverse proxy](reverse-proxy.md), which also must filter port 22362.
- TLS verification of an IMAP server can be turned off per server. Do that only for a server you control.
- The login throttle is per IP and in memory. Behind a proxy that does not forward `X-Forwarded-For`, all requests share the proxy's IP.
- The address policy of server-side fetch limits what an administrator can reach from the server, it is not a firewall.
  Anyone with the admin password can make the server connect to any allowed address on your network.
- SFTP without `host_key_sha256` trusts the first key seen and then pins it. Provide the fingerprint to be safe from the first connection.
- Fetch and notification secrets can be decrypted by the application, like mailbox passwords.

## Report a vulnerability

See [SECURITY.md](https://github.com/yetysw/mccb/blob/main/SECURITY.md).
