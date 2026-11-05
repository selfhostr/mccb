"""CSV import of accounts: `email;password[;login]`, `;`, `,` or tab separator, optional header,
UTF-8 (with or without BOM), UTF-16 (with or without BOM: Excel "Unicode text") or cp1252."""
from __future__ import annotations

import csv
import io
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEADER_WORDS = {"email", "e-mail", "mail", "adresse", "courriel", "login", "identifiant", "password",
                "mot de passe", "motdepasse", "mdp", "pass"}


def _utf16_without_bom(data: bytes) -> str | None:
    """Excel "Unicode text" without BOM: ASCII-heavy UTF-16, NULs on one side."""
    if len(data) < 4 or len(data) % 2:
        return None
    even, odd = data[0::2], data[1::2]
    for zeros, enc in ((odd, "utf-16-le"), (even, "utf-16-be")):
        if zeros.count(0) >= len(zeros) * 0.3:
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                return None
    return None


def decode_bytes(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    elif b"\x00" in data:
        t = _utf16_without_bom(data)
        if t is not None:
            return t
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # cp1252 (Excel "CSV" on a Western Windows); latin-1 for its 5 undefined bytes
        return "".join(chr(b) if b in (0x81, 0x8D, 0x8F, 0x90, 0x9D) else bytes([b]).decode("cp1252")
                       for b in data)


def sniff_delimiter(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            if line.count("\t") > max(line.count(";"), line.count(",")):
                return "\t"
            return ";" if line.count(";") >= line.count(",") and ";" in line else ("," if "," in line else ";")
    return ";"


def parse(data: bytes) -> tuple[list[dict], list[dict]]:
    """Returns (valid rows [{line,email,password,login}], errors [{line,msg}])."""
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
            errors.append({"line": lineno, "msg": f"invalid email address: {email!r}"})
            continue
        if not password:
            errors.append({"line": lineno, "msg": f"missing password for {email}"})
            continue
        rows.append({"line": lineno, "email": email, "password": password, "login": login})
    return rows, errors


TEMPLATE = "email;password;login\r\njohn.doe@example.com;Password;\r\n"
