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
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

from level_core.calendar.enrich import heuristic_activity
from level_core.calendar.google_client import build_calendar_client
from level_core.config import get_settings
from level_core.observability import get_logger, span
from level_core.schemas import CachedEvent, DailyAgenda, EventTime
from level_core.storage.base import UserStore
from level_core.tz import tz_for_store

logger = get_logger(__name__)


@dataclass
class RefreshResult:
    added: int
    updated: int
    removed: int
    total_cached: int
    fingerprint: str
    fingerprint_changed: bool
    calendars: list[str]
    last_error: str | None = None


async def refresh_agenda(store: UserStore, *, calendar_id: str | None = None) -> RefreshResult:
    settings = get_settings()
    tz = await tz_for_store(store)
    now = datetime.now(UTC)
    time_min, time_max = await _sync_window(store, settings=settings, now=now)

    with span("calendar.refresh", user=store.user_id, calendar=calendar_id or "all"):
        service = await build_calendar_client(store)
        calendars = await asyncio.to_thread(_resolve_calendars, service, calendar_id)

    added = 0
    updated = 0
    seen_ids: set[str] = set()
    pulled_ids: set[str] = set()
    errors: list[str] = []
    to_upsert: list[CachedEvent] = []

    existing_list = await store.agenda.list()
    by_id = {e.event_id: e for e in existing_list}

    for cal in calendars:
        cal_id = cal["id"]
        try:
            events_page = await asyncio.to_thread(
                _list_events,
                service=service,
                calendar_id=cal_id,
                time_min=time_min,
                time_max=time_max,
            )
        except Exception as exc:  # noqa: BLE001 - one calendar must not wipe the rest
            errors.append(f"{cal.get('summary') or cal_id}: {exc}")
            logger.warning("calendar.refresh.calendar_failed", calendar=cal_id, error=str(exc)[:200])
            continue

        pulled_ids.add(cal_id)
        if cal.get("primary"):
            pulled_ids.add("primary")

        for item in events_page:
            cached = _to_cached_event(item, calendar_id=cal_id, tz=tz)
            if not cached:
                continue
            seen_ids.add(cached.event_id)
            existing = by_id.get(cached.event_id)
            if existing and existing.etag == cached.etag:
                continue
            if existing:
                to_upsert.append(_merge_preserving_ai(existing, cached))
                updated += 1
            else:
                to_upsert.append(cached)
                added += 1

    await store.agenda.upsert_many(to_upsert)

    all_cached = await store.agenda.list()
    to_remove = [
        e
        for e in all_cached
        if e.event_id not in seen_ids
        and _within_window(e, time_min, time_max)
        and (e.calendar_id in pulled_ids or not pulled_ids)
    ]
    # Never wipe the cache because every calendar listing failed.
    if errors and not pulled_ids:
        to_remove = []
    await store.agenda.delete_many([e.event_id for e in to_remove])
    removed = len(to_remove)

    remaining = await store.agenda.list()
    fingerprint = _fingerprint(remaining)
    prev_state = await store.calendar_sync.read() or {}
    fingerprint_changed = prev_state.get("events_fingerprint") != fingerprint
    await _rebuild_daily_agenda(store, remaining, tz=tz)

    last_error = "; ".join(errors) if errors else None
    await store.calendar_sync.write(
        {
            **prev_state,
            "events_fingerprint": fingerprint,
            "last_pull_at": now.isoformat(),
            "calendar_id": calendars[0]["id"] if calendars else "primary",
            "calendars": [
                {"id": c["id"], "summary": c.get("summary"), "primary": bool(c.get("primary"))}
                for c in calendars
            ],
            "last_error": last_error,
            "sync_token": None,
        }
    )

    logger.info(
        "calendar.refresh.done",
        user=store.user_id,
        added=added,
        updated=updated,
        removed=removed,
        total=len(remaining),
        calendars=[c["id"] for c in calendars],
        fingerprint_changed=fingerprint_changed,
        last_error=last_error,
    )
    return RefreshResult(
        added=added,
        updated=updated,
        removed=removed,
        total_cached=len(remaining),
        fingerprint=fingerprint,
        fingerprint_changed=fingerprint_changed,
        calendars=[c["id"] for c in calendars],
        last_error=last_error,
    )


