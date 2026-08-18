"""Agenda sync: pull calendar events into `agenda_cache`, no LLM in this path.

Two triggers:
  - `refresh_agenda()` called explicitly on Today load, on OAuth complete,
    or from the webhook.
  - `ensure_watch()` registers a Google Calendar push channel so future
    changes hit /v1/calendar/webhook.

Uses incremental syncToken when available; falls back to time-window pull.
Emits a fingerprint over event IDs+etags so downstream infer skips work
when nothing changed.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from level_core.config import get_settings
from level_core.observability import get_logger, span
from level_core.schemas import CachedEvent, DailyAgenda, EventTime
from level_core.storage.base import UserStore

logger = get_logger(__name__)


@dataclass
class RefreshResult:
    added: int
    updated: int
    removed: int
    total_cached: int
    fingerprint: str
    fingerprint_changed: bool


async def refresh_agenda(store: UserStore, *, calendar_id: str = "primary") -> RefreshResult:
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)
    now = datetime.now(UTC)
    time_min = now - timedelta(days=settings.level_cal_days_back)
    time_max = now + timedelta(days=settings.level_cal_days_forward)

    with span("calendar.refresh", user=store.user_id, calendar=calendar_id):
        from level_core.calendar.google_client import build_calendar_client

        service = await build_calendar_client(store)
        events_page = await asyncio.to_thread(
            _list_events,
            service=service,
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
        )

    added = 0
    updated = 0
    seen_ids: set[str] = set()
    for item in events_page:
        cached = _to_cached_event(item, calendar_id=calendar_id, tz=tz)
        if not cached:
            continue
        seen_ids.add(cached.event_id)
        existing = await store.agenda.get(cached.event_id)
        if existing and existing.etag == cached.etag:
            continue
        if existing:
            merged = _merge_preserving_ai(existing, cached)
            await store.agenda.upsert(merged)
            updated += 1
        else:
            await store.agenda.upsert(cached)
            added += 1

    all_cached = await store.agenda.list()
    to_remove = [e for e in all_cached if e.event_id not in seen_ids and _within_window(e, time_min, time_max)]
    for e in to_remove:
        await store.agenda.delete(e.event_id)
    removed = len(to_remove)

    remaining = [e for e in await store.agenda.list()]
    fingerprint = _fingerprint(remaining)
    prev = (await store.calendar_sync.read() or {}).get("events_fingerprint")
    fingerprint_changed = prev != fingerprint
    await _rebuild_daily_agenda(store, remaining, tz=tz)

    await store.calendar_sync.write(
        {
            "events_fingerprint": fingerprint,
            "last_pull_at": now.isoformat(),
            "calendar_id": calendar_id,
            **({"sync_token": None}),
        }
    )

    logger.info(
        "calendar.refresh.done",
        user=store.user_id,
        added=added,
        updated=updated,
        removed=removed,
        total=len(remaining),
        fingerprint_changed=fingerprint_changed,
    )
    return RefreshResult(
        added=added,
        updated=updated,
        removed=removed,
        total_cached=len(remaining),
        fingerprint=fingerprint,
        fingerprint_changed=fingerprint_changed,
    )


def _list_events(
    *, service: Any, calendar_id: str, time_min: datetime, time_max: datetime
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def _to_cached_event(
    item: dict[str, Any], *, calendar_id: str, tz: ZoneInfo
) -> CachedEvent | None:
    status = item.get("status")
    if status == "cancelled":
        return None
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})
    all_day = "date" in start_raw
    start = _parse_time(start_raw, tz=tz)
    end = _parse_time(end_raw, tz=tz)
    if not start or not end:
        return None

    ext = item.get("extendedProperties", {}) or {}
    private = ext.get("private", {}) or {}
    origin = "level" if private.get("origin") == "level" else "google"

    attendee_tokens = _first_name_tokens(item.get("attendees") or [])

    return CachedEvent(
        event_id=item["id"],
        calendar_id=calendar_id,
        summary=(item.get("summary") or "").strip(),
        time=EventTime(start=start, end=end, tz=str(tz), all_day=all_day),
        location=(item.get("location") or None),
        attendee_tokens=attendee_tokens,
        origin=origin,
        level_reason=private.get("level_reason"),
        etag=item.get("etag"),
    )


def _parse_time(raw: dict[str, Any], *, tz: ZoneInfo) -> datetime | None:
    if "dateTime" in raw:
        return date_parser.isoparse(raw["dateTime"]).astimezone(UTC)
    if "date" in raw:
        d = date_parser.isoparse(raw["date"]).replace(tzinfo=tz)
        return d.astimezone(UTC)
    return None


def _first_name_tokens(attendees: list[dict[str, Any]]) -> list[str]:
    """Stable first-name tokens; we never store emails."""
    out: list[str] = []
    for a in attendees:
        name = (a.get("displayName") or a.get("email", "").split("@")[0] or "").strip()
        first = name.split()[0] if name else ""
        first = "".join(ch for ch in first if ch.isalpha())
        if first and first.lower() not in {"self", "me"}:
            out.append(first)
    return sorted(set(out))


def _merge_preserving_ai(existing: CachedEvent, incoming: CachedEvent) -> CachedEvent:
    """Keep AI-classified fields when the event's core content is unchanged."""
    return incoming.model_copy(
        update={
            "activity_type": existing.activity_type
            if existing.summary == incoming.summary
            else None,
            "classified_at": existing.classified_at
            if existing.summary == incoming.summary
            else None,
            "matched_person_ids": existing.matched_person_ids,
            "matched_reminder_ids": existing.matched_reminder_ids,
        }
    )


