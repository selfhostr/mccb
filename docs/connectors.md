# Connectors

By the end of this page you will know how a migration route is put together and how to add a connector.

A route is a source and a target. `JobRunner` keeps the state: pause, stop, resume, no duplicates, report and tuning are the same for every route. A connector only reads or writes.

| Kind | Source | Target | Items |
|---|---|---|---|
| `pst` | yes | | mail, calendar, contacts |
| `imap` | yes | yes | mail |
| `caldav` | | yes | calendar |
| `carddav` | | yes | contacts |

`GET /api/connectors` returns this catalogue with the capabilities and the fields of each role. The UI renders its forms from it, so a new kind needs no UI code.

## How an account picks its route

- `source_kind` empty: the PST linked to the account. `target_kind` empty: the server of the account (IMAP and DAV, with the account's password).
- Both empty is the historical path of `JobRunner`, with its builder processes. Accounts created before connectors existed stay on it, unchanged.
- Anything else runs in `app/server/connector_runner.py`, one path for every kind.

## Add a connector

Create `app/connectors/<kind>.py`, call `registry.register` at the bottom, and add the module to the import line of `app/connectors/__init__.py`.

```python
class MySource(Source):
    def __init__(self, config, secrets, ctx=None): ...
    def open(self): ...                     # connect; raise ConnectorError
    def close(self): ...                    # never raises
    def folders(self): ...                  # [SourceFolder(path, kind, special, count, ref)]
    def iter_items(self, folder): ...       # yields SourceItem(key, alt_key, date, size, flags, ref)
    def fetch(self, folder, item, import_id): ...  # FetchedItem(raw, internal_date, flags, subject), None to skip

registry.register(registry.ConnectorSpec(
    kind="mine", label="My service",
    source=registry.Role(MySource, ("mail",), frozenset({CAP_FOLDERS}),
                         (registry.Field("host", "Server", required=True),
                          registry.Field("token", "Token", "password", required=True, secret=True)),
                         identity=lambda config: config["host"]),
))
```

A target is a `Target` (settings, thread-safe) that makes one `TargetSession` per thread: `connect`, `close`, `plan_folders`, `ensure_folder`, `put`, `find`, `count_ours`, and optionally `read` and `quota`. Declare `CAP_PARALLEL` if several sessions may write at once, `CAP_READBACK` if `read` returns what was written. See `app/connectors/base.py` for the exact contract, `tests/server/connector_fakes.py` for the smallest complete example.

## What a connector must get right

- **`SourceItem.key` is stable across runs.** It is ASCII, at most 200 characters, unique within the account and the item kind, so include the folder when the protocol's ids are per folder. It decides what is already migrated.
- **`SourceItem.alt_key` is optional and survives a key change.** After a change of UIDVALIDITY, for instance, the runner matches it against the rows whose key the source no longer lists.
- **`identity` names the mailbox, from the config alone.** No secret, no network. The same text keeps the migration state; another text starts afresh.
- **`fetch` never alters the source.** Use read-only access.
- **`find` matches exactly.** It answers "is this item already on the target?" after a lost answer or a crash. A substring match would lose mail, a miss would duplicate it.
- **Errors are `ConnectorError`, with the right kind.**

| Kind | Meaning | What the runner does |
|---|---|---|
| `auth` | credentials refused | fails the job |
| `config` | wrong settings | fails the job |
| `unreachable` | cannot connect | retries with backoff |
| `transient` | a working session failed | retries with backoff, then checks before writing again |
| `rejected` | this item is refused | marks the item as an error, goes on |
| `quota` | the target is full | fails the job |
| `folder_missing` | target folder absent | creates it, tries once more |

## Secrets

A field declared with `secret=True` is refused in `config`, stored encrypted with the application key, and never returned: the API only says `has_secret`. On `PUT`, omitting `secrets` keeps them, `{}` clears them, and a value replaces that one field. Changing the kind drops the secrets of the previous kind.

## Tests

`tests/server/test_connectors.py` runs the route with no network service, through the in-memory connectors. `tests/server/test_connectors_imap.py` runs IMAP to IMAP against GreenMail.
