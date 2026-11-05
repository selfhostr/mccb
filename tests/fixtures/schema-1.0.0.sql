-- SQLite schema of mccb 1.0.0 (git show v1.0.0:app/server/db.py, SCHEMA): upgrade tests
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
    created_at REAL NOT NULL
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
    eta REAL,
    mode TEXT NOT NULL DEFAULT 'full',
    worker_id TEXT,
    heartbeat REAL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    report_path TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_account ON jobs(account_id);

CREATE TABLE IF NOT EXISTS items (
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
    UNIQUE (account_id, pst_key, kind, item_key)
);

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
