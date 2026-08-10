"""LocalFileTokenStore must survive JSON round-trips (strict datetime models)."""

from __future__ import annotations

from pathlib import Path

import pytest

from level_core.auth.tokens import LocalFileTokenStore
from level_core.schemas.user import OAuthToken, User


@pytest.mark.asyncio
async def test_local_store_roundtrip_keeps_google_user(tmp_path: Path) -> None:
    path = tmp_path / "oauth_store.json"
    store = LocalFileTokenStore(path)
    user = User(
        user_id="abc123",
        email="parent@example.com",
        display_name="Parent Example",
        google_sub="sub-9",
    )
    token = OAuthToken(
        user_id="abc123",
        access_token="access",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="secret",
        scopes=["openid"],
    )
    await store.upsert_user(user)
    await store.upsert_token(token)

    reloaded = LocalFileTokenStore(path)
    got = await reloaded.get_user("abc123")
    assert got is not None
    assert got.email == "parent@example.com"
    assert got.google_sub == "sub-9"
    tok = await reloaded.get_google_token("abc123")
    assert tok is not None
    assert tok.refresh_token == "refresh"


@pytest.mark.asyncio
async def test_local_store_merge_does_not_wipe_disk_tokens(tmp_path: Path) -> None:
    path = tmp_path / "oauth_store.json"
    a = LocalFileTokenStore(path)
    await a.upsert_user(User(user_id="u1", email="a@example.com", google_sub="s1"))
    await a.upsert_token(
        OAuthToken(
            user_id="u1",
            access_token="a",
            refresh_token="keep-me",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="cid",
            client_secret="secret",
            scopes=[],
        )
    )

    # Simulate a reload worker that only knows about a guest, then saves.
    b = LocalFileTokenStore(path)
    await b.upsert_user(User(user_id="guest1", display_name=None))
    got = await b.get_google_token("u1")
    assert got is not None
    assert got.refresh_token == "keep-me"
    assert await b.get_user("u1") is not None
