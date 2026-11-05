"""Secret key, Fernet encryption of passwords, signed session."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import settings

_cache: dict[str, object] = {}


def secret_key() -> bytes:
    if "key" in _cache:
        return _cache["key"]  # type: ignore[return-value]
    if settings.secret_key_env:
        raw = settings.secret_key_env.encode()
    else:
        settings.ensure_dirs()
        p = settings.data_dir / "secret.key"
        if not p.exists():
            tmp = p.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                f.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, p)  # atomic: the first process wins
            except FileExistsError:
                pass
            tmp.unlink(missing_ok=True)
        raw = p.read_bytes().strip()
    _cache["key"] = raw
    return raw


def _fernet() -> Fernet:
    if "fernet" not in _cache:
        k = hashlib.sha256(b"fernet:" + secret_key()).digest()
        _cache["fernet"] = Fernet(base64.urlsafe_b64encode(k))
    return _cache["fernet"]  # type: ignore[return-value]


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError("password cannot be decrypted (SECRET_KEY changed?)")


def _signer() -> TimestampSigner:
    return TimestampSigner(hashlib.sha256(b"session:" + secret_key()).hexdigest(), salt="psttoimap-session")


def _admin_file() -> Path:
    return settings.data_dir / "admin.json"


def _hash_pw(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def _write_admin(pw: str, generated: bool) -> dict:
    """Persist the admin password (scrypt hash only) in DATA_DIR/admin.json."""
    settings.ensure_dirs()
    salt = secrets.token_bytes(16)
    rec = {"salt": salt.hex(), "hash": _hash_pw(pw, salt), "gen": secrets.token_hex(8),
           "generated": generated,
           "created": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
    p = _admin_file()
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    _cache.pop("admin", None)
    return rec


def _admin_rec() -> dict | None:
    if "admin" not in _cache:
        try:
            _cache["admin"] = json.loads(_admin_file().read_text())
        except (OSError, ValueError):
            _cache["admin"] = None
    return _cache["admin"]  # type: ignore[return-value]


def password_from_env() -> bool:
    return bool(settings.admin_password)


def ensure_admin_password() -> str | None:
    """Called once at web startup. Returns a newly generated password (to be shown once) or None."""
    if settings.admin_password:
        return None
    settings.ensure_dirs()
    if _admin_file().exists():
        return None
    pw = secrets.token_urlsafe(18)
    # exclusive creation guard: the first process wins
    try:
        fd = os.open(_admin_file().with_suffix(".lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        return None
    try:
        _write_admin(pw, True)
    finally:
        _admin_file().with_suffix(".lock").unlink(missing_ok=True)
    return pw


def admin_created() -> str:
    rec = _admin_rec()
    return rec["created"] if rec else "?"


def set_admin_password(pw: str) -> None:
    """Change the stored admin password (not allowed when ADMIN_PASSWORD is set). Invalidates sessions."""
    _write_admin(pw, False)


def _generation() -> str:
    """Fingerprint of the admin credential: changing the password invalidates sessions."""
    if settings.admin_password:
        material = b"gen:" + settings.admin_password.encode("utf-8")
    else:
        rec = _admin_rec()
        material = b"genfile:" + (rec["gen"] + rec["hash"] if rec else "").encode()
    return hmac.new(secret_key(), material, hashlib.sha256).hexdigest()[:16]


def make_session() -> str:
    return _signer().sign(f"{secrets.token_hex(8)}.{_generation()}").decode()


def check_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        val = _signer().unsign(token, max_age=settings.session_max_age).decode()
        return hmac.compare_digest(val.rpartition(".")[2], _generation())
    except (BadSignature, SignatureExpired):
        return False


def check_password(pw: str) -> bool:
    if settings.admin_password:
        return hmac.compare_digest(pw.encode("utf-8"), settings.admin_password.encode("utf-8"))
    rec = _admin_rec()
    if not rec:
        return False
    return hmac.compare_digest(_hash_pw(pw, bytes.fromhex(rec["salt"])), rec["hash"])


def reset_cache() -> None:
    _cache.clear()
