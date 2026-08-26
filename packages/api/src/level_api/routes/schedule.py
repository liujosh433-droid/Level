"""Find + book a time, priorities- and usuals-aware."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from level_core.schedule.book import book_event
from level_core.schedule.slots import find_candidate_slots, score_slots
from level_core.schemas import ActivityType
from level_core.storage.base import UserStore
from level_core.tz import tz_for_store
from pydantic import BaseModel, Field

from level_api.deps import get_user_store

router = APIRouter()

# In-process pending-booking table. Confirmation tokens expire after
# PENDING_BOOKING_TTL to bound memory and to prevent a token issued
# hours ago from being redeemed against stale slot data.
_pending_bookings: dict[str, dict[str, Any]] = {}
PENDING_BOOKING_TTL = timedelta(minutes=10)


def _prune_pending(now: datetime) -> None:
    """Drop expired tokens. O(N) but N is per-process, capped by TTL."""
    expired = [
        t for t, p in _pending_bookings.items()
        if isinstance(p, dict) and (p.get("_expires_at") or now) < now
    ]
    for t in expired:
        _pending_bookings.pop(t, None)


class FindBody(BaseModel):
    activity_type: ActivityType
    duration_minutes: int = Field(default=60, ge=15, le=480)
    within_days: int = Field(default=7, ge=1, le=30)
    summary_hint: str = ""


class BookBody(BaseModel):
    confirmation_token: str
    summary: str
    start_iso: str
    end_iso: str
    activity_type: ActivityType


@router.post("/find")
async def find(body: FindBody, store: UserStore = Depends(get_user_store)) -> dict[str, Any]:
    events = await store.agenda.list()
    priorities = await store.priorities.list()
    usuals = await store.usuals.list()

    now = datetime.now(UTC)
    tz = await tz_for_store(store)
    candidates = find_candidate_slots(
        events=events,
        window_days=body.within_days,
        duration_minutes=body.duration_minutes,
        starts_at=now,
        tz=tz,
    )
    events_by_id = {e.event_id: e for e in events}
    ranked = score_slots(
        candidates,
        activity_type=body.activity_type,
        priorities=priorities,
        usuals=usuals,
        events_by_id=events_by_id,
        tz=tz,
    )
    top = ranked[:3]
    token = secrets.token_urlsafe(24)
    _prune_pending(now)
    _pending_bookings[token] = {
        "activity_type": body.activity_type,
        "summary_hint": body.summary_hint,
        "_expires_at": now + PENDING_BOOKING_TTL,
        "top": [
            {
                "start_iso": s.start.isoformat(),
                "end_iso": s.end.isoformat(),
                "score": s.score,
                "conflicts": s.conflicts,
                "aligned_priorities": s.aligned_priorities,
                "aligned_usuals": s.aligned_usuals,
                "local_label": s.local_label,
            }
            for s in top
        ],
    }
    return {"confirmation_token": token, "slots": _pending_bookings[token]["top"]}


@router.post("/book")
async def book(
    body: BookBody,
    store: UserStore = Depends(get_user_store),
    x_idempotency_key: str | None = Header(default=None),
) -> dict[str, Any]:
    _prune_pending(datetime.now(UTC))
    pending = _pending_bookings.pop(body.confirmation_token, None)
    if not pending:
        raise HTTPException(status_code=400, detail="unknown_or_expired_confirmation_token")

    start = datetime.fromisoformat(body.start_iso)
    end = datetime.fromisoformat(body.end_iso)
    booked = await book_event(
        store,
        summary=body.summary,
        start=start,
        end=end,
        reason=f"chat:{body.activity_type}",
    )
    return {"event_id": booked.event_id, "html_link": booked.html_link, "origin": "level"}
