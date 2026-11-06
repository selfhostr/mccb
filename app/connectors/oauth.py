"""OAuth2 access tokens for the connectors (client credentials or refresh token), and the HTTP client they share.
A token source is thread-safe and may be shared by every session of a target. Tokens are never logged."""
from __future__ import annotations

import threading
import time

import httpx

from .base import ConnectorError

TRANSPORT: httpx.BaseTransport | None = None  # tests only: replaces the network with a fake server
EXPIRY_MARGIN = 120.0  # seconds before the announced expiry at which a token counts as expired
_AUTH_ERRORS = {"invalid_client", "unauthorized_client", "invalid_grant", "access_denied"}


def http_client(verify_tls: bool = True, timeout: float = 120.0, **kw) -> httpx.Client:
    """Redirects are never followed: credentials must not travel to a host the configuration does not name."""
    return httpx.Client(verify=verify_tls, timeout=timeout, follow_redirects=False, transport=TRANSPORT, **kw)


class OAuthToken:
    """Grant chosen by the secrets: a refresh token if there is one, the client credentials otherwise.

    A provider that rotates refresh tokens returns a new one with each refresh: it is kept in memory for this
    object, not stored. See docs/exchange.md."""

    def __init__(self, token_url: str, client_id: str, client_secret: str = "", scope: str = "",
                 refresh_token: str = "", verify_tls: bool = True):
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.refresh_token = refresh_token
        self.verify_tls = verify_tls
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def token(self, force: bool = False) -> str:
        """A valid access token. `force`: the server refused the current one, get another."""
        with self._lock:
            if not force and self._token and time.time() < self._expires:
                return self._token
            self._request()
            return self._token

    def invalidate(self, token: str) -> None:
        with self._lock:
            if self._token == token:
                self._token, self._expires = "", 0.0

    def _form(self) -> dict:
        form = {"client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        if self.scope:
            form["scope"] = self.scope
        if self.refresh_token:
            form.update(grant_type="refresh_token", refresh_token=self.refresh_token)
        elif self.client_secret:
            form["grant_type"] = "client_credentials"
        else:
            raise ConnectorError("OAuth2: a client secret or a refresh token is required", ConnectorError.CONFIG)
        return form

    def _request(self) -> None:
        form = self._form()
        try:
            with http_client(self.verify_tls, 60.0) as c:
                r = c.post(self.token_url, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise ConnectorError(f"OAuth2 token endpoint: {type(e).__name__}", ConnectorError.UNREACHABLE) from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if r.status_code == 200 and body.get("access_token"):
            self._token = str(body["access_token"])
            self._expires = time.time() + max(0.0, float(body.get("expires_in") or 3600) - EXPIRY_MARGIN)
            if body.get("refresh_token"):
                self.refresh_token = str(body["refresh_token"])
            return
        code = str(body.get("error") or f"HTTP {r.status_code}")
        # the description names the tenant and the app, never the secret
        msg = f"OAuth2 token refused: {code}: {str(body.get('error_description') or '')[:300]}".rstrip(": ")
        if r.status_code >= 500 or r.status_code == 429:
            raise ConnectorError(msg, ConnectorError.UNREACHABLE)
        if code in _AUTH_ERRORS or r.status_code == 401:
            raise ConnectorError(msg, ConnectorError.AUTH)
        raise ConnectorError(msg, ConnectorError.CONFIG)
