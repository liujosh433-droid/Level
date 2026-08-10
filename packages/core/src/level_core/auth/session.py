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
# One-time handoff after Google OAuth (callback is on :8080, cookie must land on :3000).
_HANDOFF_MAX_AGE_SECONDS = 120


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _secret(settings: Settings) -> bytes:
    # Local default is a fixed string so API reloads never invalidate cookies
    # when GOOGLE_API_KEY / OAuth secrets change in .env during development.
    raw = settings.session_secret
    if not raw:
        if settings.is_local:
            raw = "level-local-session-v1"
        else:
            raw = (
                settings.google_oauth_client_secret
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


def mint_handoff_token(user_id: str, settings: Settings | None = None) -> str:
    """Short-lived token so the web origin can mint the real session cookie."""
    settings = settings or get_settings()
    body = {"uid": user_id, "t": int(time.time()), "kind": "handoff"}
    raw = _b64url(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret(settings), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def parse_handoff_token(token: str, settings: Settings | None = None) -> str | None:
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
        if body.get("kind") != "handoff":
            return None
        uid = str(body["uid"])
        ts = int(body["t"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    age = int(time.time()) - ts
    if age < 0 or age > _HANDOFF_MAX_AGE_SECONDS or not uid:
        return None
    return uid


def safe_next_path(next_path: str | None, default: str = "/today") -> str:
    """Only allow same-origin relative paths (block open redirects)."""
    if not next_path:
        return default
    path = next_path.strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    if any(c in path for c in ("\n", "\r", "\x00")):
        return default
    return path


__all__ = [
    "COOKIE_NAME",
    "mint_handoff_token",
    "mint_session_token",
    "parse_handoff_token",
    "parse_session_token",
    "safe_next_path",
    "session_cookie_kwargs",
]
