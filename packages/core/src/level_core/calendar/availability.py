"""Conflict + free-slot helpers over Google Calendar event dicts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from level_core.ingest.google_live import _parse_when
from level_core.schemas.commitment import CalendarConflict, EventDraft, FreeSlot, Weekday


_BYDAY_TO_WEEKDAY = {
    Weekday.MO: 0,
    Weekday.TU: 1,
    Weekday.WE: 2,
    Weekday.TH: 3,
    Weekday.FR: 4,
    Weekday.SA: 5,
    Weekday.SU: 6,
}


def _event_bounds(
    event: dict[str, Any],
    *,
    timezone_name: str = "America/Los_Angeles",
) -> tuple[datetime | None, datetime | None]:
    """Return UTC bounds. All-day ``date`` fields are interpreted in ``timezone_name``."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    tz = ZoneInfo(timezone_name)

    # All-day events: Google uses exclusive end date, floating local days.
    if start.get("date") and not start.get("dateTime"):
        try:
            y, m, d = (int(x) for x in str(start["date"]).split("-"))
            start_local = datetime(y, m, d, 0, 0, 0, tzinfo=tz)
            if end.get("date"):
                y2, m2, d2 = (int(x) for x in str(end["date"]).split("-"))
                end_local = datetime(y2, m2, d2, 0, 0, 0, tzinfo=tz)
            else:
                end_local = start_local + timedelta(days=1)
            return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
        except ValueError:
            return None, None

    start_raw = start.get("dateTime") or start.get("date")
    end_raw = end.get("dateTime") or end.get("date")
    start_dt = _parse_when(start_raw)
    end_dt = _parse_when(end_raw)
    if start_dt and end_dt is None:
        end_dt = start_dt + timedelta(hours=1)
    return start_dt, end_dt


def _overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


def _label_local(dt: datetime, tz_name: str) -> str:
    local = dt.astimezone(ZoneInfo(tz_name))
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a')} {hour}:{local.strftime('%M')}{local.strftime('%p').lower()}"


def _is_all_day(event: dict[str, Any]) -> bool:
    start = event.get("start") or {}
    return bool(start.get("date") and not start.get("dateTime"))


def find_conflicts(
    events: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    timezone_name: str = "America/Los_Angeles",
) -> list[CalendarConflict]:
    """Return events that overlap ``[window_start, window_end)``."""
    out: list[CalendarConflict] = []
    for event in events:
        start_dt, end_dt = _event_bounds(event, timezone_name=timezone_name)
        if start_dt is None or end_dt is None:
            continue
        if not _overlaps(window_start, window_end, start_dt, end_dt):
            continue
        summary = (event.get("summary") or "(no title)").strip()
        if _is_all_day(event):
            label = f"All day · {summary}"
        else:
            label = f"{_label_local(start_dt, timezone_name)} · {summary}"
        out.append(
            CalendarConflict(
                summary=summary,
                start=start_dt.isoformat(),
                end=end_dt.isoformat(),
                label=label,
            )
        )
    out.sort(key=lambda c: c.start or "")
    return out


def day_agenda(
    events: list[dict[str, Any]],
    *,
    day_start: datetime,
    day_end: datetime,
    timezone_name: str = "America/Los_Angeles",
) -> list[str]:
    """Human labels for everything on a local day (context, not only conflicts)."""
    lines: list[str] = []
    for event in events:
        start_dt, end_dt = _event_bounds(event, timezone_name=timezone_name)
        if start_dt is None or end_dt is None:
            continue
        if not _overlaps(day_start, day_end, start_dt, end_dt):
            continue
        summary = (event.get("summary") or "(no title)").strip()
        if _is_all_day(event):
            lines.append(f"All day · {summary}")
        else:
            lines.append(f"{_label_local(start_dt, timezone_name)} · {summary}")
    return lines


def find_free_slots(
    events: list[dict[str, Any]],
    *,
    day_start: datetime,
    day_end: datetime,
    duration: timedelta,
    timezone_name: str = "America/Los_Angeles",
    step: timedelta | None = None,
    max_slots: int = 4,
) -> list[FreeSlot]:
    """Greedy free windows of ``duration`` within ``[day_start, day_end]``."""
    step = step or timedelta(minutes=30)
    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        start_dt, end_dt = _event_bounds(event, timezone_name=timezone_name)
        if start_dt is None or end_dt is None:
            continue
        if end_dt <= day_start or start_dt >= day_end:
            continue
        busy.append((max(start_dt, day_start), min(end_dt, day_end)))
    busy.sort(key=lambda x: x[0])

    merged: list[tuple[datetime, datetime]] = []
    for b0, b1 in busy:
        if not merged or b0 > merged[-1][1]:
            merged.append((b0, b1))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b1))

    slots: list[FreeSlot] = []
    cursor = day_start
    for b0, b1 in merged:
        while cursor + duration <= b0 and len(slots) < max_slots:
            end = cursor + duration
            slots.append(
                FreeSlot(
                    start=cursor.isoformat(),
                    end=end.isoformat(),
                    label=f"{_label_local(cursor, timezone_name)}–{_label_local(end, timezone_name).split()[-1]}",
                )
            )
            cursor += step
        cursor = max(cursor, b1)
    while cursor + duration <= day_end and len(slots) < max_slots:
        end = cursor + duration
        slots.append(
            FreeSlot(
                start=cursor.isoformat(),
                end=end.isoformat(),
                label=f"{_label_local(cursor, timezone_name)}–{_label_local(end, timezone_name).split()[-1]}",
            )
        )
        cursor += step
    return slots


