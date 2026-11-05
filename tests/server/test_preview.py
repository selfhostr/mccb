import io
import zipfile

from tests.server.fake_engine import make_fake_pst


def _make_pst(env, status="valid"):
    from app.server import db
    path = str(env.pst_dir / "1.pst")
    env.ensure_dirs()
    make_fake_pst(path)
    with db.conn() as c:
        c.execute("INSERT INTO psts(filename,size,chunk_size,status,path,created_at,updated_at) VALUES(?,?,?,?,?,0,0)", ("Mon été.pst", 300000, 65536, status, path))
    return db.q1("SELECT id FROM psts ORDER BY id DESC LIMIT 1")["id"]


def test_preview_zip(client, env):
    pid = _make_pst(env)
    r = client.get(f"/api/psts/{pid}/preview?limit=9")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and "preview-" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert len(names) == 9
    assert any(n.endswith(".eml") for n in names)
    assert any(n.endswith(".ics") for n in names)
    assert any(n.endswith(".vcf") for n in names)
    assert any(n.startswith("Calendrier/") for n in names)
    eml = next(n for n in names if n.endswith(".eml"))
    assert "Hélène".encode() in z.read(eml) or b"=?utf-8?" in z.read(eml).lower() or b"Content-Type" in z.read(eml)


def test_preview_not_valid_and_unknown(client, env):
    pid = _make_pst(env, status="invalid")
    assert client.get(f"/api/psts/{pid}/preview").status_code == 409
    assert client.get("/api/psts/9999/preview").status_code == 404


def test_preview_crash(client, env, monkeypatch):
    from app.server import preview
    pid = _make_pst(env)

    async def boom(*a, **k):
        raise preview.PreviewCrash()
    monkeypatch.setattr(preview, "build_zip", boom)
    r = client.get(f"/api/psts/{pid}/preview")
    assert r.status_code == 500 and "libpff" in r.json()["detail"]


def test_preview_unauth(env):
    from fastapi.testclient import TestClient
    from app.server.web import app
    with TestClient(app) as c:
        assert c.get("/api/psts/1/preview").status_code == 401
