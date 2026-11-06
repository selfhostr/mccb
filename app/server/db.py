"""SQLite access (WAL) shared between the web process and the worker."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    pst_id INTEGER NOT NULL,
    pst_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    item_key TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    target TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    warnings TEXT,
    subject TEXT,
    hash TEXT,
    size INTEGER,
    job_id INTEGER,
    updated_at REAL NOT NULL,
    source_ref TEXT,
    UNIQUE (account_id, pst_key, kind, item_key)
);
"""
# Items are attached to the PST by its fingerprint (`pst_key` = psts.pst_sha) and not by its id:
# deleting then re-uploading the same file recovers the progress state (no re-import or duplicate).
# Other sources use a namespace of their own in pst_key ("src-...", see connectors/registry.py) and pst_id 0.
# source_ref: second identity of the item at the source (SourceItem.alt_key), survives a change of item_key.
ITEMS_INDEXES = """
CREATE INDEX IF NOT EXISTS items_status2 ON items(account_id, pst_key, kind, status);
CREATE INDEX IF NOT EXISTS items_folder2 ON items(account_id, pst_key, kind, folder);
"""
ITEMS_COLS = ("id, account_id, pst_id, pst_key, kind, item_key, folder, target, status, error, warnings, subject, "
              "hash, size, job_id, updated_at")

SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL DEFAULT 993,
    imap_security TEXT NOT NULL DEFAULT 'ssl',
    verify_tls INTEGER NOT NULL DEFAULT 1,
    root_folder TEXT,
    caldav_url TEXT,
    carddav_url TEXT,
    dav_enabled INTEGER NOT NULL DEFAULT 1,
    workers INTEGER NOT NULL DEFAULT 4,
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    tuning TEXT NOT NULL DEFAULT 'auto',
    builders INTEGER
);
CREATE TABLE IF NOT EXISTS psts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    last_modified INTEGER,
    chunk_size INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'uploading',
    path TEXT NOT NULL DEFAULT '',
    scan TEXT,
    error TEXT,
    pst_sha TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS psts_key ON psts(filename, size, last_modified);