def find_free_slots_nearby(
    events: list[dict[str, Any]],
    *,
    anchor: datetime,
    duration: timedelta,
    timezone_name: str = "America/Los_Angeles",
    days: int = 4,
    day_start_hour: int = 11,
    day_end_hour: int = 21,
    max_slots: int = 4,
    preferred_weekdays: set[int] | None = None,
) -> list[FreeSlot]:
    """Free slots on/after the anchor day.

    When ``preferred_weekdays`` is set (0=Mon…6=Sun), only those weekdays are
    searched — so a weekend ask does not suggest Tuesday alternatives.
    """
    tz = ZoneInfo(timezone_name)
    local_anchor = anchor.astimezone(tz)
    collected: list[FreeSlot] = []
    # Scan enough calendar days to hit preferred weekdays (e.g. Fri → Sat/Sun).
    scan_limit = max(days, 8) if preferred_weekdays else days
    offset = 0
    checked = 0
    while checked < scan_limit and len(collected) < max_slots:
        day = (local_anchor + timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        offset += 1
        if preferred_weekdays is not None and day.weekday() not in preferred_weekdays:
            continue
        checked += 1
        start = day.replace(hour=day_start_hour)
        end = day.replace(hour=day_end_hour)
        # Prefer evening-ish when asking about dinner times.
        if (
            preferred_weekdays is None
            and local_anchor.hour >= 16
            and day.date() == local_anchor.date()
        ):
            start = max(start, local_anchor.replace(second=0, microsecond=0))
        slots = find_free_slots(
            events,
            day_start=start.astimezone(timezone.utc),
            day_end=end.astimezone(timezone.utc),
            duration=duration,
            timezone_name=timezone_name,
            max_slots=max_slots - len(collected),
        )
        collected.extend(slots)
    return collected[:max_slots]


def draft_window(
    draft: EventDraft,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """First occurrence window for a draft (UTC-aware)."""
    now = now or datetime.now(tz=timezone.utc)
    tz = ZoneInfo(draft.timezone)
    local_now = now.astimezone(tz)
    hour, minute = (int(x) for x in draft.local_time.split(":", 1))

    if draft.local_date:
        y, m, d = (int(x) for x in draft.local_date.split("-"))
        start_local = datetime(y, m, d, hour, minute, tzinfo=tz)
    elif draft.by_days:
        target_wdays = {_BYDAY_TO_WEEKDAY[d] for d in draft.by_days}
        start_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for _ in range(14):
            if start_local.weekday() in target_wdays and start_local >= local_now:
                break
            start_local += timedelta(days=1)
        else:
            start_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            while start_local.weekday() not in target_wdays:
                start_local += timedelta(days=1)
    else:
        start_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start_local < local_now:
            start_local += timedelta(days=1)

    end_local = start_local + timedelta(minutes=draft.duration_minutes)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def draft_search_day(
    draft: EventDraft,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Local-day bounds (as UTC) used for free-slot search around a draft."""
    window_start, _ = draft_window(draft, now=now)
    tz = ZoneInfo(draft.timezone)
    local = window_start.astimezone(tz)
    day_start = local.replace(hour=8, minute=0, second=0, microsecond=0)
    day_end = local.replace(hour=22, minute=0, second=0, microsecond=0)
    return day_start.astimezone(timezone.utc), day_end.astimezone(timezone.utc)


def occurrence_windows(
    draft: EventDraft,
    *,
    now: datetime | None = None,
    weeks: int = 2,
) -> list[tuple[datetime, datetime]]:
    """Upcoming occurrence windows for conflict scanning.

    Expands ``by_days`` (e.g. weekend SA/SU) even when the draft is not a
    recurring series — availability asks still need those preferred days.
    """
    now = now or datetime.now(tz=timezone.utc)
    first_start, first_end = draft_window(draft, now=now)
    duration = first_end - first_start
    if not draft.by_days:
        return [(first_start, first_end)]

    tz = ZoneInfo(draft.timezone)
    target = {_BYDAY_TO_WEEKDAY[d] for d in draft.by_days}
    hour, minute = (int(x) for x in draft.local_time.split(":", 1))
    local_now = now.astimezone(tz)
    cursor = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[tuple[datetime, datetime]] = []
    for _ in range(weeks * 7 + 1):
        if cursor.weekday() in target:
            start_local = cursor.replace(hour=hour, minute=minute)
            if start_local >= local_now - timedelta(minutes=5):
                start_utc = start_local.astimezone(timezone.utc)
                out.append((start_utc, start_utc + duration))
        cursor += timedelta(days=1)
        if len(out) >= weeks * max(1, len(draft.by_days)):
            break
    return out or [(first_start, first_end)]


__all__ = [
    "day_agenda",
    "draft_search_day",
    "draft_window",
    "find_conflicts",
    "find_free_slots",
    "find_free_slots_nearby",
    "occurrence_windows",
]