async def _sync_window(
    store: UserStore, *, settings: Any, now: datetime
) -> tuple[datetime, datetime]:
    profile = await store.profile.read() or {}
    days_back = int(profile.get("calendar_window_days_back") or settings.level_cal_days_back)
    days_forward = int(profile.get("calendar_window_days_forward") or settings.level_cal_days_forward)
    return now - timedelta(days=days_back), now + timedelta(days=days_forward)


def _resolve_calendars(service: Any, calendar_id: str | None) -> list[dict[str, Any]]:
    if calendar_id:
        return [{"id": calendar_id, "summary": calendar_id, "primary": calendar_id == "primary"}]
    try:
        return _list_writable_calendars(service)
    except Exception as exc:  # noqa: BLE001
        logger.warning("calendar.list_failed", error=str(exc)[:200])
        return [{"id": "primary", "summary": "primary", "primary": True}]


def _list_writable_calendars(service: Any) -> list[dict[str, Any]]:
    """Calendars the user can write (owner/writer). Skips holidays and subscriptions."""
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        resp = (
            service.calendarList()
            .list(
                minAccessRole="writer",
                showHidden=True,
                pageToken=page_token,
            )
            .execute()
        )
        for cal in resp.get("items") or []:
            items.append(
                {
                    "id": cal["id"],
                    "summary": cal.get("summary") or cal["id"],
                    "primary": bool(cal.get("primary")),
                    "access_role": cal.get("accessRole"),
                }
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if not items:
        items.append({"id": "primary", "summary": "primary", "primary": True})
    return items


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


def _cached_event_id(calendar_id: str, google_id: str) -> str:
    """Event ids are unique per calendar, not globally."""
    return google_id if calendar_id == "primary" else f"{calendar_id}:{google_id}"


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
    summary = (item.get("summary") or "").strip()
    activity = heuristic_activity(summary)

    return CachedEvent(
        event_id=_cached_event_id(calendar_id, item["id"]),
        calendar_id=calendar_id,
        summary=summary,
        time=EventTime(start=start, end=end, tz=str(tz), all_day=all_day),
        location=(item.get("location") or None),
        attendee_tokens=attendee_tokens,
        origin=origin,
        level_reason=private.get("level_reason"),
        etag=item.get("etag"),
        activity_type=activity,
        classified_at=datetime.utcnow() if activity else None,
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
    same_title = existing.summary == incoming.summary
    return incoming.model_copy(
        update={
            "activity_type": existing.activity_type
            if same_title and existing.activity_type
            else incoming.activity_type,
            "classified_at": existing.classified_at
            if same_title and existing.classified_at
            else incoming.classified_at,
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


def agenda_is_fresh(sync_meta: dict[str, Any] | None, *, ttl_seconds: int = 45) -> bool:
    """Skip a Google round-trip on rapid /today reloads (chat, tab focus)."""
    if not sync_meta:
        return False
    raw = sync_meta.get("last_pull_at")
    if not raw:
        return False
    try:
        pulled = date_parser.isoparse(str(raw))
        if pulled.tzinfo is None:
            pulled = pulled.replace(tzinfo=UTC)
        return datetime.now(UTC) - pulled < timedelta(seconds=ttl_seconds)
    except (TypeError, ValueError, OverflowError):
        return False


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
    await store.daily_agenda.upsert_many(
        [DailyAgenda(date=date_key, event_ids=sorted(ids)) for date_key, ids in buckets.items()]
    )
    stale = [d.date for d in await store.daily_agenda.list() if d.date not in buckets]
    await store.daily_agenda.delete_many(stale)


async def ensure_watch(store: UserStore, *, calendar_id: str = "primary") -> bool:
    """Register a push channel if LEVEL_PUBLIC_API_URL is https."""
    settings = get_settings()
    if not settings.level_public_api_url.startswith("https://"):
        logger.info("calendar.watch.skipped", reason="no_public_https_url")
        return False

    service = await build_calendar_client(store)
    channel_id = f"level-{uuid.uuid4().hex[:12]}"
    channel_token = secrets.token_urlsafe(24)
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
