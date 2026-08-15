"""Calendar push watch vs local pull substitute."""

from __future__ import annotations

from datetime import datetime, timezone

from level_core.calendar.sync_state import CalendarSyncState, watch_is_live


def test_watch_is_live_false_without_channel() -> None:
    state = CalendarSyncState(user_id="u1")
    assert watch_is_live(state) is False
    assert watch_is_live(None) is False


def test_watch_is_live_true_before_expiry() -> None:
    now = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    state = CalendarSyncState(
        user_id="u1",
        channel_id="ch",
        resource_id="res",
        channel_expiration_ms=now_ms + 60_000,
    )
    assert watch_is_live(state, now=now) is True


def test_watch_is_live_false_after_expiry() -> None:
    now = datetime(2026, 8, 15, 21, 0, tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    state = CalendarSyncState(
        user_id="u1",
        channel_id="ch",
        resource_id="res",
        channel_expiration_ms=now_ms - 1,
    )
    assert watch_is_live(state, now=now) is False
