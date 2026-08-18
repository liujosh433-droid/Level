"""Google OAuth 2.0 for end-user Calendar + Gmail access."""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from typing import Any

from level_core.config import get_settings

GOOGLE_SCOPES: list[str] = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.send",
]


@dataclass
class OAuthStart:
    url: str
    state: str
    code_verifier: str | None = None


@dataclass
class OAuthExchange:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expiry_epoch: int
    email: str | None


def build_auth_url() -> OAuthStart:
    """Build the OAuth consent URL. Caller sets `state` in a signed cookie."""
    from google_auth_oauthlib.flow import Flow

    settings = get_settings()
    state = secrets.token_urlsafe(24)
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_oauth_redirect_uri],
            }
        },
        scopes=GOOGLE_SCOPES,
        state=state,
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return OAuthStart(url=url, state=state, code_verifier=getattr(flow, "code_verifier", None))


def exchange_code(*, code: str, code_verifier: str | None = None) -> OAuthExchange:
    from google_auth_oauthlib.flow import Flow

    settings = get_settings()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_oauth_redirect_uri],
            }
        },
        scopes=GOOGLE_SCOPES,
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials

    email = _email_from_id_token(getattr(creds, "id_token", None))
    expiry_epoch = int(creds.expiry.timestamp()) if getattr(creds, "expiry", None) else 0
    return OAuthExchange(
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        id_token=getattr(creds, "id_token", None),
        expiry_epoch=expiry_epoch,
        email=email,
    )


def _email_from_id_token(id_token: str | None) -> str | None:
    if not id_token:
        return None
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        return payload.get("email")
    except Exception:
        return None
