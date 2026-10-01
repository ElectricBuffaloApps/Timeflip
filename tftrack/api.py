"""Minimal client for the TimeFlip cloud API (https://newapi.timeflip.io)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE_URL = "https://newapi.timeflip.io"


class AuthError(Exception):
    pass


class TimeFlipClient:
    def __init__(self, email: str, password: str, token: str | None = None, base_url: str = BASE_URL):
        self.email = email
        self.password = password
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            # The sign-in response may carry the token in a header rather than the body.
            header_token = resp.headers.get("Authorization") or resp.headers.get("token")
        parsed = json.loads(raw) if raw else None
        return parsed, header_token

    def login(self) -> str:
        try:
            body, header_token = self._request(
                "POST", "/api/auth/email/sign-in",
                {"email": self.email, "password": self.password}, auth=False,
            )
        except urllib.error.HTTPError as e:
            raise AuthError(f"Sign-in failed ({e.code}). Check your email and password.") from e
        token = (body or {}).get("token") if isinstance(body, dict) else None
        token = token or header_token
        if not token:
            raise AuthError(f"Sign-in succeeded but no token was found in the response: {body!r}")
        self.token = token.removeprefix("Bearer ").strip()
        return self.token

    def get(self, path: str):
        if not self.token:
            self.login()
        try:
            return self._request("GET", path)[0]
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
        # Token expired: sign in again once and retry.
        self.login()
        return self._request("GET", path)[0]

    def sync_all(self) -> dict:
        """All tasks and time intervals for the account."""
        return self.get("/api/sync/all")