CREATE TABLE IF NOT EXISTS upload_chunks (
    pst_id INTEGER NOT NULL REFERENCES psts(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (pst_id, n)
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    login TEXT NOT NULL DEFAULT '',
    password_enc TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    last_test REAL,
    last_test_result TEXT,
    pst_id INTEGER REFERENCES psts(id) ON DELETE SET NULL,
    opt_mail INTEGER NOT NULL DEFAULT 1,
    opt_calendar INTEGER NOT NULL DEFAULT 1,
    opt_contacts INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    source_kind TEXT,
    source_config TEXT,
    source_secret_enc TEXT,
    target_kind TEXT,
    target_config TEXT,
    target_secret_enc TEXT,
    UNIQUE (server_id, email)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT NOT NULL DEFAULT 'precheck',
    progress TEXT NOT NULL DEFAULT '{}',
    current_folder TEXT,
    rate REAL,
    rate_bytes REAL,
    eta REAL,
    mode TEXT NOT NULL DEFAULT 'full',
    worker_id TEXT,
    heartbeat REAL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    report_path TEXT,
    created_at REAL NOT NULL,
    tuning TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_account ON jobs(account_id);
{ITEMS}
CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    msg TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_logs_job ON job_logs(job_id, id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    ts REAL NOT NULL DEFAULT ((julianday('now') - 2440587.5) * 86400.0)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TRIGGER IF NOT EXISTS ev_job_i AFTER INSERT ON jobs BEGIN INSERT INTO events(kind, ref_id) VALUES ('job', NEW.id); END;
CREATE TRIGGER IF NOT EXISTS ev_job_u AFTER UPDATE ON jobs BEGIN INSERT INTO events(kind, ref_id) VALUES ('job', NEW.id); END;
CREATE TRIGGER IF NOT EXISTS ev_acc_i AFTER INSERT ON accounts BEGIN INSERT INTO events(kind, ref_id) VALUES ('account', NEW.id); END;
CREATE TRIGGER IF NOT EXISTS ev_acc_u AFTER UPDATE ON accounts BEGIN INSERT INTO events(kind, ref_id) VALUES ('account', NEW.id); END;
CREATE TRIGGER IF NOT EXISTS ev_pst_i AFTER INSERT ON psts BEGIN INSERT INTO events(kind, ref_id) VALUES ('pst', NEW.id); END;
CREATE TRIGGER IF NOT EXISTS ev_pst_u AFTER UPDATE ON psts BEGIN INSERT INTO events(kind, ref_id) VALUES ('pst', NEW.id); END;
CREATE TRIGGER IF NOT EXISTS ev_chunk_i AFTER INSERT ON upload_chunks BEGIN INSERT INTO events(kind, ref_id) VALUES ('upload', NEW.pst_id); END;
"""

SCHEMA = SCHEMA_TEMPLATE.replace("{ITEMS}", ITEMS_DDL.replace("{name}", "items"))


def pst_key_of(pst_row) -> str | None:
    """Key attaching items to a PST: its fingerprint (stable across uploads of the same file)."""
    if pst_row is None:
        return None
    return pst_row["pst_sha"] or f"id:{pst_row['id']}"


DEFAULT_WORKERS = 4  # servers.workers default (schema)
# `workers` values that earlier versions used as their default (schema: 4, 1.0.0 web UI: 2): such servers
# were not tuned by hand and become 'auto'; any other value was chosen by the administrator (stays manual)
DEFAULT_WORKERS_LEGACY = (2, 4)


def _add_column(conn: sqlite3.Connection, table: str, ddl: str) -> None:
    name = ddl.split()[0]
    if name in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():  # added in the meantime by the other process
            raise


def _migrate_tuning(conn: sqlite3.Connection) -> None:
    """servers.tuning ('auto' | 'manual') + servers.builders (manual builder count, NULL = machine default).
    Servers of earlier versions become 'auto' when `workers` is one of the earlier defaults (2: 1.0.0 UI, 4:
    schema), otherwise the administrator chose that value: they stay 'manual' with it. Idempotent: runs only
    while the column is missing. Columns added and values set in ONE
    transaction (re-checked under the write lock): a concurrent process sees either nothing or everything."""
    if "tuning" in {r[1] for r in conn.execute("PRAGMA table_info(servers)")}:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "tuning" not in {r[1] for r in conn.execute("PRAGMA table_info(servers)")}:
            conn.execute("ALTER TABLE servers ADD COLUMN tuning TEXT NOT NULL DEFAULT 'auto'")
            if "builders" not in {r[1] for r in conn.execute("PRAGMA table_info(servers)")}:
                conn.execute("ALTER TABLE servers ADD COLUMN builders INTEGER")
            marks = ",".join("?" * len(DEFAULT_WORKERS_LEGACY))
            conn.execute(f"UPDATE servers SET tuning = CASE WHEN workers IN ({marks}) THEN 'auto' ELSE 'manual' END",
                         DEFAULT_WORKERS_LEGACY)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrations: jobs.rate_bytes, jobs.tuning, servers.tuning/builders, items(pst_id) -> items(pst_key = PST
    fingerprint), kv keys `pst:<id>:crash_skips` -> `pst:<sha>:...`, accounts.source_* / target_* (NULL: the PST
    of the account, written to its server) and items.source_ref."""
    for ddl in ("source_kind TEXT", "source_config TEXT", "source_secret_enc TEXT",
                "target_kind TEXT", "target_config TEXT", "target_secret_enc TEXT"):
        _add_column(conn, "accounts", ddl)
    _add_column(conn, "items", "source_ref TEXT")
    _add_column(conn, "jobs", "rate_bytes REAL")  # bytes/s (sliding 60 s)
    _add_column(conn, "jobs", "tuning TEXT")      # JSON {mode, builders, connections, fallback}
    _migrate_tuning(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    if "pst_key" not in cols:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "pst_key" in {r[1] for r in conn.execute("PRAGMA table_info(items)")}:
                conn.execute("COMMIT")  # migrated in the meantime by the other process
                conn.executescript(ITEMS_INDEXES)
                return
            conn.execute("DROP TABLE IF EXISTS items_new")
            conn.execute(ITEMS_DDL.replace("{name}", "items_new"))
            # on collision (same PST uploaded twice), keep the most advanced state
            conn.execute(
                f"INSERT OR IGNORE INTO items_new({ITEMS_COLS}) "
                "SELECT i.id, i.account_id, i.pst_id, COALESCE(p.pst_sha, 'id:' || i.pst_id), i.kind, i.item_key, "
                "i.folder, i.target, i.status, i.error, i.warnings, i.subject, i.hash, i.size, i.job_id, i.updated_at "
                "FROM items i LEFT JOIN psts p ON p.id = i.pst_id ORDER BY CASE i.status WHEN 'ok' THEN 0 "
                "WHEN 'inflight' THEN 1 WHEN 'skipped' THEN 2 WHEN 'error' THEN 3 ELSE 4 END, i.id")
            conn.execute("DROP TABLE items")
            conn.execute("ALTER TABLE items_new RENAME TO items")
            for r in conn.execute("SELECT key FROM kv WHERE key LIKE 'pst:%:crash_skips'").fetchall():
                mid = r[0][4:-len(":crash_skips")]
                if mid.isdigit():
                    p = conn.execute("SELECT id, pst_sha FROM psts WHERE id=?", (int(mid),)).fetchone()
                    new = f"pst:{pst_key_of(p)}:crash_skips" if p else None
                    if new and new != r[0]:
                        conn.execute("INSERT OR REPLACE INTO kv(key, value) SELECT ?, value FROM kv WHERE key=?",
                                     (new, r[0]))
                    conn.execute("DELETE FROM kv WHERE key=?", (r[0],))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    conn.executescript(ITEMS_INDEXES)


_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()


def _connect() -> sqlite3.Connection:
    settings.ensure_dirs()
    path = str(settings.db_path)
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    with _init_lock:
        if path not in _initialized:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _migrate(conn)
            _initialized.add(path)
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    path = str(settings.db_path)
    if c is None or getattr(_local, "path", None) != path:
        c = _connect()
        _local.conn = c
        _local.path = path
    return c


def reset() -> None:
    """Forget the connections (tests)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None
    _initialized.clear()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    c = conn()
    for attempt in range(50):
        try:
            c.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 49:
                raise
            time.sleep(0.1)
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def q(sql: str, params: Any = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def q1(sql: str, params: Any = ()) -> sqlite3.Row | None:
    return conn().execute(sql, params).fetchone()


def ex(sql: str, params: Any = ()) -> sqlite3.Cursor:
    for attempt in range(50):
        try:
            return conn().execute(sql, params)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or attempt == 49:
                raise
            time.sleep(0.1)
    raise RuntimeError("unreachable")


def jload(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def jdump(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


def kv_set(key: str, value: str) -> None:
    ex("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def kv_get(key: str) -> str | None:
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    return r["value"] if r else None
