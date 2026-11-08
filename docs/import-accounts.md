# Import mailboxes from a CSV file

By the end of this page you will have added many mailboxes to a server in one step.

Import through the UI (**New migration**, then **Import a CSV file**) or `POST /api/accounts/import-csv` (multipart: `file` +
`server_id`). A template can be downloaded via `GET /api/accounts/csv-template`.

| column | required | content |
|--------|----------|---------|
| `email` | yes | mailbox address (must look like `x@domain.tld`) |
| `password` | yes | mailbox password (non-empty) |
| `login` | no | IMAP/DAV login if it differs from the address; empty = the address |

- **Separator**: `;` or `,`, detected on the first non-empty line.
- **Header**: optional. Recognized if the first line contains names such as `email`, `e-mail`,
  `mail`, `adresse`, `courriel`, `password`, `mot de passe`, `mdp`, `pass`, `login`, `identifiant`;
  columns are then located by name. Without a header, the order is `email;password;login`.
- **Encoding**: UTF-8 (with or without BOM), UTF-16 (with BOM) or, failing that, cp1252.
- Empty lines are ignored. Invalid lines (invalid address, missing password) are
  rejected one by one and reported with their line number; the others are imported.
- Response: `{created, updated, errors:[{line,msg}]}` (an address already present on that server is
  updated).

## Optional columns

With a header line, the file may carry more columns, in any order:

| column | content |
|--------|---------|
| `pst` | file name of a PST already on the server (case-insensitive); the mailbox is linked to it. An unknown name is a warning, the mailbox is still imported |
| `mail`, `calendar`, `contacts` | what to migrate: `1`/`0`, `yes`/`no`, `true`/`false`, `oui`/`non`. An empty cell leaves the mailbox as it is |
| `source_kind` | a source connector (`imap`...), see [Connectors](connectors.md). `pst` goes back to the PST of the mailbox |
| `source_<field>` | a setting of that source: `source_host`, `source_port`, `source_login`, `source_password`... Secret fields (such as `source_password`) are encrypted like the mailbox password |

A file with a header like these may leave `password` empty for mailboxes that already exist: their password and their source secrets are kept. For a new mailbox the password is still required. A file without a header is read as `email;password;login`, exactly as before. The answer may gain `warnings:[{line,msg}]`.

## Export

`GET /api/accounts/export-csv[?server_id=]` writes one line per mailbox with `email;login;server;pst;mail;calendar;contacts;source_kind;source_<field>...`, in the same format. **Passwords and source secrets are never exported**; their columns do not exist. The file can be imported again as it is.

Example:

```csv
email;password;login
jean.dupont@example.com;Password1;
marie.martin@example.com;S3cret;mmartin
```

A password containing the separator must be enclosed in quotes (`"a;b"`).
