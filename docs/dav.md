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

With none, a collection named after the source folder is created, only when that folder has something to copy (an
empty one is neither created nor checked). CalDAV tries MKCALENDAR then an extended MKCOL (RFC 5689), CardDAV an
extended MKCOL; a name with "/" in it (a sub-list at the source, `Tâches/24me`), which SmarterMail reads as a path and
refuses (409), is tried again as `Tâches - 24me`, and the log says so. If the server still refuses, the log says it
once for the folder (`the target refuses to create task lists: create 'Tâches/24me' in the webmail then rerun`) and
its items are in error with that reason: they never go into another collection. A name the target already has is
never created a second time, and a collection the server listed is never created over itself. A PST written to the DAV of its server, or a URL that names one
collection, still puts everything in that collection. The job log says where each folder goes
(`Target for contacts: 'Contacts' -> existing 'Contacts' (...)`).

When the plan of a folder changed since an earlier run (an earlier version made a second "Contacts"), the next run
copies its items into the collection it now goes to: an item already there (same resource name) is left as it is, and
the log and the report name the earlier collection (`collection_changed`, `dav_moved`). The collection chosen is then
kept for the next runs of the job. A mirror pass keeps updating and deleting each item where mccb wrote it.

### Collections earlier versions left behind

At the end of a full run or of a final resync (never a mirror pass, never a rerun of the errors only), mccb deletes the
collections earlier versions left on the target: that second "Contacts" (calendar, task list), and the copies of the
collections shared by another mailbox or public, which are now left to their owner (see below; not with
`DAV_INCLUDE_SHARED=1`). A collection is deleted only when:

- mccb created it (an URL of its own, `mccb-...` or `import-pst-...`), no folder of the run goes to it, and no migrated
  item of any mailbox is recorded in it;
- every object in it, as the server lists it then, is a copy mccb made for this mailbox and no longer needs there: an
  item recorded as moved out of it (by this version, or the `dav_moved` of an earlier report of the mailbox) that the
  collection its folder now goes to lists, or an item of a shared folder that is no longer migrated.

On a server that keeps one object per UID in the account (SmarterMail), the copy in the earlier collection is what
keeps an item out of the new one (its error names that collection): such an item is moved first, deleted there then
written into the new one, and written back where it was if the new one still refuses it.

Anything else keeps the collection, with a `collection_kept` line in the log (and `dav_kept` in the report) saying why:
an object added in the webmail, an item not yet in its new collection, an item gone from the source that only that
collection still holds, a listing that failed. A collection the server or the user made is never touched. A copy of
every object is kept under `mirror-trash/` before anything is deleted (purged after `MIRROR_TRASH_DAYS` days); the log
names each deleted collection with its number of items, the report lists them (`dav_removed`: url, name, items,
reason `moved` or `shared`, moved, backup), and the items rows and the choices of earlier runs that pointed at it are
forgotten. The source is only read.

Calendars that Outlook or Google generate and that cannot be edited (holidays, birthdays built from the contacts,
internet subscriptions) are not migrated: the log names them.

With DAV disabled on the server, a PST job sends no calendar or contact item: its log and report say how many were
left out. An Outlook distribution list becomes a vCard contact group (`X-ADDRESSBOOKSERVER-KIND:group`): its members
are written as `mailto:` members, as `EMAIL` lines and in the note, so that clients without groups still show them.

Objects are written as `<UID>.ics` or `<UID>.vcf` when their UID is safe as a file name, as some servers (MDaemon)
store them under that name whatever the URL of the write.

As a source, the `caldav` and `carddav` connectors read every collection of the mailbox, each object as it is on the
server. A collection the mailbox does not own is left out, with a line in the log and an entry in the report
(`folders_skipped`): it is migrated with the mailbox that owns it, not once more with every mailbox it is shared with.
That is what the server says (a calendarserver.org `shared` mark, a `DAV:owner` other than the account's principal),
then the names SmarterMail gives them: `[compta@kmy.ch] - Calendrier` (shared by another mailbox) and
`[Public] - kmy.ch/Contacts` (public folders of the domain). `DAV_INCLUDE_SHARED=1` migrates them as the others.

## Errors of the target

A server that answers 500 to one object is not taken for a server in trouble at once (SmarterMail does it every time
for some objects). The object is tried once more a moment later (without `If-None-Match` when it is not there, as
some servers answer 500 to the condition; over it with `If-Match` when it is). If the collection still answers
(PROPFIND) the problem is that object, and it fails in a few seconds instead of a minute of retries, with a reason:

- an object of the same UID elsewhere in the account (same resource name in another collection, or an
  `addressbook-query` / `calendar-query` on the UID): SmarterMail keeps one per UID for the whole account. The log
  names that collection, e.g. the second "Contacts" an earlier version made. When mccb created it and it holds
  only copies of this mailbox, the end of the run moves those items and deletes it (see above); otherwise delete it in
  the webmail once checked, then rerun;
- otherwise the object is written in a plainer form: a vCard 3.0 (a 4.0 card converted, FN and N added when
  missing, CRLF, lines folded at 75 octets, a photo over 256 KB left out), an iCalendar object with VERSION, PRODID
  and a DTSTAMP in each component; then without its X- and binary properties. The form that was written is logged and
  kept in the warnings of the item.

A server that does not answer the PROPFIND either is in trouble: its errors are retried as before. When 20 objects in
a row are refused by a collection that answers, the next ones of that collection are not sent (one in 25 still is: a
success sends the rest again): they are in error, with one line in the log for the collection.

Two objects of one UID under two resource names (a UID not safe as a file name), in one collection, are said in the
log when they are written; a server that keeps only one of them is not a verification problem. A collection that
holds fewer of our objects than were sent names the missing ones in the log.
