"""Google Calendar webhook token verification."""

from __future__ import annotations

from level_core.storage.base import UserStore


async def verify_channel(
    store: UserStore, *, channel_id: str | None, channel_token: str | None
) -> bool:
    state = await store.calendar_sync.read() or {}
    watch = state.get("watch_channel") or {}
    if not watch.get("id") or not watch.get("token"):
        return False
    return watch["id"] == channel_id and watch["token"] == channel_token
