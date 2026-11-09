# Exchange

By the end of this page you will know which connector to use for Exchange, which rights to give it, how to migrate a whole Microsoft 365 tenant, and what does not survive.

**These connectors have been tested against simulated servers, not real Exchange.** The tests run the code against fake EWS and Graph servers written from the Microsoft documentation. They prove the logic of the connectors, not that Exchange behaves as documented. Try one mailbox with few items first, and read [What to check on a first real test](#what-to-check-on-a-first-real-test).

| Kind | For | Source | Target | Items |
|---|---|---|---|---|
| `exchange_ews` | Exchange Server on premises | yes | yes | mail, calendar, contacts, tasks |
| `m365_graph` | Exchange Online | yes | yes | mail, calendar, contacts, tasks |
| `imap_oauth2` | any IMAP server with XOAUTH2, Exchange Online included | yes | yes | mail |

EWS is not offered for Exchange Online. Microsoft blocks it by default since 2026-10-01 and plans to remove it on 2027-04-01.

## Routes

A mailbox has one source and one target of its own. The server of the mailbox covers the kinds its target does not serve.

- **Exchange to IMAP, CalDAV and CardDAV on one server:** source `exchange_ews` or `m365_graph`, no target of its own. Mail goes to the IMAP server of the mailbox, calendars and contacts to its DAV addresses if DAV is enabled on the server, tasks to a CalDAV calendar that accepts `VTODO`.
- **Exchange to a separate CalDAV or CardDAV server:** set the target of the mailbox to `caldav` or `carddav`. It serves its own kinds, the server of the mailbox covers the rest.
- **To Exchange:** one mccb mailbox per source, as a source reads one protocol. For a mailbox on IMAP and DAV, that is up to three mccb mailboxes with the same Exchange target: source `imap` for mail, `caldav` for calendars and tasks, `carddav` for contacts. `email` is unique per server: create a second server entry if the same address must appear twice.
- **Exchange to Exchange:** source and target are both Exchange, every kind goes through.

Tasks have no option of their own: they follow the calendar option of the mailbox.

## Exchange on premises (EWS)

Exchange Server 2013, 2016 and 2019 use the default `server_version`. For Exchange 2010 SP2 select `Exchange2010_SP2`.

| Field | Meaning |
|---|---|
| `url` | Usually `https://mail.example.com/EWS/Exchange.asmx`. Autodiscover is not used. |
| `login`, `password` | `DOMAIN\user` or `user@example.com`. |
| `auth` | `auto` follows the `WWW-Authenticate` of the server, NTLM first. Also `ntlm`, `basic`. Basic is refused over plain HTTP. |
| `access` | `own`: the mailbox of the login. `impersonation`: the login acts as `mailbox`. `delegate`: the login has rights on `mailbox`. |
| `mailbox` | SMTP address of the mailbox, for `impersonation` and `delegate`. |
| `include_archive` | Source only: copies the online archive under `Online Archive`. |
| `root_folder`, `time_zone` | Target only. See [Limits](#limits). |

### Rights

One service account can migrate every mailbox with impersonation. In the Exchange Management Shell:

```powershell
New-ManagementScope -Name "mccb" -RecipientRestrictionFilter "MemberOfGroup -eq 'CN=Migration,OU=Groups,DC=example,DC=com'"
New-ManagementRoleAssignment -Name "mccb" -Role ApplicationImpersonation -User svc-migration -CustomRecipientWriteScope "mccb"
```

Without a scope, the role reaches every mailbox of the organisation. For a shared mailbox, `delegate` needs full access:

```powershell
Add-MailboxPermission -Identity shared@example.com -User svc-migration -AccessRights FullAccess
```

## Exchange Online (Microsoft Graph)

App-only: one Entra application and one secret reach every mailbox of the tenant. A shared mailbox is a mailbox like the others: give its address in `mailbox`.

### Connect with Microsoft

The quickest way, and the one to try first. In **Settings**, **Servers**, add a Microsoft 365 tenant and press **Connect with Microsoft**.

1. mccb shows a code. Open `microsoft.com/devicelogin`, enter it and sign in with a **Global Administrator** or a **Privileged Role Administrator** of the tenant: granting Microsoft Graph application permissions requires one of these two roles.
2. Accept the consent screen. It is named **Microsoft Graph Command Line Tools**, the public Microsoft application mccb uses once to sign you in and create the migration application. It is not an mccb application, and mccb keeps no access to it afterwards.
3. mccb creates the **mccb migration** application with the exact application permissions listed below, grants admin consent, and creates a client secret valid for one year.
4. Microsoft takes a few minutes to activate a new application and its consent. mccb waits for it, then runs the test of the tenant. The tenant is then ready, with its directory ID, client ID and secret filled in.

After the migration, remove the application: in the [Entra admin center](https://entra.microsoft.com), **App registrations**, **mccb migration**, **Delete**. Deleting it also removes its consent and secret. The secret expires by itself one year after its creation. To use another application as the sign-in application, or to turn this button off, set `M365_BOOTSTRAP_CLIENT_ID` (see [Configuration](configuration.md)).

If your organisation does not allow this, register the application by hand.

### Register the application by hand

1. In the [Entra admin center](https://entra.microsoft.com), open **App registrations**, **New registration**. Any name, **Accounts in this organizational directory only**, no redirect URI.
2. Note the **Application (client) ID** and the **Directory (tenant) ID**.
3. Under **Certificates & secrets**, create a **client secret**. Note its value, it is shown once.
4. Under **API permissions**, **Add a permission**, **Microsoft Graph**, **Application permissions**, add the permissions below.
5. Click **Grant admin consent**.

| Permission | Needed for |
|---|---|
| `Mail.Read` | reading mail (source) |
| `Mail.ReadWrite` | writing mail (target) |
| `Mail-Advanced.ReadWrite.All` | `import_mode` `import_mime` (target) |
| `Calendars.Read` or `Calendars.ReadWrite` | calendars, source or target |
| `Contacts.Read` or `Contacts.ReadWrite` | contacts, source or target |
| `Tasks.Read.All` or `Tasks.ReadWrite.All` | tasks, source or target. Without it, turn off `include_tasks`. |
| `MailboxSettings.Read` | reading inbox rules (source) |
| `User.Read.All` | listing the mailboxes of the tenant, for [Migrate a Microsoft 365 tenant](#migrate-a-microsoft-365-tenant). `Directory.Read.All` also works. |

`User.Read.All` is new: an application registered before that release needs it added, with admin consent, to list the tenant. Migrating a single mailbox does not need it.

Required: the mail permission of the direction you migrate (`Mail.Read` for a source, `Mail.ReadWrite` for a target) and `User.Read.All` to list the tenant. Optional: calendars, contacts, tasks, `MailboxSettings.Read`, `Mail-Advanced.ReadWrite.All`. `Tasks.Read.All` is the one most often missing: without it, tasks are left out and the rest goes on.

The **Test** button of a tenant in **Settings**, **Servers** reads the roles of the access token and lists every check. A missing permission is named (for example `Missing application permission User.Read.All (admin consent needed)`), marked required or optional. A missing optional permission turns the matching kind off in the coverage of the matching screen (`permission not granted by the tenant`). A token with no role at all means admin consent was not given.

The token is asked for with the scope `https://graph.microsoft.com/.default`. These permissions reach every mailbox of the tenant. To limit them, use [RBAC for Applications](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac) in Exchange Online.

The other clouds are in `cloud`: `usgov`, `usgov-dod`, `china`.

### Mail into Exchange Online: choose the path

Graph has no way to import a message with everything intact.

| Path | Normal message | Flags | Received date | Size | API |
|---|---|---|---|---|---|
| `m365_graph`, `import_mime` | yes | yes | date of the import, see below | not documented, see below | beta |
| `m365_graph`, `draft` | no, every message is a draft | yes | date of the import | not documented, see below | stable |
| `imap_oauth2` | yes | yes | kept | limit of the IMAP service | stable |

`importMime` is a beta API: Microsoft does not support it in production and it can change. It keeps the sent date, and the documented example shows a received date set to the time of the import. After each import the connector tries to set the received date through the property `PR_MESSAGE_DELIVERY_TIME`. This is not documented for Graph and may have no effect.

Microsoft documents a limit of 4 MB for S/MIME payloads and none for other MIME content. A larger message may be refused with HTTP 413: it is reported as an error for this item, the others go on.

**If you need the received dates, write mail with `imap_oauth2`.** A mailbox has one target, so this takes two mccb mailboxes for the same source: one with the target `imap_oauth2`, the other with the target `m365_graph` and the mail option off. `email` is unique per server: create a second server entry for the second mailbox.

### IMAP with OAuth2, app-only on Exchange Online

1. In the application, add the permission **Office 365 Exchange Online**, **Application permissions**, `IMAP.AccessAsApp`, and grant admin consent.
2. In Exchange Online PowerShell, register the application and give it each mailbox:

```powershell
New-ServicePrincipal -AppId <APPLICATION_ID> -ObjectId <OBJECT_ID>
Add-MailboxPermission -Identity "ann@contoso.com" -User <SERVICE_PRINCIPAL_ID> -AccessRights FullAccess
```

3. Connector `imap_oauth2`: `host` `outlook.office365.com`, `login` the mailbox, `token_url` `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, `client_id`, secret `client_secret`, `scope` `https://outlook.office365.com/.default`.

For a provider where each user consents, leave `client_secret` as the provider requires and give a `refresh_token`. A provider that rotates refresh tokens gives a new one with each refresh: it is kept in memory for the job, not stored. If the provider revokes the old one at once, the next job fails with `auth` until a new token is entered.

## IMAP with a master user

On a Dovecot-style server one account reaches every mailbox: in the `imap` connector, `login` is the mailbox, `password` the password of the master, `master_user` its name. The connector logs in as `<login>*<master_user>`. `master_separator` changes the `*`, to match `auth_master_user_separator`.

## Migrate a Microsoft 365 tenant

One credential covers many mailboxes: the tenant is registered once as a source connection, and each mailbox of the target server is linked to it. Nothing is typed per mailbox.

1. **Register the tenant.** In **Settings**, **Servers**, add a Microsoft 365 tenant and press [Connect with Microsoft](#connect-with-microsoft). Or fill in the name, directory (tenant) ID, client ID, secret and cloud after [registering the application by hand](#register-the-application-by-hand), then press **Test** and fix the permissions it names.
2. **Create the target mailboxes.** On the server the mailboxes go to, [import them by CSV](import-accounts.md) (address and password of each). Turn on DAV on that server if calendars and contacts must be copied: without it, the coverage shows `no CalDAV/CardDAV on the server` and only mail is migrated.
3. **Open the matching screen.** On the tenant, press **Migrate mailboxes** and pick the server. The mailboxes of the tenant (users, shared mailboxes, rooms and equipment, with their type) are shown next to the accounts of the server, paired by primary address, then user principal name, then alias.
4. **Read the states.** `Ready`: paired, can start. `Linked`: already uses this tenant. `Not on <server>`: a mailbox of the tenant with no account on the server. `No Microsoft 365 mailbox`: an account with no mailbox in the tenant. `Has another source`: the account has a source of its own, it is not replaced. `Conflict`: two mailboxes want the same account, or one is a tie. Each row also shows what will be copied (mail, calendar, contacts, tasks) and why a kind is not.
5. **Choose a different destination if needed.** On a row, **Choose a destination** (or **Change the destination**) sends the mailbox to another account of the server, whatever its address: pick it in the list, or type the address and password of a mailbox not imported yet (it is created as a CSV line would). The choice is kept, the row says `Chosen by hand`, and **Back to automatic** returns to the match by address. An account receives one mailbox only.
6. **Tick and start.** Tick the `Ready` rows, give the batch a name (default: tenant, server, date) and start. The mailboxes are linked and queued in that batch, and the list of migrations opens on it.
7. **Cutover.** Once the first copy is done, change the DNS (MX) records. This is done outside mccb.
8. **Final resync.** Come back to the same screen and run the final resync on the linked mailboxes: only what changed since the last run is copied.

An account is linked to the tenant by the Entra object ID of the mailbox, not by its address. If the domain is removed from the tenant after the cutover, the final resync still finds the mailbox. The migration state follows the mailbox: a rerun copies only what is new, and nothing is duplicated if an account was already configured alone for the same tenant.

Like the rest of this page, this was tested against simulated servers, not a real tenant. Try one or two mailboxes first. Hidden from the list: guest users and users with no mailbox, counted on the screen. The list is kept 5 minutes; **Refresh** reads it again.

Changing the secret or the application of the tenant keeps every state. The directory ID and the cloud cannot be changed while accounts are linked, and a tenant cannot be deleted then.

To script it, see the `source-connections` routes in the [API](api.md). A mailbox can still be set up alone, with its own `m365_graph` source and `mailbox`, as before.

The migration state follows the mailbox: changing the secret, the application or the service account keeps it, a rerun copies only what is new.

Graph allows 150 MB of uploads per mailbox and application in 5 minutes, so about 43 GB per mailbox per day at best, and 10,000 requests per 10 minutes. Several mailboxes can run at once: these limits are per mailbox.

## Mail rules

```
POST /api/accounts/{id}/rules
```

reads the inbox rules of an `exchange_ews` or `m365_graph` source and returns a Sieve script, with the list of what was converted and what was not. **This is best effort.** Read the script before using it.

| In Exchange | In Sieve |
|---|---|
| subject, body, sender or recipient contains, from, sent to | `header`, `body`, `address` |
| exceptions on those | `not` |
| move, copy, delete | `fileinto`, `fileinto :copy`, `fileinto "<trash_folder>"` |
| delete permanently | `discard` |
| mark as read | `addflag "\\Seen"` |
| forward, redirect | `redirect :copy`, with a warning |
| stop processing | `stop` |

- A condition or an exception with no equivalent drops the whole rule: it would match more mail than in Exchange. Attachments, importance, size, categories, sent only to me and the rest are in this case.
- An action with no equivalent (categories, importance, forward as attachment) is dropped, and the rule is listed under `partial`.
- A disabled rule is not converted.
- Client-only rules of Outlook are not read: the server does not expose them.

With `managesieve` in the request, the script is uploaded over ManageSieve (port 4190, STARTTLS by default) under `mccb-exchange-rules`. It is **not activated** unless you ask, as this replaces the active script of the user. If the upload fails, the answer still holds the script.

The body of a Sieve rule needs the `body` extension, flags `imap4flags`, copies `copy`. Check that the server has them.

## Limits

### Everywhere

- **One pass, not a sync.** A change at the source after its copy is not carried over. `changed_since` exists in the connectors, jobs do not use it yet.
- **A moved item is copied again** with EWS, whose ids change with the folder, unless the Message-ID finds it again in the same folder. Graph uses immutable ids.
- **Items can be listed as `skipped`:** distribution lists with EWS. Graph does not list contact groups at all. Neither is migrated.
- **Attachments of events and contact photos are not copied.** A warning is recorded for each one.
- **No re-read of a sample.** Exchange rewrites the MIME it stores, a hash would never match. The check after a job counts the items only.
- **A recurring series is written whole.** If one of its exceptions fails, the series is removed and the whole item is tried again.

### Calendars

- **Organiser.** The event is created by the mailbox of the target, which becomes its organiser, whoever organised it at the source. The source organiser is kept when the target is a DAV server.
- **Responses of attendees are read and written to DAV, not written to Exchange:** Exchange does not let a client set them. With EWS, attendees are written and nobody is notified. An attendee known only by a legacy Exchange address is dropped, with a warning.
- **Graph sends invitations.** Graph mails every attendee of an event it creates, and Microsoft says this cannot be configured. So by default `m365_graph` does not write attendees as attendees: they are written as text in the body of the event, with the organiser. `send_invitations` writes them, and every attendee of every migrated event receives a mail.
- **Recurrences.** Daily, weekly, monthly and yearly rules go through. A rule Exchange cannot hold (hourly, several days of the month, position with several unrelated days) loses its recurrence when read from DAV: the first occurrence is migrated, with a warning. A rule EWS or Graph refuses to write is an error for this item. Tasks that regenerate after completion lose their recurrence.
- **Exceptions** written to Exchange keep subject, start, end, location and body. Other changes on one occurrence are lost. A DAV target also gets their attendees.
- **Time zones.** A zone is written by its Windows name. An IANA zone that the CLDR table does not list is matched by its offsets over the year. No match: the event is written in UTC, so it stays at the right instant but a recurrence does not follow summer time. A custom zone of Outlook (`tzone://Microsoft/Custom`) is read as UTC, with a warning.
- **All-day events** have no zone in iCalendar. `time_zone` on the target says which one to use if the source does not, `X-WR-TIMEZONE` being read when present. Empty: UTC, and some clients may show the event on the previous or next day.
- **Body** as plain text only: HTML of events, contact notes and tasks is lost.

### Contacts

- Exchange holds 3 email addresses. With Graph, the other addresses and every phone number other than business, home and mobile (fax, pager, assistant) go in the notes. EWS has fields for them, but only 3 addresses: the others are lost, with a line in the server log.
- Exchange holds one postal address each for business, home and other: a second one of the same kind is lost.

### Tasks

- Only `exchange_ews` to `exchange_ews`, `m365_graph` to `m365_graph`, between them, and to or from a CalDAV `VTODO` collection. An IMAP target cannot hold them. A PST source does not read them.
- `m365_graph` reads Microsoft To Do, not legacy Outlook tasks. Checklists and attachments of tasks are not copied.
- To Do has no nested lists: a nested source folder becomes `A / B`.
- With no target for tasks, the job log says so and the job goes on.

### Folders

- Calendars and contact folders are flat with `m365_graph`: `A / B` for a nested source folder.
- On a DAV target, the default calendar, address book and task list go to the default collection, each other one to its own collection, matched by name or created. See [DAV](dav.md).
- Read-only calendars of Outlook (holidays, birthdays, subscriptions) are not migrated.
- The inbox, sent items, drafts, deleted items and junk of the source go to the well-known folders of the target, whatever their language. The others go by name under `root_folder`.

### Archive mailboxes

- **`m365_graph` does not migrate archive mailboxes.** Graph's mail API, which reads MIME, does not expose them. Graph has separate [import and export APIs](https://learn.microsoft.com/en-us/graph/mailbox-import-export-concept-overview) that reach archives, but they export an opaque stream that can only be imported back into Exchange Online: nothing to give to IMAP or DAV. mccb does not use them. To migrate an archive, move its content to the primary mailbox first.
- `exchange_ews` reads the online archive with `include_archive`. Whether an archive on a separate database answers on the same URL is not verified.
- IMAP does not expose archive mailboxes either.

### Throttling and connections

- **Graph:** on 429, 503 and 504 the connector waits for the `Retry-After` of the answer, up to 5 minutes and 6 attempts per request, then the runner backs off and retries. It keeps to the documented 4 concurrent requests per mailbox and application, whatever the tuning. A pause or a stop waits for the end of the current wait.
- **EWS:** on `ErrorServerBusy` the connector waits for `BackOffMilliseconds`, on the same terms. On premises, a throttling policy applied to the service account helps more than anything here.
- **Extended Protection.** The NTLM of the connector does not send a channel binding: on a server that requires Extended Protection for EWS, NTLM fails. Use `basic` if the server allows it.
- **Autodiscover and hybrid.** No autodiscover: give the URL. Mailboxes redirected to another server (HTTP redirect) are reported as a configuration error with the address to use.
- **Secrets** are stored encrypted like the other secrets, and never returned by the API. A rotated refresh token is not stored back, see above.

## What to check on a first real test

Nothing here has run against real Exchange. In this order, on one test mailbox with few items:

1. **Duplicates.** Run a job, then run it again. Nothing must be copied twice. This depends on Exchange storing and finding our custom property `MccbImportId`, and with Graph on the `singleValueExtendedProperties` filter with `eq` and `startswith`.
2. **Crash between the import and the PATCH with Graph.** `find` then relies on Exchange exposing the `X-PST-Import-Id` header as a named property, which is not documented for imported MIME. Stop the job during a run, resume, count.
3. **Mail state on EWS.** A message created with `MimeContent` and `PR_MESSAGE_FLAGS` must not be a draft, and the read state, flag, reply mark and received date must be those of the source.
4. **Received date with Graph.** Check `receivedDateTime` after import, with and without the `PR_MESSAGE_DELIVERY_TIME` PATCH.
5. **Size limit of `importMime`.** Try messages of 3, 5, 20 and 40 MB.
6. **The `$expand` with `or` on extended properties** in the message listing of Graph: if refused, sizes and reply marks are lost or the listing fails.
7. **A recurring series with one moved and one deleted occurrence**, in each direction. Check the occurrences in Outlook. Finding them depends on `OriginalStart` in EWS and `originalStart` in Graph, and on `cancelledOccurrences` and `exceptionOccurrences`.
8. **No invitation mail.** With EWS `SendToNone`. With Graph, create an event with attendees and check that no mail leaves with `send_invitations` off.
9. **Time zones.** An event in a zone with summer time, one in a zone outside the CLDR table, an all-day event.
10. **NTLM against IIS**, with and without Extended Protection, and impersonation with `X-AnchorMailbox`.
11. **Tasks through Graph:** a recurring task needs a due date, `linkedResources` must come back with `$expand`, mailboxes without To Do.
12. **Rules:** `GetInboxRules` and `messageRules` on real rules, then the script on Dovecot through ManageSieve.
13. **Throttling.** A real mailbox of several GB, to see how the tuning and the Graph limit on uploads behave.
