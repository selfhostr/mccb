"""Import CSV des comptes : `email;password[;login]`, séparateur `;` ou `,`, entête optionnel,
encodage UTF-8 (avec ou sans BOM) ou cp1252."""
from __future__ import annotations

import csv
import io
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEADER_WORDS = {"email", "e-mail", "mail", "adresse", "courriel", "login", "identifiant", "password",
                "mot de passe", "motdepasse", "mdp", "pass"}


def decode_bytes(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def sniff_delimiter(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return ";" if line.count(";") >= line.count(",") and ";" in line else ("," if "," in line else ";")
    return ";"


def parse(data: bytes) -> tuple[list[dict], list[dict]]:
    """Retourne (lignes valides [{line,email,password,login}], erreurs [{line,msg}])."""
    text = decode_bytes(data)
    delim = sniff_delimiter(text)
    rows, errors = [], []
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    cols = {"email": 0, "password": 1, "login": 2}
    first = True
    for lineno, rec in enumerate(reader, start=1):
        rec = [c.strip() for c in rec]
        if not any(rec):
            continue
        if first:
            first = False
            low = [c.lower() for c in rec]
            if low and low[0] in HEADER_WORDS or (low and "@" not in low[0] and any(w in HEADER_WORDS for w in low)):
                for i, c in enumerate(low):
                    if c in ("email", "e-mail", "mail", "adresse", "courriel"):
                        cols["email"] = i
                    elif c in ("password", "mot de passe", "motdepasse", "mdp", "pass"):
                        cols["password"] = i
                    elif c in ("login", "identifiant"):
                        cols["login"] = i
                continue
        def get(k: str) -> str:
            i = cols[k]
            return rec[i] if i < len(rec) else ""
        email, password, login = get("email"), get("password"), get("login")
        if not EMAIL_RE.match(email):
            errors.append({"line": lineno, "msg": f"adresse e-mail invalide : {email!r}"})
            continue
        if not password:
            errors.append({"line": lineno, "msg": f"mot de passe manquant pour {email}"})
            continue
        rows.append({"line": lineno, "email": email, "password": password, "login": login})
    return rows, errors


TEMPLATE = "email;password;login\r\njean.dupont@exemple.fr;MotDePasse;\r\n"
