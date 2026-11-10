# Repair a damaged mailbox

By the end of this page you will have checked a mailbox that another migration tool damaged, and repaired it with every original kept aside.

The **Repair** tab works on mailboxes that are already on an IMAP server. It needs no PST or other source file. Its list is separate from the migration accounts (the [Duplicates](duplicates.md) tab shows it too): a customer who calls about a broken mailbox does not have to be a migration of yours.

## What it finds and fixes

| Damage | What you see | What the repair does |
|--------|--------------|----------------------|
| Accents replaced by `�` (U+FFFD) | `D�j� re�u` | Rebuilds each word from the other version of the same message (the plain text often survives when the HTML is broken, and the reverse). A word it cannot rebuild stays as it is and is reported. |
| Wrong charset (mojibake) | `RÃ©union`, `câ€™est` | Decodes the text back (UTF-8 read as Windows-1252), in the body and in the subject. |
| Attachments without a real name | `piece_jointe_3`, `ATT00001`, no extension | Reads the real type from the content (PDF, Word, Excel, PowerPoint, OpenDocument, images, `.msg`, `.ics`, `.vcf`, zip) and sets the extension and the MIME type. The name itself is lost; content that tells nothing is left alone. |
| Detached signature images | Broken images in the HTML, the same images as attachments | Puts them back in line with the `cid:` the HTML expects, in order. |
| Unreadable messages | | Reported, never changed. |

Forwarded messages (`message/rfc822`) are kept exactly as they are.

## Check a mailbox

1. Open **Maintenance**, tab **Repair**, and click **+** to add a mailbox, or **Import a CSV file** in the same panel for many (`email;password;login`, like the [accounts](import-accounts.md)). Each mailbox is tested right away: a green check means the login works.
2. Select the mailboxes and click **Analyse**. The analysis only reads (`EXAMINE`, `BODY.PEEK`): nothing is changed, nothing is marked as read.
3. Open a mailbox in the list to read the result: damage by kind, by folder, and examples. **Preview** shows one message as it is and as the repair would write it.

## Repair

Click **Repair** on a mailbox, or select several. For each damaged message:

1. The repaired message is built and checked: same sender, recipients, date and Message-ID, the same attachments byte for byte, no new `�`. A message that fails this check is not touched and is listed under the errors.
2. It is written to the same folder, with the same flags and the same date.
3. The original is copied to `_originaux_avant_reparation/<folder>` in the same mailbox.
4. Only then is the original removed from the folder (`UID EXPUNGE`, only that message).

A repaired message carries the header `X-MCCB-Repair`. A second repair leaves it alone, and so do messages marked by the earlier repair script (`X-Repare-Import`).

A repair can be stopped and started again at any time, also after a crash or a restart: what was done is skipped, and a message is never written twice (the folder is searched for the hash of the original before each write).

> **Warning.** Without the `UIDPLUS` extension the server cannot remove one message alone: the originals are then flagged as deleted instead of removed, and the dashboard says so. Empty the trash of the folders once you checked the result. MCCB never runs a plain `EXPUNGE`, which would also remove what the owner flagged.

## Restore the originals

**Restore the originals** puts every message of `_originaux_avant_reparation` back in its folder and removes its repaired copy. Delete the backup folder yourself once you are satisfied with the repair.

## Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `REPAIR_BACKUP_FOLDER` | `_originaux_avant_reparation` | Folder of the mailbox that keeps the originals. Keep it constant: restore reads it. |
| `REPAIR_PARALLEL` | `2` | Mailboxes analysed or repaired at the same time. |

## Limits

> **Warning.** The real name of an attachment called `piece_jointe_3` is lost: the repair can only give it the right extension. An accent that no other version of the message contains cannot be rebuilt.

- Only IMAP mailboxes. Calendars and contacts are not checked.
- The repair reads every message of the mailbox: a large mailbox takes as long as a download of it.
