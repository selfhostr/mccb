# Security policy

MCCB holds the IMAP and DAV passwords of the mailboxes it migrates and reads the PST files of your users. We take reports about it seriously.

## Supported versions

| Version | Supported |
|---|---|
| `main` | Yes |
| Latest tagged release (`latest` image) | Yes |
| Older tags | No, upgrade |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub's private vulnerability reporting: open the repository's **Security** tab, then **Report a vulnerability** (<https://github.com/yetysw/mccb/security/advisories/new>).

Include what you can of: the affected component, steps to reproduce or a proof of concept, the impact as you understand it, and the version or commit you tested.

## What to expect

- An acknowledgement within **7 days**.
- A first assessment (confirmed, not a vulnerability, or need more information) within **14 days**.
- A fix on `main`, a tagged release and a published advisory. Disclosure is coordinated: the advisory stays private until a fix is released. Reporters are credited unless they prefer not to be.

This is a volunteer-maintained project; there is no bug bounty.

## Scope

In scope:

- Authentication, sessions and the login rate limit.
- Encryption of stored mailbox passwords and the handling of `SECRET_KEY` and `data/secret.key`.
- The upload endpoints and the handling of untrusted PST files.
- Requests made to the IMAP, CalDAV and CardDAV servers you configure.
- The Docker image and Compose files: defaults that expose more than they should.

Out of scope: vulnerabilities in the servers you migrate to, in the native PST library itself (report those upstream), findings that require an attacker who already controls the host or the `./data` volume, and reports from automated scanners without a demonstrated impact.

## Hardening notes for operators

- Port 22362 speaks plain HTTP. Put MCCB behind a reverse proxy with TLS and publish only the proxy, for example `127.0.0.1:22362:22362` in the compose file. Set `COOKIE_SECURE=1`.
- Back up `./data` including `secret.key`, and keep it out of screenshots.
- Disable TLS verification of an IMAP server only for a server you control.
- Delete uploaded PST files and the data directory when a migration is finished.
