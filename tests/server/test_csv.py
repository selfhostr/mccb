import io

from app.server import csvimport

SRV = {"name": "S", "imap_host": "h", "imap_port": 143, "imap_security": "none"}


def test_parse_semicolon_with_header_utf8():
    rows, errs = csvimport.parse("email;password;login\nzoé@exemple.fr;mötdepasse;zoe\n".encode("utf-8"))
    assert errs == [] and rows[0]["email"] == "zoé@exemple.fr" and rows[0]["password"] == "mötdepasse"
    assert rows[0]["login"] == "zoe"


def test_parse_comma_no_header():
    rows, errs = csvimport.parse(b"a@b.fr,pw1\nc@d.fr,pw2,login2\n")
    assert [r["email"] for r in rows] == ["a@b.fr", "c@d.fr"] and rows[1]["login"] == "login2"
    assert errs == []


def test_parse_cp1252_and_bom():
    rows, _ = csvimport.parse("jose@exemple.fr;café€\n".encode("cp1252"))
    assert rows[0]["password"] == "café€"
    rows, _ = csvimport.parse(("﻿Email;Mot de passe\nx@y.fr;Été\n").encode("utf-8"))
    assert rows == [{"line": 2, "email": "x@y.fr", "password": "Été", "login": ""}]


def test_parse_errors_reported_with_line():
    rows, errs = csvimport.parse(b"email;password\npas-un-mail;x\nok@a.fr;\n\nbon@a.fr;p\n")
    assert [r["email"] for r in rows] == ["bon@a.fr"]
    assert [e["line"] for e in errs] == [2, 3]


def test_import_csv_endpoint(client):
    sid = client.post("/api/servers", json=SRV).json()["id"]
    data = "email;password\nanne@exemple.fr;pâté\nbad;x\n".encode("cp1252")
    r = client.post("/api/accounts/import-csv", data={"server_id": str(sid)},
                    files={"file": ("comptes.csv", io.BytesIO(data), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and r.json()["updated"] == 0 and r.json()["errors"][0]["line"] == 3
    r = client.post("/api/accounts/import-csv", data={"server_id": str(sid)},
                    files={"file": ("c.csv", io.BytesIO(b"anne@exemple.fr,nouveau,anne\n"), "text/csv")})
    assert r.json() == {"created": 0, "updated": 1, "errors": []}
    acc = client.get(f"/api/accounts?server_id={sid}").json()
    assert acc[0]["login"] == "anne"
    t = client.get("/api/accounts/csv-template")
    assert t.status_code == 200 and "email;password" in t.content.decode("utf-8-sig")