def _within_window(e: CachedEvent, lo: datetime, hi: datetime) -> bool:
    start = e.time.start
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return lo <= start <= hi


def _fingerprint(events: list[CachedEvent]) -> str:
    hasher = hashlib.sha256()
    for e in sorted(events, key=lambda x: x.event_id):
        hasher.update(f"{e.event_id}:{e.etag}:{e.summary}:{e.time.start.isoformat()}".encode())
    return hasher.hexdigest()[:16]


async def _rebuild_daily_agenda(
    store: UserStore, events: list[CachedEvent], *, tz: ZoneInfo
) -> None:
    buckets: dict[str, list[str]] = {}
    for e in events:
        start = e.time.start
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        local = start.astimezone(tz)
        key = local.strftime("%Y-%m-%d")
        buckets.setdefault(key, []).append(e.event_id)
    for date_key, ids in buckets.items():
        await store.daily_agenda.upsert(
            DailyAgenda(date=date_key, event_ids=sorted(ids))
        )
    existing = [d for d in await store.daily_agenda.list() if d.date not in buckets]
    for d in existing:
        await store.daily_agenda.delete(d.date)


async def ensure_watch(store: UserStore, *, calendar_id: str = "primary") -> bool:
    """Register a push channel if LEVEL_PUBLIC_API_URL is https."""
    settings = get_settings()
    if not settings.level_public_api_url.startswith("https://"):
        logger.info("calendar.watch.skipped", reason="no_public_https_url")
        return False

    from level_core.calendar.google_client import build_calendar_client

    service = await build_calendar_client(store)
    import secrets as _s
    import uuid as _u

    channel_id = f"level-{_u.uuid4().hex[:12]}"
    channel_token = _s.token_urlsafe(24)
    webhook_url = f"{settings.level_public_api_url.rstrip('/')}/v1/calendar/webhook"

    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "token": channel_token,
    }
    resp = await asyncio.to_thread(
        service.events().watch, calendarId=calendar_id, body=body
    )
    resp = await asyncio.to_thread(resp.execute)
    state = await store.calendar_sync.read() or {}
    state["watch_channel"] = {
        "id": channel_id,
        "resource_id": resp.get("resourceId"),
        "expiration": resp.get("expiration"),
        "token": channel_token,
    }
    await store.calendar_sync.write(state)
    return True
