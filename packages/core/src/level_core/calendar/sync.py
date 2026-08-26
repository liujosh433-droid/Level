"""Agenda sync: pull calendar events into `agenda_cache`, no LLM in this path.

Two triggers:
  - `refresh_agenda()` called explicitly on Today load, on OAuth complete,
    or from the webhook.
  - `ensure_watch()` registers a Google Calendar push channel so future
    changes hit /v1/calendar/webhook.

Sync strategy (v2, this session):
  - Per-calendar Google `syncToken` PERSISTED in
    `calendar_sync["sync_tokens"][calendar_id]`. After the first pull for
    a calendar we save `nextSyncToken`; subsequent pulls send that token
    and Google returns ONLY changed / deleted events. This drops the
    typical rescan from ~500 events refetched to ~0-5.
  - HTTP 410 on the token means "expired". We drop the token and fall
    back to a full time-window pull automatically.
  - Cancellations arrive with `status="cancelled"`; we honor them
    incrementally by removing the cached row.
  - Per-calendar pulls run through `asyncio.gather` so multi-calendar
    users pay one Google round-trip, not N.
  - The store is read ONCE at the start and every diff is computed in
    memory - saves ~2*N Firestore doc reads per refresh vs. v1.
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
    # New in v2: surfaced so callers (and the SUBMISSION demo) can prove
    # incremental sync is happening. `incremental_hits` == number of
    # calendars that used a valid syncToken this pull.
    incremental_hits: int = 0
    full_pulls: int = 0


@dataclass
class _CalendarPull:
    calendar_id: str
    summary: str
    primary: bool
    events: list[dict[str, Any]]
    next_sync_token: str | None
    used_sync_token: bool
    error: str | None = None


async def refresh_agenda(
    store: UserStore, *, calendar_id: str | None = None
) -> RefreshResult:
    settings = get_settings()
    tz = await tz_for_store(store)
    now = datetime.now(UTC)
    time_min, time_max = await _sync_window(store, settings=settings, now=now)

    prev_state = await store.calendar_sync.read() or {}
    sync_tokens: dict[str, str] = dict(prev_state.get("sync_tokens") or {})

    with span("calendar.refresh", user=store.user_id, calendar=calendar_id or "all"):
        service = await build_calendar_client(store)
        calendars = await asyncio.to_thread(_resolve_calendars, service, calendar_id)

    # Single full read at the start. Everything else diffs in memory.
    existing_list = await store.agenda.list()
    by_id: dict[str, CachedEvent] = {e.event_id: e for e in existing_list}
    existing_ids: set[str] = set(by_id.keys())

    # Parallel Google pulls, one per calendar. Google client is sync so
    # asyncio.to_thread runs them on separate threads = concurrent HTTP.
    pull_tasks = [
        _pull_calendar(
            service=service,
            calendar=cal,
            time_min=time_min,
            time_max=time_max,
            sync_token=sync_tokens.get(cal["id"]),
        )
        for cal in calendars
    ]
    pulls: list[_CalendarPull] = await asyncio.gather(*pull_tasks)

    added = 0
    updated = 0
    removed = 0
    seen_by_calendar: dict[str, set[str]] = {}
    incrementally_removed: set[str] = set()
    to_upsert: list[CachedEvent] = []
    errors: list[str] = []
    incremental_hits = 0
    full_pulls = 0

    for pull in pulls:
        if pull.error:
            errors.append(f"{pull.summary}: {pull.error}")
            logger.warning(
                "calendar.refresh.calendar_failed",
                calendar=pull.calendar_id,
                error=pull.error[:200],
            )
            continue

        if pull.used_sync_token:
            incremental_hits += 1
        else:
            full_pulls += 1

        seen: set[str] = set()
        for item in pull.events:
            # Cancellations arrive with status=cancelled. On incremental
            # sync we MUST remove the cached row; on full pull, the
            # missing-in-window scan below catches it.
            if item.get("status") == "cancelled":
                cached_id = _cached_event_id(
                    pull.calendar_id, item.get("id") or ""
                )
                if cached_id in existing_ids:
                    incrementally_removed.add(cached_id)
                continue

            cached = _to_cached_event(item, calendar_id=pull.calendar_id, tz=tz)
            if not cached:
                continue
            seen.add(cached.event_id)
            existing = by_id.get(cached.event_id)
            if existing and existing.etag == cached.etag:
                continue
            if existing:
                to_upsert.append(_merge_preserving_ai(existing, cached))
                updated += 1
            else:
                to_upsert.append(cached)
                added += 1

        seen_by_calendar[pull.calendar_id] = seen
        # Only persist syncToken on a successful pull. If we hit 410 it's
        # already been cleared by _pull_calendar.
        if pull.next_sync_token:
            sync_tokens[pull.calendar_id] = pull.next_sync_token
        elif pull.used_sync_token:
            # Successful incremental pull but no new nextSyncToken? Google
            # sometimes omits it mid-stream; keep the old one.
            pass
        elif pull.calendar_id in sync_tokens:
            # We just did a fresh full pull that returned no nextSyncToken;
            # the old token (if any) is stale, drop it.
            sync_tokens.pop(pull.calendar_id, None)

    # Full-pull removal: events cached inside the window that Google
    # didn't return this time. Only applies to calendars we successfully
    # full-pulled (not incremental ones).
    full_pulled_ids: set[str] = {
        p.calendar_id for p in pulls if not p.used_sync_token and not p.error
    }
    full_pull_seen: set[str] = set()
    for cid in full_pulled_ids:
        full_pull_seen |= seen_by_calendar.get(cid, set())
    window_removed: set[str] = {
        e.event_id
        for e in existing_list
        if e.calendar_id in full_pulled_ids
        and e.event_id not in full_pull_seen
        and _within_window(e, time_min, time_max)
    }

    all_removed = incrementally_removed | window_removed
    # Never wipe the cache when every calendar failed.
    if errors and not full_pulled_ids and incremental_hits == 0:
        all_removed = set()

    # Apply changes.
    if to_upsert:
        await store.agenda.upsert_many(to_upsert)
    if all_removed:
        await store.agenda.delete_many(sorted(all_removed))
        removed = len(all_removed)

    # Build the current in-memory view of the cache without another list().
    upsert_by_id = {e.event_id: e for e in to_upsert}
    remaining: list[CachedEvent] = [
        upsert_by_id.get(e.event_id, e)
        for e in existing_list
        if e.event_id not in all_removed
    ]
    for e in to_upsert:
        if e.event_id not in {x.event_id for x in remaining}:
            remaining.append(e)

    fingerprint = _fingerprint(remaining)
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
                {
                    "id": c["id"],
                    "summary": c.get("summary"),
                    "primary": bool(c.get("primary")),
                }
                for c in calendars
            ],
            "last_error": last_error,
            "sync_tokens": sync_tokens,
            "sync_token": None,  # legacy field; kept for backward-compat readers
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
        incremental_hits=incremental_hits,
        full_pulls=full_pulls,
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
        incremental_hits=incremental_hits,
        full_pulls=full_pulls,
    )


async def _pull_calendar(
    *,
    service: Any,
    calendar: dict[str, Any],
    time_min: datetime,
    time_max: datetime,
    sync_token: str | None,
) -> _CalendarPull:
    """Pull one calendar. Prefer incremental syncToken; fall back on 410."""
    calendar_id = calendar["id"]
    summary = calendar.get("summary") or calendar_id
    primary = bool(calendar.get("primary"))

    try:
        if sync_token:
            try:
                events, next_token = await asyncio.to_thread(
                    _list_events_incremental,
                    service=service,
                    calendar_id=calendar_id,
                    sync_token=sync_token,
                )
                return _CalendarPull(
                    calendar_id=calendar_id,
                    summary=summary,
                    primary=primary,
                    events=events,
                    next_sync_token=next_token,
                    used_sync_token=True,
                )
            except _SyncTokenExpired:
                logger.info(
                    "calendar.sync_token_expired",
                    calendar=calendar_id,
                )
                # Fall through to full pull.

        events, next_token = await asyncio.to_thread(
            _list_events_full,
            service=service,
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
        )
        return _CalendarPull(
            calendar_id=calendar_id,
            summary=summary,
            primary=primary,
            events=events,
            next_sync_token=next_token,
            used_sync_token=False,
        )
    except Exception as exc:  # noqa: BLE001 - one calendar failing must not sink the rest
        return _CalendarPull(
            calendar_id=calendar_id,
            summary=summary,
            primary=primary,
            events=[],
            next_sync_token=None,
            used_sync_token=False,
            error=str(exc),
        )


class _SyncTokenExpired(Exception):
    """HTTP 410 - Google says the token is too old, pull the window fresh."""


async def _sync_window(
    store: UserStore, *, settings: Any, now: datetime
) -> tuple[datetime, datetime]:
    profile = await store.profile.read() or {}
    days_back = int(
        profile.get("calendar_window_days_back") or settings.level_cal_days_back
    )
    days_forward = int(
        profile.get("calendar_window_days_forward") or settings.level_cal_days_forward
    )
    return now - timedelta(days=days_back), now + timedelta(days=days_forward)


def _resolve_calendars(
    service: Any, calendar_id: str | None
) -> list[dict[str, Any]]:
    if calendar_id:
        return [
            {
                "id": calendar_id,
                "summary": calendar_id,
                "primary": calendar_id == "primary",
            }
        ]
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


def _list_events_full(
    *,
    service: Any,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> tuple[list[dict[str, Any]], str | None]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    next_sync_token: str | None = None
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
            next_sync_token = resp.get("nextSyncToken")
            break
    return items, next_sync_token


def _list_events_incremental(
    *,
    service: Any,
    calendar_id: str,
    sync_token: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Incremental pull with syncToken. Raises _SyncTokenExpired on 410."""
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    next_sync_token: str | None = None
    current_token: str | None = sync_token
    while True:
        try:
            resp = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    syncToken=current_token if page_token is None else None,
                    pageToken=page_token,
                    singleEvents=True,
                    maxResults=250,
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 410:
                raise _SyncTokenExpired() from exc
            raise
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            next_sync_token = resp.get("nextSyncToken") or current_token
            break
    return items, next_sync_token


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
        name = (
            a.get("displayName") or a.get("email", "").split("@")[0] or ""
        ).strip()
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
        hasher.update(
            f"{e.event_id}:{e.etag}:{e.summary}:{e.time.start.isoformat()}".encode()
        )
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
    """Diff-only rebuild: only upsert buckets whose event_id list changed.

    Previous impl upserted every bucket on every refresh even when nothing
    moved - N doc writes per refresh at Firestore. Now we read once, diff,
    and write only what actually changed.
    """
    new_buckets: dict[str, list[str]] = {}
    for e in events:
        start = e.time.start
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        local = start.astimezone(tz)
        key = local.strftime("%Y-%m-%d")
        new_buckets.setdefault(key, []).append(e.event_id)
    for k in new_buckets:
        new_buckets[k].sort()

    existing = await store.daily_agenda.list()
    existing_by_date: dict[str, DailyAgenda] = {d.date: d for d in existing}

    upserts: list[DailyAgenda] = []
    for date_key, ids in new_buckets.items():
        prev = existing_by_date.get(date_key)
        if prev and prev.event_ids == ids:
            continue
        upserts.append(DailyAgenda(date=date_key, event_ids=ids))

    stale = [d.date for d in existing if d.date not in new_buckets]

    if upserts:
        await store.daily_agenda.upsert_many(upserts)
    if stale:
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
