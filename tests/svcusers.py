"""Utilisateurs de test des services d'intégration (docker-compose.test.yml).

GreenMail tourne AVEC authentification : chaque utilisateur est créé via son API REST
(`TEST_GREENMAIL_API`, POST /api/user). Radicale utilise un fichier htpasswd (mots de passe en clair)
partagé par volume (`TEST_DAV_HTPASSWD`) ; Radicale relit le fichier à chaque authentification.
Sans ces variables (services lancés à la main, auth désactivée), les fonctions ne font rien.
"""
from __future__ import annotations

import os
import threading
import uuid

_lock = threading.Lock()


def new_email(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}@example.test"


def create_user(email: str | None = None, password: str = "pw", prefix: str = "u") -> str:
    """Crée l'utilisateur (login = adresse) sur GreenMail et Radicale. Retourne l'adresse."""
    email = email or new_email(prefix)
    api = os.environ.get("TEST_GREENMAIL_API")
    if api:
        import time

        import httpx
        end = time.time() + 90  # démarrage à froid de GreenMail
        while True:
            try:
                r = httpx.post(api.rstrip("/") + "/api/user",
                               json={"email": email, "login": email, "password": password}, timeout=30)
                break
            except httpx.TransportError:
                if time.time() > end:
                    raise
                time.sleep(0.5)
        assert r.status_code == 200, f"GreenMail : création de {email} impossible ({r.status_code} {r.text})"
    ht = os.environ.get("TEST_DAV_HTPASSWD")
    if ht:
        with _lock:
            os.makedirs(os.path.dirname(ht), exist_ok=True)
            with open(ht, "a", encoding="utf-8") as f:
                f.write(f"{email}:{password}\n")
            os.chmod(ht, 0o644)
    return email
