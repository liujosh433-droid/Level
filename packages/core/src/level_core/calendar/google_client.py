"""Google API client construction from stored OAuth tokens."""

from __future__ import annotations

from typing import Any

from level_core.config import get_settings
from level_core.storage.base import UserStore


async def _credentials(store: UserStore) -> Any:
    from google.oauth2.credentials import Credentials

    settings = get_settings()
    tokens = await store.tokens.read() or {}
    if not tokens.get("access_token"):
        raise RuntimeError("no_google_tokens")
    return Credentials(
        token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=tokens.get("scopes"),
    )


async def build_calendar_client(store: UserStore) -> Any:
    from googleapiclient.discovery import build

    creds = await _credentials(store)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def build_gmail_client(store: UserStore) -> Any:
    from googleapiclient.discovery import build

    creds = await _credentials(store)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
