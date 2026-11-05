import hashlib
import os

import pytest

from tests.server.fake_engine import make_fake_pst


def _put(client, uid, n, data, sha=None):
    return client.put(f"/api/uploads/{uid}/chunks/{n}", content=data,
                      headers={"X-Chunk-Sha256": sha or hashlib.sha256(data).hexdigest(),
                               "Content-Type": "application/octet-stream"})


def _validate(env):
    from app.server.worker import Worker
    w = Worker()
    w.tick()
    for t in list(w.validating.values()):
        t.join(10)


def test_resumable_upload_roundtrip(client, env, tmp_path):
    src = tmp_path / "client été.pst"
    data = make_fake_pst(str(src), 300_000)
    meta = {"filename": "client été.pst", "size": len(data), "last_modified": 1700000000000}
    r = client.post("/api/uploads", json=meta)
    assert r.status_code == 200, r.text
    up = r.json()
    cs = up["chunk_size"]
    assert cs == 64 * 1024 and up["received"] == []
    chunks = [data[i:i + cs] for i in range(0, len(data), cs)]
    # chunk au hash faux → 422, non enregistré
    assert _put(client, up["id"], 0, chunks[0], sha="0" * 64).status_code == 422
    # chunk de taille incorrecte → 422
    assert _put(client, up["id"], 1, chunks[1][:-1]).status_code == 422
    # désordre + doublon
    for n in (2, 0, 2):
        assert _put(client, up["id"], n, chunks[n]).status_code == 200
    assert _put(client, up["id"], 99, b"x").status_code == 416
    # complete prématuré → 409
    assert client.post(f"/api/uploads/{up['id']}/complete").status_code == 409
    # reprise : même fichier = même upload
    r2 = client.post("/api/uploads", json=meta).json()
    assert r2["id"] == up["id"] and r2["received"] == [0, 2]
    for n in range(len(chunks)):
        if n not in r2["received"]:
            assert _put(client, up["id"], n, chunks[n]).status_code == 200
    g = client.get(f"/api/uploads/{up['id']}").json()
    assert g["received"] == list(range(len(chunks))) and g["status"] == "uploading"
    c = client.post(f"/api/uploads/{up['id']}/complete").json()
    assert c["status"] == "validating" and c["pst_id"] == up["id"] and c["pst"]["status"] == "validating"
    path = env.pst_dir / f"{up['id']}.pst"
    assert path.read_bytes() == data
    _validate(env)
    p = client.get(f"/api/psts/{up['id']}").json()
    assert p["status"] == "valid", p
    assert p["scan"]["totals"]["mail"] == 11 and p["filename"] == "client été.pst"
    assert client.get("/api/psts").json()[0]["id"] == up["id"]
    # chunk renvoyé après validation : idempotent si identique, refusé sinon
    assert _put(client, up["id"], 0, chunks[0]).status_code == 200
    assert _put(client, up["id"], 0, b"y" * len(chunks[0])).status_code == 409
    # association à un compte puis suppression
    sid = client.post("/api/servers", json={"name": "s", "imap_host": "h"}).json()["id"]
    aid = client.post("/api/accounts", json={"server_id": sid, "email": "u@example.test", "password": "p"}).json()["id"]
    assert client.put(f"/api/accounts/{aid}/pst", json={"pst_id": up["id"]}).json()["pst_id"] == up["id"]
    assert client.delete(f"/api/psts/{up['id']}").status_code == 200
    assert not path.exists()
    assert client.get(f"/api/accounts/{aid}").json()["pst_id"] is None


def test_invalid_signature(client, env):
    data = b"PAS UN PST" + b"\0" * 1000
    up = client.post("/api/uploads", json={"filename": "x.pst", "size": len(data), "last_modified": 1}).json()
    assert _put(client, up["id"], 0, data).status_code == 200
    assert client.post(f"/api/uploads/{up['id']}/complete").json()["status"] == "validating"
    _validate(env)
    p = client.get(f"/api/psts/{up['id']}").json()
    assert p["status"] == "invalid" and "BDN" in p["error"]


def test_disk_space_checked(client, env):
    r = client.post("/api/uploads", json={"filename": "big.pst", "size": 1 << 60, "last_modified": 1})
    assert r.status_code == 507


def test_streaming_large_chunk(client, env, monkeypatch):
    """Un chunk de plusieurs Mo est écrit en streaming à son offset."""
    from app.server.config import settings
    monkeypatch.setattr(settings, "chunk_size", 8 * 1024 * 1024)
    data = b"!BDN" + os.urandom(8 * 1024 * 1024 + 1000 - 4)
    up = client.post("/api/uploads", json={"filename": "m.pst", "size": len(data), "last_modified": 2}).json()
    assert up["chunk_size"] == 8 * 1024 * 1024
    assert _put(client, up["id"], 1, data[8 * 1024 * 1024:]).status_code == 200
    assert _put(client, up["id"], 0, data[:8 * 1024 * 1024]).status_code == 200
    client.post(f"/api/uploads/{up['id']}/complete")
    assert (env.pst_dir / f"{up['id']}.pst").read_bytes() == data


def test_validation_crash_marks_invalid(client, env, monkeypatch):
    """L'analyse tourne dans un sous-processus : un crash natif (SIGSEGV simulé) → PST « invalid »."""
    monkeypatch.setenv("PSTMIG_TEST_CRASH", "validate")
    data = make_fake_pst(str(env.data_dir / "c.pst"), 70_000)
    up = client.post("/api/uploads", json={"filename": "c.pst", "size": len(data), "last_modified": 3}).json()
    cs = up["chunk_size"]
    for n in range(up["chunks"]):
        assert _put(client, up["id"], n, data[n * cs:(n + 1) * cs]).status_code == 200
    assert client.post(f"/api/uploads/{up['id']}/complete").json()["status"] == "validating"
    _validate(env)
    p = client.get(f"/api/psts/{up['id']}").json()
    assert p["status"] == "invalid" and "crash libpff pendant l'analyse" in p["error"], p
    assert "SIGSEGV" in p["error"]
