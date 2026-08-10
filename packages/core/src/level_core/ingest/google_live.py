"""Live Google Calendar pull using a user's OAuth credentials."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.discovery import build

from level_core.auth.google_oauth import credentials_from_token
from level_core.schemas.signal import Signal, SignalSource
from level_core.schemas.user import OAuthToken

# Titles that appear this many times in the window are treated as a repeating
# habit / reminder — skip them entirely (we want exceptions, not the grind).
_REPEAT_TITLE_THRESHOLD = 3



def _parse_when(start_raw: str | None) -> datetime | None:
    """Parse Google Calendar date/dateTime into an aware UTC datetime.

    All-day events arrive as ``YYYY-MM-DD`` (naive). Timed events may be
    offset-aware. Callers subtract these from ``datetime.now(tz=utc)``, so
    every return value must be timezone-aware.
    """
    if not start_raw:
        return None
    try:
        # date-only all-day events
        if len(start_raw) == 10 and start_raw[4] == "-" and start_raw[7] == "-":
            return datetime.fromisoformat(start_raw).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _month_shift(year: int, month: int, delta: int) -> tuple[int, int]:
    month += delta
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return year, month


def calendar_window(
    now: datetime | None = None,
    *,
    days_back: int = 14,
    days_forward: int = 28,
) -> tuple[datetime, datetime]:
    """Tight window: ~2 weeks back (patterns) + ~4 weeks ahead (planning).

    Caregivers don't need years of history — just enough to see load patterns
    and the near-term schedule that drives decisions.
    """
    now = now or datetime.now(tz=timezone.utc)
    start = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=days_forward)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    return start, end


def _norm_title(summary: str) -> str:
    return re.sub(r"\s+", " ", summary.strip().lower())



def _calendar_statement(summary: str, start_raw: str | None, description: str) -> str:
    when = _parse_when(start_raw)
    if when:
        when_s = when.strftime("%a %b %d %Y")
        if when.hour or when.minute:
            when_s = when.strftime("%a %b %d %Y %I:%M%p").replace(" 0", " ")
    else:
        when_s = start_raw or "unknown date"
    stmt = f"On my calendar {when_s}: {summary}"
    if description:
        snippet = re.sub(r"\s+", " ", description).strip()[:180]
        if snippet:
            stmt += f" — {snippet}"
    return stmt[:500]


def filter_calendar_events(
    items: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    max_events: int = 40,
) -> list[dict[str, Any]]:
    """Drop recurring / high-frequency repeats; keep unique one-offs near now."""
    now = now or datetime.now(tz=timezone.utc)

    one_offs = [e for e in items if not e.get("recurringEventId")]

    title_counts = Counter(
        _norm_title(e.get("summary") or "")
        for e in one_offs
        if (e.get("summary") or "").strip()
    )
    repeating_titles = {
        t for t, n in title_counts.items() if t and n >= _REPEAT_TITLE_THRESHOLD
    }

    best_by_title: dict[str, dict[str, Any]] = {}
    for event in one_offs:
        summary = (event.get("summary") or "").strip()
        if not summary:
            continue
        key = _norm_title(summary)
        if key in repeating_titles:
            continue
        start = event.get("start") or {}
        start_raw = start.get("dateTime") or start.get("date")
        occurred_at = _parse_when(start_raw)
        prev = best_by_title.get(key)
        if prev is None:
            best_by_title[key] = event
            continue
        prev_start = prev.get("start") or {}
        prev_raw = prev_start.get("dateTime") or prev_start.get("date")
        prev_at = _parse_when(prev_raw)
        if occurred_at and prev_at:
            if abs((occurred_at - now).total_seconds()) < abs((prev_at - now).total_seconds()):
                best_by_title[key] = event
        elif occurred_at and not prev_at:
            best_by_title[key] = event

    def _sort_key(e: dict[str, Any]) -> tuple[int, float]:
        start = e.get("start") or {}
        when = _parse_when(start.get("dateTime") or start.get("date")) or now
        has_desc = 0 if (e.get("description") or "").strip() else 1
        return (has_desc, abs((when - now).total_seconds()))

    return sorted(best_by_title.values(), key=_sort_key)[:max_events]


def _list_primary_events(
    service: Any, *, time_min: str, time_max: str
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(resp.get("items") or [])
        page_token = resp.get("nextPageToken")
        if not page_token or len(items) >= 800:
            break
    return items


def _event_to_signal(event: dict[str, Any], *, user_id: str) -> Signal | None:
    event_id = event.get("id") or ""
    if not event_id:
        return None
    summary = (event.get("summary") or "(no title)").strip()
    description = (event.get("description") or "").strip()
    start = event.get("start") or {}
    start_raw = start.get("dateTime") or start.get("date")
    occurred_at = _parse_when(start_raw)
    statement = _calendar_statement(summary, start_raw, description)
    text = f"Calendar: {statement}"
    if description:
        text += f"\n{description[:2000]}"
    return Signal(
        user_id=user_id,
        source=SignalSource.GCAL,
        external_id=f"gcal:{event_id}",
        occurred_at=occurred_at,
        text=text[:8000],
    )


@dataclass(slots=True)
class CalendarPull:
    signals: list[Signal] = field(default_factory=list)
    window_start: datetime | None = None
    window_end: datetime | None = None


async def pull_calendar(
    token: OAuthToken,
    *,
    user_id: str,
    max_events: int = 25,
) -> CalendarPull:
    """Fetch filtered calendar events for ingest / priority inference."""
    creds = credentials_from_token(token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    now = datetime.now(tz=timezone.utc)
    window_start, window_end = calendar_window(now)
    raw = _list_primary_events(
        service,
        time_min=window_start.isoformat(),
        time_max=window_end.isoformat(),
    )
    selected = filter_calendar_events(raw, now=now, max_events=max_events)
    signals: list[Signal] = []
    seen: set[str] = set()
    for event in selected:
        sig = _event_to_signal(event, user_id=user_id)
        if sig is None or sig.external_id in seen:
            continue
        seen.add(sig.external_id)
        signals.append(sig)
    return CalendarPull(
        signals=signals,
        window_start=window_start,
        window_end=window_end,
    )


async def fetch_calendar_signals(
    token: OAuthToken,
    *,
    user_id: str,
    max_events: int = 25,
) -> AsyncIterator[Signal]:
    pull = await pull_calendar(token, user_id=user_id, max_events=max_events)
    for signal in pull.signals:
        yield signal


async def list_primary_events_window(
    token: OAuthToken,
    *,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    """List primary-calendar events in ``[time_min, time_max]`` (expanded instances)."""
    creds = credentials_from_token(token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _list_primary_events(
        service,
        time_min=time_min.astimezone(timezone.utc).isoformat(),
        time_max=time_max.astimezone(timezone.utc).isoformat(),
    )


async def create_calendar_event(
    token: OAuthToken,
    *,
    summary: str,
    start: datetime,
    end: datetime,
    timezone_name: str = "America/Los_Angeles",
    description: str = "",
    by_days: list[str] | None = None,
) -> dict[str, Any]:
    """Insert a primary-calendar event; optional weekly RRULE via ``by_days`` (MO,TU,…)."""
    from zoneinfo import ZoneInfo

    creds = credentials_from_token(token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    tz = ZoneInfo(timezone_name)
    start_wall = start.astimezone(tz)
    end_wall = end.astimezone(tz)
    body: dict[str, Any] = {
        "summary": summary,
        "description": description or "Added via Level (confirmed).",
        "start": {
            "dateTime": start_wall.isoformat(timespec="seconds"),
            "timeZone": timezone_name,
        },
        "end": {
            "dateTime": end_wall.isoformat(timespec="seconds"),
            "timeZone": timezone_name,
        },
    }
    if by_days:
        days = ",".join(by_days)
        body["recurrence"] = [f"RRULE:FREQ=WEEKLY;BYDAY={days}"]
    return (
        service.events()
        .insert(calendarId="primary", body=body)
        .execute()
    )


async def fetch_day_events(
    token: OAuthToken,
    *,
    day_offset: int = 0,
    now: datetime | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> list[dict[str, Any]]:
    """Return primary-calendar events for a local calendar day (``day_offset`` from today)."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(tz=timezone.utc)
    local = now.astimezone(ZoneInfo(timezone_name)) + timedelta(days=day_offset)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = local.replace(hour=23, minute=59, second=59, microsecond=0)
    raw = await list_primary_events_window(
        token,
        time_min=day_start.astimezone(timezone.utc),
        time_max=day_end.astimezone(timezone.utc),
    )
    out: list[dict[str, Any]] = []
    for event in raw:
        summary = (event.get("summary") or "(no title)").strip()
        start = event.get("start") or {}
        start_raw = start.get("dateTime") or start.get("date")
        end = event.get("end") or {}
        end_raw = end.get("dateTime") or end.get("date")
        out.append(
            {
                "id": event.get("id") or "",
                "summary": summary,
                "start": start_raw,
                "end": end_raw,
                "all_day": bool(start.get("date") and not start.get("dateTime")),
            }
        )
    return out


