"""Live Google Calendar + Drive pull using a user's OAuth credentials."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable
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

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "for",
        "with",
        "from",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "is",
        "are",
        "be",
        "as",
        "my",
        "me",
        "our",
        "your",
        "meeting",
        "meet",
        "call",
        "zoom",
        "sync",
        "weekly",
        "daily",
        "standup",
        "stand",
        "up",
        "catch",
        "chat",
        "hangout",
        "event",
        "reminder",
        "block",
        "busy",
        "focus",
        "time",
        "ooo",
        "out",
        "office",
        "http",
        "https",
        "www",
        "com",
    }
)


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


def topics_from_calendar_titles(titles: Iterable[str]) -> set[str]:
    """Extract coarse keywords / short phrases from calendar titles for Drive matching."""
    topics: set[str] = set()
    for title in titles:
        norm = _norm_title(title)
        if not norm:
            continue
        # Keep short full titles as phrases (e.g. "muay thai", "parent teacher").
        if 3 <= len(norm) <= 40 and not all(t in _STOPWORDS for t in norm.split()):
            topics.add(norm)
        for tok in re.findall(r"[a-z0-9]{3,}", norm):
            if tok not in _STOPWORDS:
                topics.add(tok)
    return topics


def drive_topic_score(name: str, body: str, topics: set[str]) -> int:
    """Higher = more relevant to calendar topics. 0 = no match."""
    if not topics:
        return 0
    hay_name = name.lower()
    hay_body = body.lower()[:4000]
    score = 0
    for topic in topics:
        if topic in hay_name:
            score += 10 + min(len(topic), 24)
        elif topic in hay_body:
            score += 3 + min(len(topic), 12)
    return score


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
    topics: set[str] = field(default_factory=set)
    window_start: datetime | None = None
    window_end: datetime | None = None


async def pull_calendar(
    token: OAuthToken,
    *,
    user_id: str,
    max_events: int = 25,
) -> CalendarPull:
    """Fetch filtered calendar events + topic keywords for Drive matching."""
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
    titles: list[str] = []
    seen: set[str] = set()
    for event in selected:
        sig = _event_to_signal(event, user_id=user_id)
        if sig is None or sig.external_id in seen:
            continue
        seen.add(sig.external_id)
        signals.append(sig)
        titles.append((event.get("summary") or "").strip())
    return CalendarPull(
        signals=signals,
        topics=topics_from_calendar_titles(titles),
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


async def fetch_today_events(
    token: OAuthToken,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> list[dict[str, Any]]:
    """Return today's primary-calendar events (for the Today home screen)."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(tz=timezone.utc)
    local = now.astimezone(ZoneInfo(timezone_name))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = local.replace(hour=23, minute=59, second=59, microsecond=0)
    raw = await list_primary_events_window(
        token,
        time_min=day_start.astimezone(timezone.utc),
        time_max=day_end.astimezone(timezone.utc),
    )
    # Keep recurring instances for *today* — they are the real schedule.
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


async def fetch_drive_signals(
    token: OAuthToken,
    *,
    user_id: str,
    topics: set[str] | None = None,
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    max_files: int = 4,
    candidate_pool: int = 25,
) -> AsyncIterator[Signal]:
    """Pull Google Docs that match calendar topics and fall in the time window.

    If ``topics`` is empty, yields nothing — random Drive noise is not useful.
    """
    topics = topics or set()
    if not topics:
        return

    window_start, window_end = calendar_window()
    modified_after = modified_after or window_start
    modified_before = modified_before or window_end

    creds = credentials_from_token(token)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    # Drive query uses RFC3339 timestamps.
    after_s = modified_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    before_s = modified_before.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = (
        "mimeType='application/vnd.google-apps.document' and trashed=false "
        f"and modifiedTime >= '{after_s}' and modifiedTime <= '{before_s}'"
    )
    results = (
        drive.files()
        .list(
            q=query,
            pageSize=min(candidate_pool, 100),
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )
    candidates = results.get("files") or []

    # Fast path: score by filename first; only export promising docs.
    scored_meta: list[tuple[int, dict[str, Any]]] = []
    for f in candidates:
        name = (f.get("name") or "").strip()
        name_score = drive_topic_score(name, "", topics)
        if name_score > 0:
            scored_meta.append((name_score, f))
    scored_meta.sort(key=lambda x: x[0], reverse=True)

    # If nothing matched on name, try a few topic-targeted Drive searches.
    if not scored_meta:
        # Prefer longer / phrase topics for search.
        search_topics = sorted(topics, key=len, reverse=True)[:8]
        seen_ids: set[str] = set()
        for topic in search_topics:
            # Escape single quotes for Drive query language.
            safe = topic.replace("'", "\\'")
            q = (
                "mimeType='application/vnd.google-apps.document' and trashed=false "
                f"and modifiedTime >= '{after_s}' and modifiedTime <= '{before_s}' "
                f"and (name contains '{safe}' or fullText contains '{safe}')"
            )
            try:
                hit = (
                    drive.files()
                    .list(
                        q=q,
                        pageSize=5,
                        fields="files(id, name, modifiedTime)",
                        orderBy="modifiedTime desc",
                    )
                    .execute()
                )
            except Exception:  # noqa: BLE001
                continue
            for f in hit.get("files") or []:
                fid = f.get("id") or ""
                if not fid or fid in seen_ids:
                    continue
                seen_ids.add(fid)
                scored_meta.append((drive_topic_score(f.get("name") or "", "", topics) or 1, f))
            if len(scored_meta) >= max_files * 2:
                break
        scored_meta.sort(key=lambda x: x[0], reverse=True)

    exported = 0
    for _score, f in scored_meta:
        if exported >= max_files:
            break
        file_id = f.get("id") or ""
        name = (f.get("name") or "untitled").strip()
        try:
            raw = (
                drive.files()
                .export(fileId=file_id, mimeType="text/plain")
                .execute()
            )
            body = (
                raw.decode("utf-8", errors="replace")
                if isinstance(raw, bytes)
                else str(raw)
            )
        except Exception:  # noqa: BLE001
            continue
        body = body.strip()
        if len(body) < 40:
            continue
        # Confirm topic match against body too (catches weak name hits).
        if drive_topic_score(name, body, topics) <= 0:
            continue
        occurred_at = None
        if f.get("modifiedTime"):
            try:
                occurred_at = datetime.fromisoformat(
                    f["modifiedTime"].replace("Z", "+00:00")
                )
            except ValueError:
                occurred_at = None
        exported += 1
        yield Signal(
            user_id=user_id,
            source=SignalSource.GDRIVE,
            external_id=f"gdrive:{file_id}",
            occurred_at=occurred_at,
            text=f"Drive doc: {name}\n{body[:6000]}",
        )


__all__ = [
    "CalendarPull",
    "calendar_window",
    "create_calendar_event",
    "drive_topic_score",
    "fetch_calendar_signals",
    "fetch_drive_signals",
    "fetch_today_events",
    "filter_calendar_events",
    "list_primary_events_window",
    "pull_calendar",
    "topics_from_calendar_titles",
]
