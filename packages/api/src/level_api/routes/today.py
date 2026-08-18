"""Today page: greeting, events, missing usuals, day summary for TTS."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from level_core.calendar.sync import refresh_agenda
from level_core.calendar.usuals import missing_usuals_today
from level_core.config import get_settings
from level_core.schemas import ActivityType
from level_core.storage.base import UserStore
from level_core.voice.summary import get_daily_summary

from level_api.deps import get_user_store

router = APIRouter()


ACTIVITY_LABEL: dict[str, str] = {
    ActivityType.SPORTS_SOCCER: "Soccer",
    ActivityType.SPORTS_BASKETBALL: "Basketball",
    ActivityType.SPORTS_SWIM: "Swim",
    ActivityType.SPORTS_OTHER: "Sports",
    ActivityType.SCHOOL_PICKUP: "School pickup",
    ActivityType.SCHOOL_DROPOFF: "School dropoff",
    ActivityType.SCHOOL_EVENT: "School",
    ActivityType.MEDICAL_APPT: "Medical",
    ActivityType.MEDICAL_THERAPY: "Therapy",
    ActivityType.WORK: "Work",
    ActivityType.FAMILY: "Family",
    ActivityType.COMMUTE: "Commute",
    ActivityType.PERSONAL: "Personal",
    ActivityType.OTHER: "Other",
}


ACTIVITY_COLOR: dict[str, str] = {
    ActivityType.SPORTS_SOCCER: "#3aa38a",
    ActivityType.SPORTS_BASKETBALL: "#c4843a",
    ActivityType.SPORTS_SWIM: "#3a95c4",
    ActivityType.SPORTS_OTHER: "#3aa38a",
    ActivityType.SCHOOL_PICKUP: "#c4843a",
    ActivityType.SCHOOL_DROPOFF: "#c4843a",
    ActivityType.SCHOOL_EVENT: "#c4843a",
    ActivityType.MEDICAL_APPT: "#c44d4d",
    ActivityType.MEDICAL_THERAPY: "#a06ac4",
    ActivityType.WORK: "#5a7380",
    ActivityType.FAMILY: "#c47a3a",
    ActivityType.COMMUTE: "#8aa4b0",
    ActivityType.PERSONAL: "#2d9f8a",
    ActivityType.OTHER: "#8aa4b0",
}


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
            from level_core.calendar.enrich import enrich_agenda

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
        "week_load": _week_load(week),
    }


def _week_load(week_events: list[Any]) -> list[dict[str, Any]]:
    """Weekly percentage load per activity type — for the RoleLoadBar."""
    counts: Counter[str] = Counter()
    for e in week_events:
        key = str(e.activity_type) if e.activity_type else ActivityType.OTHER.value
        counts[key] += 1
    total = sum(counts.values())
    if total == 0:
        return []
    rows: list[dict[str, Any]] = []
    for activity, n in counts.most_common():
        rows.append(
            {
                "activity_type": activity,
                "label": ACTIVITY_LABEL.get(activity, activity),
                "color": ACTIVITY_COLOR.get(activity, "#8aa4b0"),
                "count": n,
                "percent": round((n / total) * 100),
            }
        )
    return rows


@router.get("/summary")
async def summary(store: UserStore = Depends(get_user_store)) -> dict[str, str]:
    text = await get_daily_summary(store)
    return {"summary": text}
