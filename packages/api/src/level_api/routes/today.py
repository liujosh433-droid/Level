"""Today page: greeting, events, missing usuals, day summary for TTS."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import refresh_agenda
from level_core.calendar.usuals import missing_usuals_this_week, missing_usuals_today
from level_core.config import get_settings
from level_core.schemas import ActivityType, CachedEvent, LoadBucket
from level_core.storage.base import UserStore
from level_core.voice.summary import get_daily_summary

from level_api.deps import get_user_store

router = APIRouter()


@router.get("")
async def get_today(store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)

    tokens = await store.tokens.read() or {}
    if tokens.get("access_token") and settings.is_local:
        try:
            result = await refresh_agenda(store)
        except Exception:
            result = None
        try:
            existing = await store.agenda.list()
            needs_enrich = (result and result.fingerprint_changed) or any(
                e.activity_type is None for e in existing
            )
            if needs_enrich:
                await enrich_agenda(store)
        except Exception:
            pass

    events = await store.agenda.list()
    events.sort(key=lambda e: e.time.start)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)
    todays = [e for e in events if e.time.start.astimezone(tz).date() == today]
    tomorrows = [e for e in events if e.time.start.astimezone(tz).date() == tomorrow]

    usuals = await store.usuals.list()
    missing = missing_usuals_today(usuals=usuals, todays_events=todays)

    reminders_by_id = {r.reminder_id: r for r in await store.reminders.list()}
    people_by_id = {p.person_id: p for p in await store.people.list()}
    events_by_id = {e.event_id: e for e in events}

    def _view(e: Any) -> dict[str, Any]:
        return {
            "event_id": e.event_id,
            "summary": e.summary,
            "start": e.time.start.astimezone(tz).isoformat(),
            "end": e.time.end.astimezone(tz).isoformat(),
            "activity_type": e.activity_type,
            "origin": e.origin,
            "level_reason": e.level_reason,
            "people": [
                {
                    "person_id": pid,
                    "display_name": people_by_id.get(pid).display_name if people_by_id.get(pid) else None,
                }
                for pid in e.matched_person_ids
                if people_by_id.get(pid)
            ],
            "reminders": [
                {"reminder_id": rid, "text": reminders_by_id[rid].text}
                for rid in e.matched_reminder_ids
                if reminders_by_id.get(rid)
            ],
        }

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    week = [
        e for e in events
        if week_start <= e.time.start.astimezone(tz).date() < week_end
    ]
    missing_week = missing_usuals_this_week(usuals=usuals, week_events=week)

    profile = await store.profile.read() or {}
    week_start_iso = week_start.isoformat()
    dismissed_this_week = profile.get("dismissed_missing_week") == week_start_iso
    missing_week_view = (
        []
        if dismissed_this_week
        else [
            _decorate_missing_group(g, usuals, events_by_id, people_by_id, tz, today)
            for g in missing_week
        ]
    )

    return {
        "date": today.isoformat(),
        "today": [_view(e) for e in todays],
        "tomorrow": [_view(e) for e in tomorrows],
        "missing_usuals": [
            {
                "usual_id": m.usual.usual_id,
                "display_summary": m.usual.display_summary,
                "person_id": m.usual.person_id,
                "hour_band": m.usual.hour_band,
            }
            for m in missing
        ],
        "missing_usuals_week": missing_week_view,
        "missing_usuals_week_dismissed": dismissed_this_week,
        "week_load": _week_load(week),
    }


@router.post("/missing-week/dismiss")
async def dismiss_missing_week(store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    """Hide 'usuals missing this week' until next Monday.

    The user is saying this week is intentionally different, not that the
    usuals are wrong forever. Next week the list comes back.
    """
    settings = get_settings()
    tz = ZoneInfo(settings.calendar_tz)
    today = datetime.now(tz).date()
    week_start = today - timedelta(days=today.weekday())
    profile = await store.profile.read() or {}
    profile["dismissed_missing_week"] = week_start.isoformat()
    await store.profile.write(profile)
    return {"status": "dismissed", "week_start": week_start.isoformat()}


def _decorate_missing_group(
    group: Any,
    all_usuals: list[Any],
    events_by_id: dict[str, CachedEvent],
    people_by_id: dict[str, Any],
    tz: ZoneInfo,
    today: Any,
) -> dict[str, Any]:
    """Coarse category-level missing entry with typical time + person context."""
    starts: list[int] = []
    durations: list[int] = []
    usuals_by_id = {u.usual_id: u for u in all_usuals}
    for uid in group.representative_usual_ids:
        u = usuals_by_id.get(uid)
        if not u:
            continue
        for src_uid in u.source_event_uids:
            ev = events_by_id.get(src_uid)
            if not ev or ev.time.all_day:
                continue
            s_local = ev.time.start.astimezone(tz)
            e_local = ev.time.end.astimezone(tz)
            starts.append(s_local.hour * 60 + s_local.minute)
            durations.append(max(15, int((e_local - s_local).total_seconds() // 60)))
    if starts:
        start_min = int(median(starts))
        dur_min = int(median(durations))
        typical_start = _fmt_hm(start_min)
        typical_end = _fmt_hm(start_min + dur_min)
    else:
        typical_start = None
        typical_end = None

    person = people_by_id.get(group.person_id)
    week_start = today - timedelta(days=today.weekday())
    day_this_week = week_start + timedelta(days=int(group.weekday))
    return {
        "group_id": f"{int(group.weekday)}:{group.person_id}:{group.category.value}",
        "weekday": int(group.weekday),
        "date": day_this_week.isoformat(),
        "category": group.category.value,
        "category_label": group.category.label,
        "person_id": group.person_id,
        "person_name": person.display_name if person else None,
        "person_relation": person.relation.value if person else None,
        "typical_start": typical_start,
        "typical_end": typical_end,
    }


def _fmt_hm(total_minutes: int) -> str:
    total_minutes = max(0, min(23 * 60 + 59, total_minutes))
    hour_24 = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "am" if hour_24 < 12 else "pm"
    hour_12 = hour_24 % 12 or 12
    if minute == 0:
        return f"{hour_12}{suffix}"
    return f"{hour_12}:{minute:02d}{suffix}"


def _week_load(week_events: list[Any]) -> list[dict[str, Any]]:
    """Weekly percentage load, rolled up to LoadBucket.

    We group at the coarse level (School, Sports, Medical, Work, ...) so the
    bar isn't fractured into eleven 3% slivers. The finer ActivityType is
    still used by the missing-usuals view where the specificity matters.
    """
    counts: Counter[LoadBucket] = Counter()
    for e in week_events:
        activity = e.activity_type or ActivityType.OTHER
        counts[activity.load_bucket] += 1
    total = sum(counts.values())
    if total == 0:
        return []
    return [
        {
            "bucket": bucket.value,
            "label": bucket.label,
            "color": bucket.color,
            "count": n,
            "percent": round((n / total) * 100),
        }
        for bucket, n in counts.most_common()
    ]


@router.get("/summary")
async def summary(store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    text = await get_daily_summary(store)
    return {"summary": text}
