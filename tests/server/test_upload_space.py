import hashlib

from app.server import uploads


def _sparse(monkeypatch, free):
    monkeypatch.setattr(uploads, "_fallocate_supported", lambda: False)
    monkeypatch.setattr(uploads, "free_space", lambda: free)


def test_sparse_subtracts_other_uploads(client, env, monkeypatch):
    _sparse(monkeypatch, 1_000_000)
    r = client.post("/api/uploads", json={"filename": "a.pst", "size": 700_000, "last_modified": 1})
    assert r.status_code == 200
    r = client.post("/api/uploads", json={"filename": "b.pst", "size": 400_000, "last_modified": 2})
    assert r.status_code == 507 and "insufficient disk space" in r.text
    r = client.post("/api/uploads", json={"filename": "c.pst", "size": 200_000, "last_modified": 3})
    assert r.status_code == 200


def test_fallocate_does_not_subtract(client, env, monkeypatch):
    monkeypatch.setattr(uploads, "_fallocate_supported", lambda: True)
    monkeypatch.setattr(uploads, "free_space", lambda: 1_000_000)
    assert client.post("/api/uploads", json={"filename": "a.pst", "size": 700_000, "last_modified": 1}).status_code == 200
    assert client.post("/api/uploads", json={"filename": "b.pst", "size": 400_000, "last_modified": 2}).status_code == 200


def test_resume_checks_space_when_file_missing(client, env, monkeypatch):
    _sparse(monkeypatch, 1_000_000)
    meta = {"filename": "a.pst", "size": 500_000, "last_modified": 1}
    up = client.post("/api/uploads", json=meta).json()
    import os
    from app.server import db
    os.unlink(db.q1("SELECT path FROM psts WHERE id=?", (up["id"],))["path"])
    monkeypatch.setattr(uploads, "free_space", lambda: 100_000)
    r = client.post("/api/uploads", json=meta)
    assert r.status_code == 507 and "insufficient disk space" in r.text


def test_resume_sparse_checks_remaining(client, env, monkeypatch):
    _sparse(monkeypatch, 1_000_000)
    meta = {"filename": "a.pst", "size": 500_000, "last_modified": 1}
    assert client.post("/api/uploads", json=meta).status_code == 200
    monkeypatch.setattr(uploads, "free_space", lambda: 1_000)
    assert client.post("/api/uploads", json=meta).status_code == 507


def test_concurrent_same_chunk_ok(client, env):
    import concurrent.futures as cf
    data = b"x" * 1000
    up = client.post("/api/uploads", json={"filename": "a.pst", "size": 1000, "last_modified": 1}).json()
    h = {"X-Chunk-Sha256": hashlib.sha256(data).hexdigest()}
    with cf.ThreadPoolExecutor(4) as ex:
        rs = list(ex.map(lambda _: client.put(f"/api/uploads/{up['id']}/chunks/0", content=data, headers=h), range(4)))
    assert all(r.status_code == 200 for r in rs)
    assert client.get(f"/api/uploads/{up['id']}").json()["received"] == [0]
