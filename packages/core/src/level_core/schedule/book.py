"""Book an event in Google Calendar with `origin=level` tag."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from level_core.calendar.google_client import build_calendar_client
from level_core.storage.base import UserStore


@dataclass
class BookedEvent:
    event_id: str
    html_link: str


async def book_event(
    store: UserStore,
    *,
    summary: str,
    start: datetime,
    end: datetime,
    reason: str,
    calendar_id: str = "primary",
    location: str | None = None,
) -> BookedEvent:
    service = await build_calendar_client(store)
    body = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "extendedProperties": {
            "private": {
                "origin": "level",
                "level_reason": reason[:200],
            }
        },
    }
    if location:
        body["location"] = location

    inserted = await asyncio.to_thread(
        service.events().insert(calendarId=calendar_id, body=body).execute
    )
    return BookedEvent(event_id=inserted["id"], html_link=inserted.get("htmlLink", ""))


async def move_event(
    store: UserStore,
    *,
    event_id: str,
    start: datetime,
    end: datetime,
    calendar_id: str = "primary",
) -> BookedEvent:
    """Patch an existing event's start/end. Title and other fields stay put."""
    service = await build_calendar_client(store)
    body = {
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    updated = await asyncio.to_thread(
        service.events()
        .patch(calendarId=calendar_id, eventId=event_id, body=body)
        .execute
    )
    return BookedEvent(event_id=updated["id"], html_link=updated.get("htmlLink", ""))


async def delete_event(
    store: UserStore,
    *,
    event_id: str,
    calendar_id: str = "primary",
) -> None:
    """Delete an event from Google Calendar. 404 is treated as already gone."""
    service = await build_calendar_client(store)
    try:
        await asyncio.to_thread(
            service.events().delete(calendarId=calendar_id, eventId=event_id).execute
        )
    except Exception as err:
        status = getattr(err, "status_code", None) or getattr(err, "resp", None)
        code = getattr(status, "status", None) if status is not None and not isinstance(status, int) else status
        if code == 404 or "404" in str(err):
            return
        raise
