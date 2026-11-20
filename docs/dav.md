# Calendars and contacts (DAV)

This page explains how mccb finds the CalDAV and CardDAV collections to write to.

`caldav_url` / `carddav_url` fields of the server:

- empty: auto-discovery via `https://<IMAP host>/.well-known/caldav|carddav`, then via the domain
  of the address;
- otherwise: template URL (`{email}`, `{user}` = local part, `{domain}`, `{login}`). It can
  point directly to a collection, or to an entry point from which we go up to the principal then the
  home-set.

The default collection is the one with a default name for its kind ("Contacts", "Calendar", "Calendrier",
"Kalender", "Tasks"...), else the first VEVENT calendar or address book found. A collection that says it grants no
write (`DAV:current-user-privilege-set`, such as a global address list) is never used. If there is none, an
"PST Import" collection is created (MKCALENDAR or extended MKCOL).

Tasks need a calendar that accepts `VTODO`: the one with a default name, else the first one found, or a new
`PST Import` collection with this component.

From a source with several calendars, address books or task lists (Exchange, Google, CalDAV, files), each folder goes
to an existing collection when there is one for it:

- the default folder of the source (a CalDAV/CardDAV source: the one with a default name, else the first) goes to the
  collection of the same display name (whatever the case), else to the default collection above;
- every other folder goes to the collection of the same display name;
- the lists some servers keep as address books (SmarterMail "Allowed Senders" and "Blocked Senders") go to the list of
  the same role under the target's language ("Expéditeurs autorisés", "Erlaubte Absender"...).

With none, a collection named after the source folder is created; if the server refuses, the log says so and its
items go to the default collection. A name the target already has is never created a second time, and a collection
the server listed is never created over itself. A PST written to the DAV of its server, or a URL that names one
collection, still puts everything in that collection. The job log says where each folder goes
(`Target for contacts: 'Contacts' -> existing 'Contacts' (...)`).

When the plan of a folder changed since an earlier run (an earlier version made a second "Contacts"), the next run
copies its items into the collection it now goes to: an item already there (same resource name) is left as it is, the
earlier copies are never deleted, and the log and the report name that collection (`collection_changed`, `dav_moved`)
so that it can be deleted by hand once checked. The collection chosen is then kept for the next runs of the job. A
mirror pass keeps updating and deleting each item where mccb wrote it.

Calendars that Outlook or Google generate and that cannot be edited (holidays, birthdays built from the contacts,
internet subscriptions) are not migrated: the log names them.

With DAV disabled on the server, a PST job sends no calendar or contact item: its log and report say how many were
left out. An Outlook distribution list becomes a vCard contact group (`X-ADDRESSBOOKSERVER-KIND:group`): its members
are written as `mailto:` members, as `EMAIL` lines and in the note, so that clients without groups still show them.

Objects are written as `<UID>.ics` or `<UID>.vcf` when their UID is safe as a file name, as some servers (MDaemon)
store them under that name whatever the URL of the write.

As a source, the `caldav` and `carddav` connectors read every collection of the mailbox, each object as it is on the server.
