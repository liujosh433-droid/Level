"""Google Calendar webhook token verification."""

from __future__ import annotations

import hmac

from level_core.storage.base import UserStore


def _constant_time_equal(a: str | None, b: str | None) -> bool:
    """Constant-time equality that tolerates missing values.

    ``hmac.compare_digest`` raises on mismatched types, so we normalize
    both operands to str first. Returns False when either side is None
    or empty (there's no valid channel to compare against).
    """
    if not a or not b:
        return False
    return hmac.compare_digest(str(a), str(b))


async def verify_channel(
    store: UserStore, *, channel_id: str | None, channel_token: str | None
) -> bool:
    state = await store.calendar_sync.read() or {}
    watch = state.get("watch_channel") or {}
    if not watch.get("id") or not watch.get("token"):
        return False
    # Constant-time compare on the shared secret (``token``); the
    # channel_id is not secret but we still compare it in constant time
    # so the timing profile matches on rotated ids.
    return _constant_time_equal(watch.get("id"), channel_id) and _constant_time_equal(
        watch.get("token"), channel_token
    )
