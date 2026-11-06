"""CSV import of accounts: `email;password[;login]`, `;`, `,` or tab separator, optional header,
UTF-8 (with or without BOM), UTF-16 (with or without BOM: Excel "Unicode text") or cp1252.

With a header, optional columns may follow (any order, any of them): `pst` (file name of an uploaded PST), `mail`,
`calendar`, `contacts` (content toggles: 1/0, yes/no, true/false, oui/non), `source_kind` and `source_<field>` (the
settings of the source connector, `source_password` for its secret). A file without a header stays
`email;password;login`. The export (`GET /api/accounts/export-csv`) writes the same columns, never a password."""
from __future__ import annotations

import csv
import io
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEADER_WORDS = {"email", "e-mail", "mail", "adresse", "courriel", "login", "identifiant", "password",
                "mot de passe", "motdepasse", "mdp", "pass"}
TOGGLES = ("mail", "calendar", "contacts")
TRUE_WORDS = {"1", "true", "yes", "y", "oui", "o", "x", "on"}
FALSE_WORDS = {"0", "false", "no", "n", "non", "off", ""}
# the first column named `mail` is the address in a file of earlier versions: it is a toggle only next to `email`


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
    """Returns (valid rows [{line,email,password,login}], errors [{line,msg}]). A row gains `pst`, `options`
    (partial {mail,calendar,contacts}) and `source` ({kind, config, secrets}) only when its file has those columns."""
    rows, errors, _extended = parse_ext(data)
    return rows, errors


def parse_ext(data: bytes) -> tuple[list[dict], list[dict], bool]:
    """Like parse, plus whether the header has columns beyond email/password/login. In such a file a blank password
    is accepted (an export has none): the importer keeps the one of an existing account."""
    text = decode_bytes(data)
    delim = sniff_delimiter(text)
    rows, errors = [], []
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    cols = {"email": 0, "password": 1, "login": 2}
    extra: dict[str, int] = {}   # optional column -> index
    first = True
    for lineno, rec in enumerate(reader, start=1):
        rec = [c.strip() for c in rec]
        if not any(rec):
            continue
        if first:
            first = False
            low = [c.lower() for c in rec]
            if low and low[0] in HEADER_WORDS or (low and "@" not in low[0] and any(w in HEADER_WORDS for w in low)):
                has_email = any(c in ("email", "e-mail", "adresse", "courriel") for c in low)
                cols = {"email": -1, "password": -1, "login": -1}
                for i, c in enumerate(low):
                    if c in ("email", "e-mail", "adresse", "courriel") or (c == "mail" and not has_email):
                        cols["email"] = i
                    elif c in ("password", "mot de passe", "motdepasse", "mdp", "pass"):
                        cols["password"] = i
                    elif c in ("login", "identifiant"):
                        cols["login"] = i
                    elif c in TOGGLES or c in ("pst", "source_kind") or c.startswith("source_"):
                        extra[c] = i
                continue
        def get(k: str) -> str:
            i = cols[k]
            return rec[i] if 0 <= i < len(rec) else ""
        email, password, login = get("email"), get("password"), get("login")
        if not EMAIL_RE.match(email):
            errors.append({"line": lineno, "msg": f"invalid email address: {email!r}"})
            continue
        if not password and not extra:
            errors.append({"line": lineno, "msg": f"missing password for {email}"})
            continue
        row = {"line": lineno, "email": email, "password": password, "login": login}
        if extra:
            bad = _extended(row, rec, extra)
            if bad:
                errors.append({"line": lineno, "msg": bad})
                continue
        rows.append(row)
    return rows, errors, bool(extra)


def _extended(row: dict, rec: list[str], extra: dict[str, int]) -> str | None:
    """Fills the optional columns of a row. Returns a message when one of them is wrong."""
    def cell(name: str) -> str:
        i = extra.get(name, -1)
        return rec[i] if 0 <= i < len(rec) else ""
    if "pst" in extra and cell("pst"):
        row["pst"] = cell("pst")
    opts = {}
    for t in TOGGLES:
        if t not in extra:
            continue
        v = cell(t).strip().lower()
        if not v:
            continue  # empty cell: unchanged
        if v not in TRUE_WORDS | FALSE_WORDS:
            return f"{t}: expected 1/0, yes/no or true/false, got {cell(t)!r}"
        opts[t] = v in TRUE_WORDS
    if opts:
        row["options"] = opts
    kind = cell("source_kind").lower() if "source_kind" in extra else ""
    cfg = {n[len("source_"):]: cell(n) for n in extra if n.startswith("source_") and n != "source_kind" and cell(n)}
    if kind or cfg:
        if not kind:
            return "source_* columns need source_kind"
        row["source"] = {"kind": kind, "config": cfg}
    return None


TEMPLATE = "email;password;login\r\njohn.doe@example.com;Password;\r\n"
