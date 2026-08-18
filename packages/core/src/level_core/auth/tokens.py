"""OAuth token persistence via UserStore.tokens KV slot."""

from __future__ import annotations

from typing import Any

from level_core.storage.base import UserStore


async def save_tokens(store: UserStore, *, payload: dict[str, Any]) -> None:
    await store.tokens.write(payload)


async def load_tokens(store: UserStore) -> dict[str, Any] | None:
    return await store.tokens.read()


async def clear_tokens(store: UserStore) -> None:
    await store.tokens.write({})
