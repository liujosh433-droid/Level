"""Signed httpOnly session cookies for Level caregivers.

The browser never needs to put ``user_id`` in URLs or localStorage for auth —
the API mints a HMAC-signed cookie and every protected route reads it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from level_core.config import Settings, get_settings

COOKIE_NAME = "level_session"
# 30 days — caregivers shouldn't re-login every refresh.
_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _secret(settings: Settings) -> bytes:
    raw = (
        settings.session_secret
        or settings.google_oauth_client_secret
        or settings.google_api_key
        or "level-dev-session"
    )
    return raw.encode("utf-8")


def mint_session_token(user_id: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    body = {"uid": user_id, "t": int(time.time())}
    raw = _b64url(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret(settings), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def parse_session_token(token: str, settings: Settings | None = None) -> str | None:
    """Return user_id if the cookie is valid and unexpired."""
    settings = settings or get_settings()
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    if not raw or not sig:
        return None
    expected = hmac.new(_secret(settings), raw.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        body = json.loads(_b64url_decode(raw).decode("utf-8"))
        uid = str(body["uid"])
        ts = int(body["t"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    age = int(time.time()) - ts
    if age < 0 or age > _SESSION_MAX_AGE_SECONDS or not uid:
        return None
    return uid


def session_cookie_kwargs(settings: Settings | None = None) -> dict[str, Any]:
    """kwargs for Starlette ``Response.set_cookie`` / ``delete_cookie``."""
    settings = settings or get_settings()
    secure = not settings.is_local
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": _SESSION_MAX_AGE_SECONDS,
    }


__all__ = [
    "COOKIE_NAME",
    "mint_session_token",
    "parse_session_token",
    "session_cookie_kwargs",
]
