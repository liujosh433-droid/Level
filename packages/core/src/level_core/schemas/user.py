"""End-user identity + OAuth token records."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from level_core.schemas.base import TimestampedModel, _new_id


class User(TimestampedModel):
    """A Level end-user (caregiver)."""

    user_id: str = Field(default_factory=_new_id)
    email: str | None = None
    display_name: str | None = None
    google_sub: str | None = Field(
        default=None, description="Google OpenID subject — stable across logins."
    )


class OAuthToken(TimestampedModel):
    """Stored Google OAuth tokens for a user (Calendar / Drive scopes)."""

    token_id: str = Field(default_factory=_new_id)
    user_id: str
    provider: str = "google"
    access_token: str
    refresh_token: str | None = None
    token_uri: str = "https://oauth2.googleapis.com/token"
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = Field(default_factory=list)
    expiry: datetime | None = None


__all__ = ["OAuthToken", "User"]
