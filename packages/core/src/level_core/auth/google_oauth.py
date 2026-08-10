"""Google OAuth helpers for Calendar access."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from level_core.config import Settings, get_settings
from level_core.schemas.user import OAuthToken


@dataclass(frozen=True, slots=True)
class OAuthStatePayload:
    code_verifier: str
    link_user_id: str | None = None

# Signed OAuth ``state`` survives API reloads (uvicorn --reload clears memory).
# Also carries the PKCE code_verifier so token exchange works on a new Flow.
_STATE_MAX_AGE_SECONDS = 15 * 60

# Calendar events = read + write (commitment gate can create after confirm).
GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.events",
)


def _client_config(settings: Settings) -> dict[str, Any]:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET not set. "
            "Create an OAuth Web client in GCP Console → APIs & Services → Credentials."
        )
    return {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_oauth_redirect_uri],
        }
    }


def build_flow(settings: Settings | None = None, *, state: str | None = None) -> Flow:
    settings = settings or get_settings()
    flow = Flow.from_client_config(
        _client_config(settings),
        scopes=list(GOOGLE_SCOPES),
        state=state,
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    return flow


def _state_secret(settings: Settings) -> bytes:
    raw = settings.google_oauth_client_secret or settings.google_api_key or "level-dev"
    return raw.encode("utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def mint_oauth_state(
    settings: Settings | None = None,
    *,
    code_verifier: str,
    link_user_id: str | None = None,
) -> str:
    """HMAC-signed state carrying PKCE verifier (+ optional guest user to link)."""
    settings = settings or get_settings()
    body: dict[str, Any] = {
        "t": int(time.time()),
        "n": secrets.token_urlsafe(12),
        "v": code_verifier,
    }
    if link_user_id:
        body["u"] = link_user_id
    raw = _b64url(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_state_secret(settings), raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def parse_oauth_state(
    state: str, settings: Settings | None = None
) -> OAuthStatePayload | None:
    """Validate state → PKCE verifier + optional link user."""
    settings = settings or get_settings()
    if "." not in state:
        return None
    raw, sig = state.rsplit(".", 1)
    if not raw or not sig:
        return None
    expected = hmac.new(
        _state_secret(settings), raw.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        body = json.loads(_b64url_decode(raw).decode("utf-8"))
        ts = int(body["t"])
        verifier = str(body["v"])
        link_user_id = str(body["u"]) if body.get("u") else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    age = int(time.time()) - ts
    if age < 0 or age > _STATE_MAX_AGE_SECONDS or not verifier:
        return None
    return OAuthStatePayload(
        code_verifier=verifier,
        link_user_id=link_user_id,
    )


def verify_oauth_state(state: str, settings: Settings | None = None) -> bool:
    return parse_oauth_state(state, settings) is not None


def authorization_url(
    settings: Settings | None = None,
    *,
    link_user_id: str | None = None,
) -> tuple[str, str]:
    """Return (auth_url, state)."""
    settings = settings or get_settings()
    verifier, challenge = _make_pkce()
    state = mint_oauth_state(
        settings,
        code_verifier=verifier,
        link_user_id=link_user_id,
    )
    flow = build_flow(settings, state=state)
    # Keep verifier on the Flow in case the library reads it later.
    flow.code_verifier = verifier  # type: ignore[attr-defined]
    url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # ensure refresh_token on re-connect
        state=state,
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    return url, returned_state or state


def exchange_code(code: str, state: str, settings: Settings | None = None) -> Credentials:
    settings = settings or get_settings()
    parsed = parse_oauth_state(state, settings)
    if not parsed:
        raise ValueError("invalid OAuth state / missing PKCE verifier")
    flow = build_flow(settings)
    flow.code_verifier = parsed.code_verifier  # type: ignore[attr-defined]
    # With include_granted_scopes, Google may also return older grants
    # (e.g. calendar.readonly from a prior connect). oauthlib rejects that
    # mismatch unless we relax scope checking.
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow.fetch_token(code=code, code_verifier=parsed.code_verifier)
    return flow.credentials


def credentials_from_token(token: OAuthToken, settings: Settings | None = None) -> Credentials:
    settings = settings or get_settings()
    creds = Credentials(
        token=token.access_token,
        refresh_token=token.refresh_token,
        token_uri=token.token_uri,
        client_id=token.client_id or settings.google_oauth_client_id,
        client_secret=token.client_secret or settings.google_oauth_client_secret,
        scopes=token.scopes or list(GOOGLE_SCOPES),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def token_from_credentials(
    creds: Credentials, *, user_id: str, settings: Settings | None = None
) -> OAuthToken:
    settings = settings or get_settings()
    expiry = None
    if creds.expiry:
        expiry = creds.expiry if creds.expiry.tzinfo else creds.expiry.replace(tzinfo=timezone.utc)
    return OAuthToken(
        user_id=user_id,
        access_token=creds.token or "",
        refresh_token=creds.refresh_token,
        token_uri=creds.token_uri or "https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=list(creds.scopes or GOOGLE_SCOPES),
        expiry=expiry,
    )


def fetch_userinfo(creds: Credentials) -> dict[str, Any]:
    import httpx

    resp = httpx.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


__all__ = [
    "GOOGLE_SCOPES",
    "OAuthStatePayload",
    "authorization_url",
    "credentials_from_token",
    "exchange_code",
    "fetch_userinfo",
    "mint_oauth_state",
    "parse_oauth_state",
    "token_from_credentials",
    "verify_oauth_state",
]
