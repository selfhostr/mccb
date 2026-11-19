# Connectors

By the end of this page you will know how a migration route is put together and how to add a connector.

A route is a source and a target. `JobRunner` keeps the state: pause, stop, resume, no duplicates, report and tuning are the same for every route. A connector only reads or writes.

| Kind | Source | Target | Items |
|---|---|---|---|
| `pst` | yes | | mail, calendar, contacts |
| `imap` | yes | yes | mail |
| `imap_oauth2` | yes | yes | mail |
| `caldav` | yes | yes | calendar, tasks |
| `carddav` | yes | yes | contacts |
| `exchange_ews` | yes | yes | mail, calendar, contacts, tasks |
| `m365_graph` | yes | yes | mail, calendar, contacts, tasks |
| `jmap` | yes | yes | mail, contacts |
| `google` | yes | | mail, calendar, contacts |
| `files` | yes | | mail, calendar, contacts |

The Exchange connectors have been tested against simulated servers, not real Exchange: see [Exchange](exchange.md). `jmap`, `google` and `files` are described in [Protocols and file formats](protocols.md).

`GET /api/connectors` returns this catalogue with the capabilities and the fields of each role. The UI renders its forms from it, so a new kind needs no UI code.

## How a mailbox picks its route

- `source_kind` empty: the PST linked to the mailbox. `target_kind` empty: the server of the mailbox (IMAP and DAV, with the mailbox's password).
- Both empty is the historical path of `JobRunner`, with its builder processes. Mailboxes created before connectors existed stay on it, unchanged.
- Anything else runs in `app/server/connector_runner.py`, one path for every kind. This path also carries tasks, to a target that declares them: they follow the calendar option of the mailbox.

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

A target is a `Target` (settings, thread-safe) that makes one `TargetSession` per thread: `connect`, `close`, `plan_folders`, `ensure_folder`, `put`, `find`, `count_ours`, and optionally `read`, `quota` and `has_folder` (used by the dry run to tell which folders will be created). `info()` may return `append_limit`, the largest message in bytes that the target accepts: a larger message is not sent and is reported as oversized. Declare `CAP_PARALLEL` if several sessions may write at once, `CAP_READBACK` if `read` returns what was written. See `app/connectors/base.py` for the exact contract, `tests/server/connector_fakes.py` for the smallest complete example.

## What a connector must get right

- **`SourceItem.key` is stable across runs.** It is ASCII, at most 200 characters, unique within the mailbox and the item kind, so include the folder when the protocol's ids are per folder. It decides what is already migrated.
- **`changed_since` is used by the final resync.** A source that declares `CAP_INCREMENTAL` answers with the items new since an opaque cursor; the runner stores the cursor per folder once the items are recorded. `Changes.reset=True` means the list is complete.
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

## Mirror mode

The mirror (`mode: "mirror"`, see [Plan, filter and control](planning.md#mirror)) adds, deletes and updates on the target what changed at the source since the previous pass. It needs more of a connector, and every part is optional: a connector that declares nothing is simply refused by the mirror, its other routes are unchanged.

A source declares `CAP_MIRROR` when it can be trusted to list a folder completely:

- `mirror_listing(folder)` returns a `Listing(items, complete, epoch, problem)`. The default lists `iter_items`. Say `complete=False` (with `problem`) whenever something may be missing, such as paging cut short or a partial answer: the mirror then deletes nothing in that folder. Raise `ConnectorError` on a failure: nothing is deleted either.
- `SourceItem.version` changes whenever the content of the item changes: DAV etag, EWS `ChangeKey`, Graph `changeKey`, a JMAP state per item. `None` means unknown, and such items are never updated. Mail flags go in `flags`, as before.
- `key_epoch(key)` gives the epoch a key was made under, such as the UIDVALIDITY inside an IMAP key. When the current `Listing.epoch` differs from the epoch of the tracked keys, the folder is refused (`uidvalidity_changed`).
- `exists(folder, keys, refs=None)` reads the source directly, key by key (`SourceItem.key`), just before the mirror deletes what a listing no longer has: `{key: still there}`. `True` also means "cannot tell", and the item is kept. The default answers `True` for every key, so a connector deletes nothing until it implements `exists`. An error refuses every deletion of the pass. `refs` maps each key to the `SourceItem.ref` recorded when the item was written (`items.src_ref`, through JSON: a tuple comes back as a list; `None` when it was not JSON or not recorded), for keys that cannot be turned back into an id.
- A folder the source no longer lists (deleted or renamed) deletes nothing: its items are counted in `skipped_by_safety` and the pass is `source_unlistable`. An epoch containing `renamed-or-moved` or `unverified` (`UNSTABLE_EPOCHS`) refuses the folder like a UIDVALIDITY change.
- `Changes.removed` lists the keys a source reports as removed since a cursor (JMAP `destroyed`, Google `cancelled` or `metadata.deleted`). It is informative: the mirror deletes only what a complete `mirror_listing` misses and `exists` confirms gone.
- **The source is read only.** The mirror reaches the source through a wrapper that only lets `open`, `close`, `folders`, `iter_items`, `fetch`, `mirror_listing`, `key_epoch` and `exists` through (`READ_ONLY_SOURCE_METHODS`). Nothing in them may write, flag or mark anything at the source.

A target declares what it can do, and the mirror only uses what is declared:

| Capability | Method | Contract |
|---|---|---|
| `CAP_INVENTORY` | `inventory(folder, namespace, names)` | Read only. `{import id or resource name: [RemoteItem(ref, flags, version)]}` of OUR items in the folder: mails whose import id starts with `<namespace>-`, resources whose name is in `names`. Never pre-existing content. Required for any mirror write. |
| `CAP_DELETE` | `delete_mode(folder)`, `delete(folder, ref, expected_import_id)`, `delete_many(folder, refs, expected_import_ids)` | `DELETE_TRASH`: moved to the trash of the target. `DELETE_SOFT`: marked, still there (a mail flagged `\Deleted` and `$MccbMirrorDeleted`, never expunged). `DELETE_HARD`: gone, and the runner first keeps a copy through `read()` under `<data>/mirror-trash/`. Already gone is not an error. |
| `CAP_UPDATE` | `update(folder, ref, item)` | Replaces the content of one of our events, contacts or tasks. |
| `CAP_SET_FLAGS` | `set_flags(folder, ref, add, remove)` | Changes the flags of one of our mails. |
| `CAP_MOVE` | `move(folder, ref, to_folder)` | Moves one of our mails to another folder of the target. Without it, a mail moved at the source is deleted from the old folder and copied to the new one. |

The mirror deletes through `delete_many` (100 refs at most per call; the default loops on `delete`): it answers `{ref: mode}` for those done, a missing ref failed. `delete` gets the import id (mail) or resource name the item must still carry, and refuses with `rejected` when it can check and it differs. The mirror calls `inventory` and then the writes on the same session.

`ref` is always a `RemoteItem.ref` that `inventory` returned for an item of the `items` table: a target method is never called on anything else. `IMAP` and `CalDAV`/`CardDAV` implement all of it; `tests/server/connector_fakes.py` has the in-memory version.

## Secrets

A field declared with `secret=True` is refused in `config`, stored encrypted with the application key, and never returned: the API only says `has_secret`. On `PUT`, omitting `secrets` keeps them, `{}` clears them, and a value replaces that one field. Changing the kind drops the secrets of the previous kind.

## Tests

`tests/server/test_connectors.py` runs the route with no network service, through the in-memory connectors. `tests/server/test_connectors_imap.py` runs IMAP to IMAP against GreenMail. `tests/server/test_connectors_exchange.py` runs the Exchange connectors against the simulated servers of `tests/server/fake_exchange.py`.
