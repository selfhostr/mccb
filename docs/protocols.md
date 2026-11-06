# Protocols and file formats

By the end of this page you will know what each built-in connector moves, which credentials it needs, and what it does not carry. For how a route is put together, see [Connectors](connectors.md).

| Kind | Source | Target | Moves |
|---|---|---|---|
| `jmap` | yes | yes | mail, contacts |
| `google` | yes | | mail, calendars, contacts |
| `files` | yes | | mail, calendars, contacts, from an uploaded file |

CalDAV and CardDAV, as sources and targets, are described in [Connectors](connectors.md) and [DAV discovery](dav.md). Every source here is read-only. Nothing is deleted, flagged or moved there.

## JMAP

Mail follows RFC 8620 (core) and RFC 8621 (mail). Contacts follow RFC 9610, where a card is a JSContact object (RFC 9553).

**Calendars are not carried by this connector.** JMAP Calendars is still an Internet-Draft, so it is not implemented. Use the `caldav` connector with the same provider (CalDAV is the standard way to reach its calendars).

- **Credentials.** The session URL (`https://host/.well-known/jmap`, or the provider's session URL) and either a login with a password (`auth: basic`) or an access token (`auth: bearer`). `account_id` is optional; empty means the primary account.
- **As a source.** The session resource is read first, then the account's mailboxes and address books. Each mailbox is a folder. A mail kept in several mailboxes (JMAP mailboxes work like labels) is copied once, in one mailbox chosen the same way on every run: trash, junk, drafts, sent, inbox, then the first by name. Mailboxes with the roles `all`, `flagged`, `important` and `subscribed` are views and are skipped. Mail is listed with `Email/query` and `Email/get` in one request, newest first, and downloaded from the blob URL. `$seen`, `$flagged`, `$answered`, `$draft` and `$forwarded` become flags; the received date is kept. Cards are converted to vCard 3.0.
- **As a target.** Mail is uploaded as a blob and added with `Email/import`, with its keywords and received date. Mailboxes are created as needed, with the special roles when the server has none of them. `root_folder` puts everything under one mailbox. Contacts are written with `ContactCard/set` in an address book of the same name as the source folder, created if absent.
- **Resume.** A mail already imported is found by its `X-PST-Import-Id` header with `Email/query` and checked for an exact match. A contact is found by its `uid`, which is derived from the item, so a second attempt never makes a second card.
- **State.** `changed_since` uses `Email/changes` and `ContactCard/changes`, and falls back to a full listing on `cannotCalculateChanges`. Jobs do not use it yet (see [Known limitations](limits.md)).
- **Limits.**
  - Credentials are only sent to the host of the session URL. A server whose `apiUrl`, `uploadUrl` or `downloadUrl` is on another host is refused. Give the final session URL, not a redirecting one.
  - Photos and other binary properties of a contact are not carried. A card of kind `group` is skipped. Vendor properties that vCard has no equivalent for are dropped.
  - A contact in several address books is copied once.
  - An `Email/import` refused by the server as invalid is reported for that mail and the job goes on.

## Google

Gmail through IMAP, Google Calendar and Google People (contacts) through their REST APIs. **This connector was written from Google's documentation and tested against simulated servers only**, never against Google itself. Try a single test account first.

- **Access, option 1: Google Workspace service account with domain-wide delegation.** Create a service account, enable domain-wide delegation, authorize its client id in the Workspace admin console for the scopes below, and paste the JSON key file whole in `service_account_key`. The connector signs a JWT (RS256) as the service account for the user in `user` and exchanges it for an access token (`urn:ietf:params:oauth:grant-type:jwt-bearer`).
- **Access, option 2: per-user OAuth.** Give `client_id`, `client_secret` and a `refresh_token` of the user. MCCB does not run the consent screen: obtain the refresh token yourself (for example with the OAuth 2.0 Playground and your own client) with the scopes below, once, with offline access.
- **Scopes.** Mail `https://mail.google.com/` (the only scope Gmail IMAP accepts; for delegation, `gmail.imap_admin` also shows labels hidden from IMAP in the user's settings, but this connector asks for `https://mail.google.com/`). Calendar `https://www.googleapis.com/auth/calendar.readonly`. Contacts `https://www.googleapis.com/auth/contacts.readonly`. Enable the Gmail, Calendar and People APIs in the Cloud project, and turn on IMAP for the account. Untick a service (`sync_mail`, `sync_calendar`, `sync_contacts`) to leave its scope out.
- **Mail.** Gmail labels are not folders: a mail with three labels appears in three IMAP folders, and a plain IMAP copy would make three copies. This connector reads `[Gmail]/All Mail` once and uses each message's `X-GM-MSGID` as its identity, and its `X-GM-LABELS` to pick one folder: Drafts, then INBOX, then Sent, then the first user label by name (a label `A/B` becomes the folder `A/B`), then `Archive` for a mail with no label. The other labels are not carried. Starred is the `\Flagged` flag. Spam and Trash are skipped unless `include_trash_spam` is ticked. Messages are read with `BODY.PEEK`, read-only.
- **Calendars.** Every calendar the user can read, as a folder. A series and its changed or cancelled occurrences are one iCalendar resource: a cancelled occurrence becomes an `EXDATE`. A single event is written in UTC; a series keeps the time zone name (`TZID`) so that it follows daylight saving time, without an embedded `VTIMEZONE` block. Attendees, organizer, reminders, location, description and the conference link (`X-GOOGLE-CONFERENCE`) are carried; attachments and the Google-specific event types birthday and working location are not.
- **Contacts.** The user's saved contacts (`people/me/connections`) as one folder, converted to vCard 3.0: names, emails, phones, addresses, organization, title, nicknames, URLs, birthday and notes. "Other contacts", the directory, groups and photos are not carried. A birthday without a year is written with the Apple Contacts convention.
- **State.** Contacts and calendars use the API sync tokens, mail the UID of All Mail, in `changed_since`. Jobs do not use it yet. People sync tokens expire after 7 days; an expired one gives a full listing.
- **Limits.** The Gmail IMAP bandwidth and API quotas of Google apply, and the first full People sync has its own quota (HTTP 429, retried). Google throttles large mailboxes: expect hours per gigabytes.

## Files: mbox, Maildir, .eml, .msg, .ics, .vcf, .zip

These arrive like a PST: through the upload page, `POST /api/uploads`, a URL fetch, or the drop folder (see [Server-side intake](server-intake.md)). The analysis recognizes them, the row appears in the PST list with `scan.format: "files"`, and an account that links it reads it with the `files` connector. A PST or OST is analysed as before and is never read by this connector.

| What you have | How to send it |
|---|---|
| An mbox file (Thunderbird, Apple Mail export, `mutt`, Gmail Takeout) | The file itself, `.mbox` or `.mbx`, or with no extension if it starts with a `From ` line |
| Maildir | A `.zip` of the directory (`cur`, `new`, `tmp`, and `.Sub.Folder` directories) |
| Loose messages | One `.eml` or `.msg`, or a `.zip` of many |
| Calendar | `.ics`, one or many events |
| Contacts | `.vcf`, one or many cards |
| A mix | A `.zip` with any of these, in any folders |

- **Folders.** A single mbox, `.eml` or `.msg` goes to `INBOX`. In a zip, a folder of messages becomes the folder of that name, a message at the root goes to `INBOX`, an mbox becomes a folder named after the file, a Maildir keeps its layout (a root Maildir is `INBOX`, `.Archive.2020` is `Archive/2020`). A calendar or address book file becomes a folder named after its `X-WR-CALNAME` or its file name. Folders named `Inbox`, `Sent`, `Drafts`, `Trash` or `Junk` get that role.
- **Flags.** Maildir flags in the file name (`:2,FLAGS`), mbox `Status` and `X-Status`, and Thunderbird's `X-Mozilla-Status` become the usual flags. A message flagged deleted or trashed is not copied. The date of the message is its `Date` header.
- **Stable keys.** The key of a message is the path inside the file plus its position, so uploading the same file again, or resuming, never copies a message twice. A different file is a different source, even if its messages are the same: messages found again by their `Message-ID` are recognized only after a key change of the same source.
- **mboxrd.** `>From ` lines are unquoted.
- **.msg.** Converted to a standard message from the headers (the transport headers when the file has them), the text and HTML body, and the file attachments. Not converted: a body that exists only as RTF, embedded messages, OLE objects. Each is reported as a warning on that message.
- **.ics and .vcf.** A file with several events or cards is split: one resource per event (a series and its exceptions together, with the file's time zone definitions) or per card. Tasks and journals are left out.
- **Zip.** It is read in place, never unpacked, except an mbox inside it, which is copied to the data folder while a job runs. Encrypted entries, nested zips, entries larger than 512 MB and entries with a compression ratio above 250 are skipped and listed under `scan.skipped`.
- **Limits.** Contacts and calendars need a target for them (the server's DAV, or a target that serves them), as for a PST. A `.zip` dropped in the inbox folder is taken as a mail export, so keep unrelated archives out of it.
