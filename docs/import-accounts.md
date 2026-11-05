# Import accounts from a CSV file

By the end of this page you will have added many mailboxes to a server in one step.

Import through the UI (Accounts page) or `POST /api/accounts/import-csv` (multipart: `file` +
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

Example:

```csv
email;password;login
jean.dupont@example.com;Password1;
marie.martin@example.com;S3cret;mmartin
```

A password containing the separator must be enclosed in quotes (`"a;b"`).
