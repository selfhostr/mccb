import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PSTMIG_ENGINE", "tests.server.fake_engine")
os.environ["ADMIN_PASSWORD"] = "test-admin-é"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PSTMIG_ENGINE", "tests.server.fake_engine")
    monkeypatch.setenv("UPLOAD_CHUNK_SIZE", str(64 * 1024))
    monkeypatch.setenv("UPLOAD_DISK_MARGIN", "0")
    monkeypatch.setenv("WORKER_STALE_SECONDS", "60")
    monkeypatch.setenv("PSTMIG_FAKE_CONFIG", str(tmp_path / "fake_engine.json"))
    monkeypatch.delenv("PSTMIG_TEST_CRASH", raising=False)
    # parallel builders, kept small so that timing-based tests (pause/stop mid-folder) stay meaningful
    monkeypatch.setenv("BUILD_PROCESSES", os.environ.get("TEST_BUILD_PROCESSES", "2"))
    from app.server import db, security
    from app.server.config import settings
    settings.reload()
    db.reset()
    security.reset_cache()
    from tests.server import fake_engine
    fake_engine.CONFIG.save()  # current state visible to the worker subprocesses
    yield settings
    db.reset()


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    from app.server.web import app
    with TestClient(app) as c:
        r = c.post("/api/login", json={"password": "test-admin-é"})
        assert r.status_code == 200, r.text
        yield c
