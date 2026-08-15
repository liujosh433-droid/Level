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
    need: str | None = None

# Signed OAuth ``state`` survives API reloads (uvicorn --reload clears memory).
# Also carries the PKCE code_verifier so token exchange works on a new Flow.
_STATE_MAX_AGE_SECONDS = 15 * 60

# Calendar events = read + write (commitment gate can create after confirm).
# gmail.send is incremental — school paper / sick-day notes only, no inbox read.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.events",
    GMAIL_SEND_SCOPE,
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
    need: str | None = None,
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
    if need:
        body["need"] = need[:20]
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
        need = str(body["need"]) if body.get("need") else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    age = int(time.time()) - ts
    if age < 0 or age > _STATE_MAX_AGE_SECONDS or not verifier:
        return None
    return OAuthStatePayload(
        code_verifier=verifier,
        link_user_id=link_user_id,
        need=need,
    )


def verify_oauth_state(state: str, settings: Settings | None = None) -> bool:
    return parse_oauth_state(state, settings) is not None


def authorization_url(
    settings: Settings | None = None,
    *,
    link_user_id: str | None = None,
    need: str | None = None,
) -> tuple[str, str]:
    """Return (auth_url, state).

    Always request the full ``GOOGLE_SCOPES`` list (Calendar + gmail.send).
    Asking for ``gmail.send`` alone let Google strip it and return the old
    Calendar grant. ``need`` is UX-only (where we send the user after).
    """
    settings = settings or get_settings()
    verifier, challenge = _make_pkce()
    state = mint_oauth_state(
        settings,
        code_verifier=verifier,
        link_user_id=link_user_id,
        need=need,
    )
    flow = Flow.from_client_config(
        _client_config(settings),
        scopes=list(GOOGLE_SCOPES),
        state=state,
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    flow.code_verifier = verifier  # type: ignore[attr-defined]
    url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
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
    flow = Flow.from_client_config(
        _client_config(settings),
        scopes=list(GOOGLE_SCOPES),
        state=state,
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    flow.code_verifier = parsed.code_verifier  # type: ignore[attr-defined]
    # With include_granted_scopes, Google may also return older grants
    # (e.g. calendar.readonly from a prior connect). oauthlib rejects that
    # mismatch unless we relax scope checking.
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow.fetch_token(code=code, code_verifier=parsed.code_verifier)
    creds = flow.credentials
    raw_scope = None
    session = getattr(flow, "oauth2session", None)
    if session is not None:
        token_blob = getattr(session, "token", None) or {}
        if isinstance(token_blob, dict):
            raw_scope = token_blob.get("scope")
    granted = _scope_list(creds.scopes) or _scope_list(raw_scope)
    if granted:
        creds._scopes = granted  # noqa: SLF001
    return creds


def credentials_from_token(token: OAuthToken, settings: Settings | None = None) -> Credentials:
    settings = settings or get_settings()
    creds = Credentials(
        token=token.access_token,
        refresh_token=token.refresh_token,
        token_uri=token.token_uri,
        client_id=token.client_id or settings.google_oauth_client_id,
        client_secret=token.client_secret or settings.google_oauth_client_secret,
        scopes=_scope_list(token.scopes) or None,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def _scope_list(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [part for part in raw.replace(",", " ").split() if part]
    return [str(part) for part in raw if part]


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
        scopes=_scope_list(creds.scopes),
        expiry=expiry,
    )


def token_has_gmail_send(scopes: list[str] | None) -> bool:
    """True only when the stored grant actually includes gmail.send.

    Empty/unknown scopes mean a legacy Calendar-only connect — not email send.
    """
    if not scopes:
        return False
    blob = " ".join(_scope_list(scopes))
    return GMAIL_SEND_SCOPE in blob or "https://mail.google.com/" in blob


def fetch_token_scopes(creds: Credentials) -> list[str]:
    """Ask Google what this access token can actually do (not the local store)."""
    import httpx

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    access = creds.token
    if not access:
        return _scope_list(creds.scopes)
    try:
        resp = httpx.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"access_token": access},
            timeout=15.0,
        )
    except httpx.HTTPError:
        return _scope_list(creds.scopes)
    if resp.status_code >= 400:
        return _scope_list(creds.scopes)
    live = _scope_list((resp.json() or {}).get("scope"))
    return live or _scope_list(creds.scopes)


def refresh_token_grant(
    token: OAuthToken, settings: Settings | None = None
) -> OAuthToken:
    """Refresh the access token and persist Google's live scope list."""
    creds = credentials_from_token(token, settings)
    live = fetch_token_scopes(creds)
    updates: dict[str, Any] = {}
    if live:
        updates["scopes"] = live
    if creds.token:
        updates["access_token"] = creds.token
    if creds.refresh_token and not token.refresh_token:
        updates["refresh_token"] = creds.refresh_token
    if creds.expiry:
        expiry = creds.expiry if creds.expiry.tzinfo else creds.expiry.replace(tzinfo=timezone.utc)
        updates["expiry"] = expiry
    return token.model_copy(update=updates) if updates else token


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
    "GMAIL_SEND_SCOPE",
    "OAuthStatePayload",
    "authorization_url",
    "credentials_from_token",
    "exchange_code",
    "fetch_token_scopes",
    "fetch_userinfo",
    "mint_oauth_state",
    "parse_oauth_state",
    "refresh_token_grant",
    "token_from_credentials",
    "token_has_gmail_send",
    "verify_oauth_state",
]
