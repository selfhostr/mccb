"""Test users for the integration services (docker-compose.test.yml).

GreenMail runs WITH authentication: each user is created via its REST API
(`TEST_GREENMAIL_API`, POST /api/user). Radicale uses an htpasswd file (plaintext passwords)
shared via a volume (`TEST_DAV_HTPASSWD`); Radicale re-reads the file on each authentication.
Without these variables (services started by hand, auth disabled), the functions do nothing.
"""
from __future__ import annotations

import os
import threading
import uuid

_lock = threading.Lock()


def new_email(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}@example.test"


def create_user(email: str | None = None, password: str = "pw", prefix: str = "u") -> str:
    """Creates the user (login = address) on GreenMail and Radicale. Returns the address."""
    email = email or new_email(prefix)
    api = os.environ.get("TEST_GREENMAIL_API")
    if api:
        import time

        import httpx
        end = time.time() + 90  # GreenMail cold start
        while True:
            try:
                r = httpx.post(api.rstrip("/") + "/api/user",
                               json={"email": email, "login": email, "password": password}, timeout=30)
                break
            except httpx.TransportError:
                if time.time() > end:
                    raise
                time.sleep(0.5)
        assert r.status_code == 200, f"GreenMail: cannot create {email} ({r.status_code} {r.text})"
    ht = os.environ.get("TEST_DAV_HTPASSWD")
    if ht:
        with _lock:
            os.makedirs(os.path.dirname(ht), exist_ok=True)
            with open(ht, "a", encoding="utf-8") as f:
                f.write(f"{email}:{password}\n")
            os.chmod(ht, 0o644)
    return email
