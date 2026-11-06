# Calendars and contacts (DAV)

This page explains how MCCB finds the CalDAV and CardDAV collections to write to.

`caldav_url` / `carddav_url` fields of the server:

- empty: auto-discovery via `https://<IMAP host>/.well-known/caldav|carddav`, then via the domain
  of the address;
- otherwise: template URL (`{email}`, `{user}` = local part, `{domain}`, `{login}`). It can
  point directly to a collection, or to an entry point from which we go up to the principal then the
  home-set.

The first VEVENT calendar or address book found is used. If there is none, an
"PST Import" collection is created (MKCALENDAR or extended MKCOL).

Tasks need a calendar that accepts `VTODO`: the first one found, or a new `PST Import` collection with this component.

As a source, the `caldav` and `carddav` connectors read every collection of the account, each object as it is on the server.
