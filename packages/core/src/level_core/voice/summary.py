"""Cache-aware wrapper around SummaryAgent for 'Hear my day'."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from level_core.agents.summary import run as summary_run
from level_core.calendar.usuals import missing_usuals_today
from level_core.storage.base import UserStore
from level_core.tz import tz_for_store


async def get_daily_summary(store: UserStore) -> str:
    tz = await tz_for_store(store)
    today = datetime.now(tz).date().isoformat()

    cache = await store.calendar_sync.read() or {}
    summary_cache: dict[str, Any] = cache.get("summary_cache", {})
    fingerprint = cache.get("events_fingerprint", "")

    cached = summary_cache.get(today)
    if cached and cached.get("fingerprint") == fingerprint:
        return cached["text"]

    events = await store.agenda.list()
    todays = [
        e
        for e in events
        if e.time.start.astimezone(tz).date().isoformat() == today
    ]
    todays.sort(key=lambda e: e.time.start)
    event_lines = [
        f"{e.time.start.astimezone(tz).strftime('%-I:%M %p')} {e.summary}"
        for e in todays
    ]

    usuals = await store.usuals.list()
    missing = missing_usuals_today(usuals=usuals, todays_events=todays, tz=tz)
    missing_lines = [f"{m.usual.display_summary} ({m.expected_hour_band.value})" for m in missing]

    reminder_lines: list[str] = []
    reminders_by_id = {
        r.reminder_id: r for r in await store.reminders.list() if r.status == "active"
    }
    for e in todays:
        for rid in e.matched_reminder_ids:
            r = reminders_by_id.get(rid)
            if r:
                reminder_lines.append(f"{r.text} on {e.summary}")

    result = await summary_run(
        store=store,
        date_label=today,
        event_lines=event_lines or ["Nothing on the calendar."],
        missing_usual_lines=missing_lines,
        reminder_lines=reminder_lines,
    )
    text = result.value.summary if result.value else "Today looks quiet."  # type: ignore[union-attr]

    summary_cache[today] = {"text": text, "fingerprint": fingerprint}
    cache["summary_cache"] = summary_cache
    await store.calendar_sync.write(cache)
    return text
