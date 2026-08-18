"""Book an event in Google Calendar with `origin=level` tag."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

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
    from level_core.calendar.google_client import build_calendar_client

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
