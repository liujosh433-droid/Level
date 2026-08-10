"""End-user identity + OAuth token records."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import Field

from level_core.schemas.base import TimestampedModel, _new_id

# Role-ish placeholders we should replace once we know who the person is.
_PLACEHOLDER_DISPLAY_NAMES = frozenset(
    {
        "caregiver",
        "guest",
        "guest parent",
        "guest parent.",
        "parent",
        "user",
    }
)


def format_person_name(name: str | None) -> str | None:
    """Capitalize a person's name for display (``anna mokkapati`` → ``Anna Mokkapati``)."""
    if name is None:
        return None
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return None
    out: list[str] = []
    for part in parts:
        # Preserve short all-caps tokens like "II"; otherwise title-case each word.
        if len(part) <= 2 and part.isupper():
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:].lower())
    return " ".join(out)


def display_name_from_email(email: str | None) -> str | None:
    """Turn ``anna.mokkapati@gmail.com`` into ``Anna Mokkapati``."""
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0].strip()
    if not local:
        return None
    # Drop plus-addressing tags (jane+newsletter@…).
    local = local.split("+", 1)[0]
    local = re.sub(r"[._\-]+", " ", local)
    local = re.sub(r"\d+", " ", local)
    return format_person_name(local)


def is_placeholder_display_name(name: str | None) -> bool:
    if name is None:
        return True
    cleaned = " ".join(name.split()).strip().lower()
    return not cleaned or cleaned in _PLACEHOLDER_DISPLAY_NAMES


def resolve_display_name(
    *,
    google_name: str | None = None,
    email: str | None = None,
    existing: str | None = None,
) -> str | None:
    """Prefer a real Google name, then email local-part, then a non-placeholder existing."""
    if google_name and google_name.strip():
        return format_person_name(google_name)
    from_email = display_name_from_email(email)
    if from_email and is_placeholder_display_name(existing):
        return from_email
    if existing and not is_placeholder_display_name(existing):
        return format_person_name(existing)
    return from_email or format_person_name(existing)


class User(TimestampedModel):
    """A Level end-user (caregiver)."""

    user_id: str = Field(default_factory=_new_id)
    email: str | None = None
    display_name: str | None = None
    google_sub: str | None = Field(
        default=None, description="Google OpenID subject — stable across logins."
    )


class OAuthToken(TimestampedModel):
    """Stored Google OAuth tokens for a user (Calendar scopes)."""

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


__all__ = [
    "OAuthToken",
    "User",
    "display_name_from_email",
    "format_person_name",
    "is_placeholder_display_name",
    "resolve_display_name",
]
