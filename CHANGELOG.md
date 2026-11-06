# Changelog

## 1.1.2
- The product is now MCCB (Mail Contact Calendrier Bridge): tab title, sidebar, login screen, logs, image title and the PRODID of exported vCards and events.
- The session cookie is renamed: everyone signs in again once after the upgrade.
- README and documentation restructured per the yety guidelines (docs/ pages, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue templates).

## 1.1.1
- Mail order and IMAP INTERNALDATE now use the same date (`mail_date`): the Date header only if it has a timezone and is plausible, otherwise MAPI submit, delivery, creation or modification time.
- Finished jobs show the average speed (items/s and MB/s).
- Log line "Indexing mail items (newest first)…".

## 1.1.0
- Version shown in the UI and at `/api/version`.
- Newest-first import for mail, calendar and contacts.
- Auto tuning by default (machine probe, AIMD control of IMAP connections), with a Manual override.
- Parallel message builders; about 2-4x faster.

## 1.0.0
- Initial release.
