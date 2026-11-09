# Calendars and contacts (DAV)

This page explains how mccb finds the CalDAV and CardDAV collections to write to.

`caldav_url` / `carddav_url` fields of the server:

- empty: auto-discovery via `https://<IMAP host>/.well-known/caldav|carddav`, then via the domain
  of the address;
- otherwise: template URL (`{email}`, `{user}` = local part, `{domain}`, `{login}`). It can
  point directly to a collection, or to an entry point from which we go up to the principal then the
  home-set.

The first VEVENT calendar or address book found is used. If there is none, an
"PST Import" collection is created (MKCALENDAR or extended MKCOL).

Tasks need a calendar that accepts `VTODO`: the first one found, or a new `PST Import` collection with this component.

From a source with several calendars, address books or task lists (Exchange, Google, CalDAV, files), the default one
goes to the collection above and each other one to the collection of the same display name. With none of that name, a
collection named after the source folder is created; if the server refuses, the log says so and its items go to the
default collection. A PST written to the DAV of its server, or a URL that names one collection, still puts everything
in that collection.

Calendars that Outlook or Google generate and that cannot be edited (holidays, birthdays built from the contacts,
internet subscriptions) are not migrated: the log names them.

Objects are written as `<UID>.ics` or `<UID>.vcf` when their UID is safe as a file name, as some servers (MDaemon)
store them under that name whatever the URL of the write.

As a source, the `caldav` and `carddav` connectors read every collection of the mailbox, each object as it is on the server.
