"""Clé secrète, chiffrement Fernet des mots de passe, session signée."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

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
                os.link(tmp, p)  # atomique : le premier process gagne
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
        raise ValueError("mot de passe indéchiffrable (SECRET_KEY changée ?)")


def _signer() -> TimestampSigner:
    return TimestampSigner(hashlib.sha256(b"session:" + secret_key()).hexdigest(), salt="psttoimap-session")


def _generation() -> str:
    """Empreinte du mot de passe admin : changer ADMIN_PASSWORD invalide les sessions."""
    return hmac.new(secret_key(), b"gen:" + settings.admin_password.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


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
    if not settings.admin_password:
        return False
    return hmac.compare_digest(pw.encode("utf-8"), settings.admin_password.encode("utf-8"))


def reset_cache() -> None:
    _cache.clear()
