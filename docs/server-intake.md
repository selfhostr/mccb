# Server-side intake

By the end of this page you will know how to get PST files onto the server without keeping your own computer on, and how to have them assigned and imported automatically.

Three ways, all ending in the usual analysis (a PST becomes `valid` or `invalid`):

- the **drop folder**: copy files into it with rsync, scp, a mount or a file manager;
- **server-side fetch**: the server downloads the file itself (HTTP(S), SFTP, S3-compatible);
- the browser upload described in [Migrate a mailbox](migrate.md), which still works.

## Drop folder

The worker watches `DATA_DIR/inbox` (`./data/inbox` with the default compose file). Set `INBOX_DIR` to use another folder, for example a mount.

```bash
rsync -av --partial *.pst user@server:/path/to/data/inbox/
```

- `.pst` and `.ost` files are taken, and also `.mbox`, `.mbx`, `.eml`, `.msg`, `.ics`, `.vcf` and `.zip` files, which become sources for the `files` connector (see [Protocols and file formats](protocols.md)). Keep unrelated archives out of the folder. Dotfiles (rsync temporary names are `.name.XXXXXX`) and names ending in `.part`, `.partial`, `.tmp`, `.temp`, `.crdownload` are ignored. Sub-folders are scanned, except hidden ones.
- A file is picked up when its size and modification time have not changed for `inbox.settle_seconds` (60 by default). A file still being copied is left alone.
- The file is moved into the PST folder: an atomic rename on the same filesystem, otherwise a copy followed by removal of the source. If the worker restarts in the middle, it finishes the move.
- A file with the same content as a PST already known is left in the folder and not registered again. MCCB never deletes it.
- The PST row shows `source: "inbox"`.

SMB shares are not read directly. Mount the share on the server (`mount -t cifs`) and set `INBOX_DIR` to the mount point.

## Fetch from a URL, SFTP or S3

```bash
curl -b cookies -X POST http://localhost:22362/api/psts/fetch -H 'Content-Type: application/json' -d '{"kind":"http","url":"https://files.example.com/jane.pst","token":"..."}'
```

| kind | fields |
|------|--------|
| `http` | `url`, optionally `token` (Bearer) or `username` + `password` (Basic) |
| `sftp` | `host`, `port` (22), `path`, `username`, `password` or `private_key` (+ `passphrase`), optionally `host_key_sha256` |
| `s3` | `endpoint` (for example `https://s3.eu-west-1.amazonaws.com` or a MinIO URL), `bucket`, `key`, `region` (`us-east-1`), `access_key`, `secret_key` |

- The transfer runs in the worker. The PST row has status `fetching`, and `received_bytes` grows (`size` is known once the server answers).
- HTTP and S3 resume with a `Range` request; SFTP resumes from the size already on disk. On a network error the worker retries up to 5 times with a growing pause, resuming each time. After that the status is `fetch_failed` with the reason in `error`; `POST /api/psts/{id}/retry-fetch` tries again.
- `POST /api/psts/{id}/cancel` stops the transfer and removes the partial file and the row.
- Free space is checked with the same rule as uploads (size plus `UPLOAD_DISK_MARGIN`).
- Credentials are encrypted at rest and never returned by the API.
- Link-local and cloud metadata addresses are refused; private ranges are allowed. See [Security](security.md).
- SFTP without `host_key_sha256`: the first key seen is remembered and then enforced.

## Automatic assignment and import

Off by default. In `PUT /api/intake/settings`:

```json
{"auto_chain": {"enabled": true, "start_import": true, "default_domains": {"1": "example.com"}}}
```

After a PST is analysed as `valid`, MCCB looks for the account whose address matches the file name:

- `jane.doe@example.com.pst` matches the account `jane.doe@example.com`;
- `jane.doe.pst` matches `jane.doe@example.com` on server 1 when `default_domains` has `"1": "example.com"`.

If exactly one account matches and it has no PST yet, the PST is assigned to it. With `start_import` the import is queued too, when the account has a password and no active job. An existing assignment is never changed. When several accounts match, nothing is done. The outcome is in the `chain` field of the PST (`assigned`, `ambiguous`, `no_match`, `account_has_pst`, `already_assigned`). Only PSTs analysed after you enable the option are considered.

Pair this with [Notifications](notifications.md) to be told when everything is done.
