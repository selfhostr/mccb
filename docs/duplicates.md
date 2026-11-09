# Remove duplicates

By the end of this page you will have found the duplicate messages, events and contacts of a mailbox, and removed them with one copy of each kept.

The **Duplicates** tab works on mailboxes MCCB already knows: the migration accounts whose target is their IMAP server, and the mailboxes of the [Repair](repair.md) tab. It uses their stored password and works on the server of the mailbox, the target of a migration. Nothing is asked again.

## What counts as a duplicate

Deleting is permanent, so the criteria are strict: two items are copies only when nothing a person would see differs. Items are compared only inside the same folder (IMAP) or the same collection (CalDAV, CardDAV). The same message in two folders is never a duplicate.

| Kind | Copies when | Kept |
|------|-------------|------|
| Mail | Same `Message-ID`, same size (RFC822, byte for byte), same `Date`, `From` and `Subject`. Without `Message-ID`: same `Date`, `From`, `Subject` and size. Without `Message-ID`, `Date` or `From`: never. | The oldest copy (lowest UID). |
| Events | Same `UID` (and `RECURRENCE-ID`), or, when the UIDs differ, the same `SUMMARY`, `DTSTART`, `DTEND` (or `DURATION`), `RRULE` and exceptions, time zone included. | The most complete copy (most properties), then the first by name. |
| Contacts | Same `FN`, same e-mail addresses (case ignored) and same phone numbers (digits and a leading `+`). A card without e-mail and phone, or a group, never. | The most complete copy, then the first by name. |

Why the size and the three headers for mail: a copy of a message keeps every byte, while a message that lost an attachment or got a list footer has the same `Message-ID` but another size, and some mailers reuse a `Message-ID`. Comparing the bodies would mean downloading the whole mailbox; the size and four headers are read in one `UID FETCH` per 500 messages (`BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)]` and `RFC822.SIZE`), so a mailbox of tens of thousands of messages is analysed in minutes.

Messages already flagged as deleted by their owner are not looked at. The originals kept by the repair tab (`_originaux_avant_reparation`) are left alone.

## Analyse

1. Open **Duplicates**, select the mailboxes, and choose the kinds (mail, calendar, contacts).
2. Click **Analyse**. The analysis only reads (`EXAMINE`, `BODY.PEEK`, `PROPFIND`, multiget): nothing is changed, nothing is marked as read.
3. Open a mailbox in the list to read the result: copies by kind, by folder and collection, a few examples with the copy that would be kept, the log and the history of the runs.

## Remove the duplicates

Click **Remove duplicates** on an analysed mailbox, or select several. The confirmation shows the number of messages, events and contacts the last analyses found. The run reads the mailbox again, then for each batch of groups:

1. The group is read again just before deleting. A copy that changed or left since is skipped; when the copy to keep changed or left, the whole group stays.
2. Mail: the copies are flagged `\Deleted` and removed with `UID EXPUNGE` of these UIDs only. Events and contacts: `DELETE` with `If-Match` and the etag just read, so an item edited in the meantime is not deleted.
3. One item of each group always stays.

> **Warning.** Deletion is permanent: there is no trash and no restore. Analyse first and read the examples.

A server without the `UIDPLUS` extension cannot remove one message alone: MCCB then refuses the removal in its folders and says so, rather than run a plain `EXPUNGE` that would also remove what the owner flagged. A DAV server that gives no etag is refused the same way.

A removal is refused while a migration or a repair writes to the same mailbox. A run can be stopped at any time and started again: it reads the mailbox again from the start.

## Settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEDUP_PARALLEL` | `2` | Mailboxes analysed or cleaned at the same time. |
