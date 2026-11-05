# Changelog

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
