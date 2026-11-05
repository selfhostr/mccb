"""Configuration (environment variables)."""
from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Settings:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        self.admin_password = os.environ.get("ADMIN_PASSWORD", "")
        self.secret_key_env = os.environ.get("SECRET_KEY", "")
        self.port = _int("PORT", 22362)
        self.chunk_size = _int("UPLOAD_CHUNK_SIZE", 32 * 1024 * 1024)
        self.disk_margin = _int("UPLOAD_DISK_MARGIN", 512 * 1024 * 1024)
        self.session_max_age = _int("SESSION_MAX_AGE", 7 * 24 * 3600)
        self.cookie_secure = os.environ.get("COOKIE_SECURE", "0") == "1"
        self.engine_module = os.environ.get("PSTMIG_ENGINE", "app.engine")
        self.web_dir = Path(os.environ.get("WEB_DIR", str(Path(__file__).resolve().parent.parent / "web")))
        self.worker_concurrency = _int("WORKER_CONCURRENCY", 2)
        self.verify_sample = _int("VERIFY_SAMPLE", 20)
        self.max_retries = _int("IMAP_MAX_RETRIES", 5)
        self.heartbeat_stale = _int("WORKER_STALE_SECONDS", 60)
        # native crashes (libpff) tolerated per job before permanent failure
        self.max_child_crashes = max(1, _int("MAX_CHILD_CRASHES", 3))
        # time allowed for subprocesses to stop (SIGTERM) before SIGKILL
        self.child_term_grace = _int("CHILD_TERM_GRACE", 20)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def pst_dir(self) -> Path:
        return self.data_dir / "pst"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.pst_dir, self.reports_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