async def fetch_today_events(
    token: OAuthToken,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> list[dict[str, Any]]:
    """Return today's primary-calendar events (for the Today home screen)."""
    return await fetch_day_events(
        token, day_offset=0, now=now, timezone_name=timezone_name
    )


@dataclass(slots=True)
class IncrementalCalendarPull:
    items: list[dict[str, Any]] = field(default_factory=list)
    next_sync_token: str | None = None
    full_resync: bool = False


class SyncTokenExpiredError(RuntimeError):
    """Google returned 410 — caller must drop syncToken and full-sync again."""


def _list_events_page(
    service: Any,
    *,
    sync_token: str | None = None,
    time_min: str | None = None,
    time_max: str | None = None,
    page_token: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "calendarId": "primary",
        "singleEvents": True,
        "showDeleted": True,
        "maxResults": 250,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    if sync_token:
        kwargs["syncToken"] = sync_token
    else:
        if time_min:
            kwargs["timeMin"] = time_min
        if time_max:
            kwargs["timeMax"] = time_max
        kwargs["orderBy"] = "startTime"
    return service.events().list(**kwargs).execute()


async def pull_calendar_incremental(
    token: OAuthToken,
    *,
    sync_token: str | None = None,
    days_back: int = 14,
    days_forward: int = 28,
) -> IncrementalCalendarPull:
    """List primary events; with ``sync_token`` returns only deltas.

    Does not touch Memory Bank / LLM — agenda freshness only.
    """
    from googleapiclient.errors import HttpError

    creds = credentials_from_token(token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    window_start, window_end = calendar_window(
        days_back=days_back, days_forward=days_forward
    )
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    next_sync: str | None = None
    full_resync = not bool(sync_token)
    first_page = True

    try:
        while True:
            use_sync = bool(sync_token) and not full_resync and first_page
            use_window = full_resync and first_page
            resp = _list_events_page(
                service,
                sync_token=sync_token if use_sync else None,
                time_min=window_start.isoformat() if use_window else None,
                time_max=window_end.isoformat() if use_window else None,
                page_token=page_token,
            )
            first_page = False
            items.extend(resp.get("items") or [])
            page_token = resp.get("nextPageToken")
            next_sync = resp.get("nextSyncToken") or next_sync
            if not page_token:
                break
            if len(items) >= 1200:
                break
    except HttpError as exc:
        if getattr(exc, "resp", None) is not None and exc.resp.status == 410:
            raise SyncTokenExpiredError("calendar syncToken expired") from exc
        raise

    return IncrementalCalendarPull(
        items=items,
        next_sync_token=next_sync,
        full_resync=full_resync,
    )


async def watch_primary_calendar(
    token: OAuthToken,
    *,
    channel_id: str,
    address: str,
    channel_token: str,
    ttl_seconds: int = 6 * 24 * 3600,
) -> dict[str, Any]:
    """Register a Google Calendar push channel for primary events."""
    creds = credentials_from_token(token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    expiration_ms = int(
        (datetime.now(tz=timezone.utc) + timedelta(seconds=ttl_seconds)).timestamp()
        * 1000
    )
    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": address,
        "token": channel_token,
        "expiration": expiration_ms,
    }
    return service.events().watch(calendarId="primary", body=body).execute()


async def stop_calendar_channel(
    token: OAuthToken,
    *,
    channel_id: str,
    resource_id: str,
) -> None:
    creds = credentials_from_token(token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    try:
        service.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()
    except Exception:  # noqa: BLE001
        # Channel may already be gone — ignore.
        return


__all__ = [
    "CalendarPull",
    "IncrementalCalendarPull",
    "SyncTokenExpiredError",
    "calendar_window",
    "create_calendar_event",
    "fetch_calendar_signals",
    "fetch_day_events",
    "fetch_today_events",
    "filter_calendar_events",
    "list_primary_events_window",
    "pull_calendar",
    "pull_calendar_incremental",
    "stop_calendar_channel",
    "watch_primary_calendar",
]
