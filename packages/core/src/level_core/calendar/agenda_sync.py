"""Agenda-only Google Calendar refresh (no LLM / Memory Bank)."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from level_core.auth.tokens import TokenStore
from level_core.calendar.sync_state import (
    CachedCalendarEvent,
    CalendarSyncState,
    CalendarSyncStore,
    events_for_local_day,
    watch_is_live,
)
from level_core.config import Settings, get_settings
from level_core.ingest.google_live import (
    SyncTokenExpiredError,
    fetch_day_events,
    pull_calendar_incremental,
    stop_calendar_channel,
    watch_primary_calendar,
)
from level_core.observability.logger import get_logger
from level_core.schemas.base import _now_utc
from level_core.schemas.user import OAuthToken

_logger = get_logger(__name__)


def _raw_to_cached(event: dict[str, Any]) -> CachedCalendarEvent | None:
    event_id = str(event.get("id") or "")
    if not event_id:
        return None
    start = event.get("start") or {}
    end = event.get("end") or {}
    start_raw = start.get("dateTime") or start.get("date")
    end_raw = end.get("dateTime") or end.get("date")
    return CachedCalendarEvent(
        id=event_id,
        summary=(event.get("summary") or "(no title)").strip() or "(no title)",
        start=start_raw,
        end=end_raw,
        all_day=bool(start.get("date") and not start.get("dateTime")),
        status=str(event.get("status") or "confirmed"),
        recurring_event_id=event.get("recurringEventId"),
    )


def _merge_incremental(
    state: CalendarSyncState,
    items: list[dict[str, Any]],
    *,
    full_resync: bool,
) -> CalendarSyncState:
    events = {} if full_resync else dict(state.events)
    for raw in items:
        cached = _raw_to_cached(raw)
        if cached is None:
            continue
        if (cached.status or "").lower() == "cancelled":
            events.pop(cached.id, None)
            continue
        events[cached.id] = cached
    return state.model_copy(
        update={
            "events": events,
            "agenda_updated_at": _now_utc(),
        }
    )


async def refresh_agenda_cache(
    *,
    user_id: str,
    token: OAuthToken,
    sync_store: CalendarSyncStore,
) -> CalendarSyncState:
    """Pull calendar deltas into the agenda cache. Never runs Gemini."""
    state = await sync_store.get(user_id) or CalendarSyncState(user_id=user_id)
    try:
        pull = await pull_calendar_incremental(token, sync_token=state.sync_token)
    except SyncTokenExpiredError:
        _logger.info("calendar_sync_token_expired", user_id=user_id)
        pull = await pull_calendar_incremental(token, sync_token=None)
    state = _merge_incremental(state, pull.items, full_resync=pull.full_resync)
    if pull.next_sync_token:
        state = state.model_copy(update={"sync_token": pull.next_sync_token})
    await sync_store.upsert(state)
    _logger.info(
        "agenda_cache_refreshed",
        user_id=user_id,
        events=len(state.events),
        full_resync=pull.full_resync,
        llm=False,
    )
    return state


async def refresh_agenda_on_read(
    *,
    user_id: str,
    token: OAuthToken,
    sync_store: CalendarSyncStore,
) -> CalendarSyncState:
    """Local substitute for Calendar push: pull deltas when no watch is live.

    Prod with ``LEVEL_PUBLIC_API_URL`` keeps a watch; the webhook refreshes
    the cache and this returns the existing state so Today stays cheap.
    """
    state = await sync_store.get(user_id) or CalendarSyncState(user_id=user_id)
    if watch_is_live(state):
        return state
    return await refresh_agenda_cache(
        user_id=user_id, token=token, sync_store=sync_store
    )


async def inject_event_into_agenda_cache(
    *,
    user_id: str,
    sync_store: CalendarSyncStore,
    google_event: dict[str, Any],
) -> None:
    """Patch a just-created Google event into the agenda cache so Today updates immediately."""
    cached = _raw_to_cached(google_event)
    if cached is None:
        return
    state = await sync_store.get(user_id) or CalendarSyncState(user_id=user_id)
    events = dict(state.events)
    events[cached.id] = cached
    state = state.model_copy(
        update={
            "events": events,
            "agenda_updated_at": _now_utc(),
        }
    )
    await sync_store.upsert(state)
    _logger.info(
        "agenda_event_injected",
        user_id=user_id,
        event_id=cached.id,
        summary=cached.summary,
    )


async def mark_events_cancelled_in_cache(
    *,
    user_id: str,
    sync_store: CalendarSyncStore,
    event_ids: list[str],
) -> None:
    """Drop cancelled instances from the agenda cache so Today updates immediately."""
    if not event_ids:
        return
    state = await sync_store.get(user_id)
    if state is None:
        return
    wanted = {eid for eid in event_ids if eid}
    if not wanted:
        return
    events = {eid: ev for eid, ev in state.events.items() if eid not in wanted}
    state = state.model_copy(
        update={"events": events, "agenda_updated_at": _now_utc()}
    )
    await sync_store.upsert(state)

async def ensure_calendar_watch(
    *,
    user_id: str,
    token: OAuthToken,
    sync_store: CalendarSyncStore,
    settings: Settings | None = None,
) -> CalendarSyncState:
    """Register / renew push channel when a public HTTPS API URL is configured."""
    settings = settings or get_settings()
    state = await sync_store.get(user_id) or CalendarSyncState(user_id=user_id)
    public = (settings.public_api_url or "").rstrip("/")
    if not public.startswith("https://"):
        _logger.info(
            "calendar_watch_skipped",
            user_id=user_id,
            reason="LEVEL_PUBLIC_API_URL must be https for Google push",
        )
        return state

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    # Renew if missing or expiring within 24h.
    if (
        state.channel_id
        and state.resource_id
        and state.channel_expiration_ms
        and state.channel_expiration_ms > now_ms + 24 * 3600 * 1000
    ):
        return state

    if state.channel_id and state.resource_id:
        await stop_calendar_channel(
            token, channel_id=state.channel_id, resource_id=state.resource_id
        )

    channel_id = uuid.uuid4().hex
    channel_token = f"uid={user_id}&n={secrets.token_urlsafe(8)}"
    address = f"{public}/v1/sources/google/webhook"
    try:
        resp = await watch_primary_calendar(
            token,
            channel_id=channel_id,
            address=address,
            channel_token=channel_token,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("calendar_watch_failed", user_id=user_id, error=str(exc))
        return state

    state = state.model_copy(
        update={
            "channel_id": channel_id,
            "resource_id": resp.get("resourceId"),
            "channel_token": channel_token,
            "channel_expiration_ms": int(resp["expiration"])
            if resp.get("expiration") is not None
            else None,
        }
    )
    await sync_store.upsert(state)
    _logger.info(
        "calendar_watch_registered",
        user_id=user_id,
        channel_id=channel_id,
        expiration_ms=state.channel_expiration_ms,
    )
    return state


async def day_events_cached_or_live(
    *,
    user_id: str,
    token: OAuthToken,
    sync_store: CalendarSyncStore,
    day_offset: int = 0,
    timezone_name: str = "America/Los_Angeles",
    max_cache_age_seconds: int = 6 * 3600,
    warm_cache: bool = False,
) -> list[dict[str, Any]]:
    """Prefer agenda cache; fall back to a live day pull (still no LLM).

    Does **not** block on a full agenda resync — that made /today feel slow.
    Pass ``warm_cache=True`` only from background tasks.
    """
    state = await sync_store.get(user_id)
    fresh = False
    if state and state.agenda_updated_at:
        age = (_now_utc() - state.agenda_updated_at).total_seconds()
        fresh = age <= max_cache_age_seconds

    if fresh and state is not None:
        return events_for_local_day(
            state, day_offset=day_offset, timezone_name=timezone_name
        )

    live = await fetch_day_events(
        token, day_offset=day_offset, timezone_name=timezone_name
    )
    if warm_cache:
        try:
            await refresh_agenda_cache(user_id=user_id, token=token, sync_store=sync_store)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("agenda_cache_warm_failed", user_id=user_id, error=str(exc))
    return live


__all__ = [
    "day_events_cached_or_live",
    "ensure_calendar_watch",
    "inject_event_into_agenda_cache",
    "mark_events_cancelled_in_cache",
    "refresh_agenda_cache",
    "refresh_agenda_on_read",
]
