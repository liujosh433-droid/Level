"""Find + book a time, priorities- and usuals-aware.

Confirmation-token lifecycle
----------------------------

``POST /schedule/find`` mints a ``confirmation_token`` and records the
top-3 slot options in TWO places:

  1. ``_pending_bookings`` — process-local dict; fast path for the
     common case where find and book land on the same instance within
     a few seconds.
  2. ``profile["pending_schedule_find"]`` — Firestore, TTL-bound;
     authoritative so a multi-instance Cloud Run deployment doesn't
     lose the token on request routing / instance replacement.

``POST /schedule/book`` accepts an ``(start_iso, end_iso)`` pair and
validates it against the persisted top-3. A client that changes those
times to something the server never proposed is rejected — booking
must always come from the ranked options we actually showed the user.
The idempotency header, when present, guards against duplicate calendar
writes during retries.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from level_core.observability import get_logger
from level_core.schedule.book import book_event
from level_core.schedule.slots import find_candidate_slots, score_slots
from level_core.schemas import ActivityType
from level_core.storage.base import UserStore
from level_core.tz import tz_for_store
from pydantic import BaseModel, Field

from level_api.deps import get_user_store

router = APIRouter()
logger = get_logger(__name__)

# In-process pending-booking table. Fast path; the profile-persisted
# copy is authoritative across instance restarts / multi-instance
# routing. Confirmation tokens expire after PENDING_BOOKING_TTL to
# bound memory and prevent a token issued hours ago from being
# redeemed against stale slot data.
_pending_bookings: dict[str, dict[str, Any]] = {}
_booked_idempotency: dict[str, float] = {}
PENDING_BOOKING_TTL = timedelta(minutes=10)
IDEMPOTENCY_WINDOW_S = 600

PENDING_FIND_KEY = "pending_schedule_find"


def _prune_pending(now: datetime) -> None:
    """Drop expired in-memory tokens. O(N) but N is per-process, capped by TTL."""
    expired = [
        t for t, p in _pending_bookings.items()
        if isinstance(p, dict) and (p.get("_expires_at") or now) < now
    ]
    for t in expired:
        _pending_bookings.pop(t, None)


def _prune_idempotency(now_s: float) -> None:
    for k, t in list(_booked_idempotency.items()):
        if now_s - t > IDEMPOTENCY_WINDOW_S:
            _booked_idempotency.pop(k, None)


def _slot_matches(candidate: dict[str, Any], start_iso: str, end_iso: str) -> bool:
    """Compare ISO timestamps by their parsed UTC value.

    Accepts minor formatting differences (``Z`` vs ``+00:00``, whitespace)
    so a well-formed client that reformats what we returned still lands
    on the same slot.
    """
    try:
        want_start = datetime.fromisoformat(start_iso.strip().replace("Z", "+00:00"))
        want_end = datetime.fromisoformat(end_iso.strip().replace("Z", "+00:00"))
        got_start = datetime.fromisoformat(candidate["start_iso"].replace("Z", "+00:00"))
        got_end = datetime.fromisoformat(candidate["end_iso"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    return want_start == got_start and want_end == got_end


def _load_persisted_pending(
    profile: dict[str, Any], token: str, now: datetime
) -> dict[str, Any] | None:
    """Return the persisted pending-find record for `token` if still valid."""
    stored = profile.get(PENDING_FIND_KEY)
    if not isinstance(stored, dict):
        return None
    if stored.get("confirmation_token") != token:
        return None
    expires_at = stored.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            expiry = None
        if expiry is not None and now >= expiry:
            return None
    return stored


class FindBody(BaseModel):
    activity_type: ActivityType
    duration_minutes: int = Field(default=60, ge=15, le=480)
    within_days: int = Field(default=7, ge=1, le=30)
    summary_hint: str = Field(default="", max_length=200)


class BookBody(BaseModel):
    confirmation_token: str = Field(min_length=8, max_length=128)
    summary: str = Field(min_length=1, max_length=200)
    start_iso: str = Field(min_length=1, max_length=64)
    end_iso: str = Field(min_length=1, max_length=64)
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
    top_records = [
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
    ]
    expires_at = now + PENDING_BOOKING_TTL
    _pending_bookings[token] = {
        "activity_type": body.activity_type,
        "summary_hint": body.summary_hint,
        "_expires_at": expires_at,
        "top": top_records,
    }
    # Persist to the profile so a book on a different Cloud Run
    # instance can still validate the slot. Keep only the fields the
    # book endpoint needs; the response can also be reconstructed from
    # here for reload/inspect scenarios.
    profile = dict(await store.profile.read() or {})
    profile[PENDING_FIND_KEY] = {
        "confirmation_token": token,
        "activity_type": str(body.activity_type),
        "top": top_records,
        "expires_at": expires_at.isoformat(),
    }
    await store.profile.write(profile)
    return {"confirmation_token": token, "slots": top_records}


@router.post("/book")
async def book(
    body: BookBody,
    store: UserStore = Depends(get_user_store),
    x_idempotency_key: str | None = Header(default=None),
) -> dict[str, Any]:
    now = datetime.now(UTC)
    _prune_pending(now)
    now_s = time.time()
    _prune_idempotency(now_s)

    if x_idempotency_key and x_idempotency_key in _booked_idempotency:
        # Same key already succeeded; refuse the duplicate rather than
        # write a second calendar event. Clients should retry with a
        # fresh key.
        raise HTTPException(status_code=409, detail="duplicate_book")

    profile = dict(await store.profile.read() or {})
    persisted = _load_persisted_pending(profile, body.confirmation_token, now)
    memory_pending = _pending_bookings.get(body.confirmation_token)
    if not memory_pending and not persisted:
        raise HTTPException(
            status_code=400, detail="unknown_or_expired_confirmation_token"
        )

    top_records: list[dict[str, Any]] = (
        list(memory_pending.get("top") or []) if memory_pending else []
    )
    if not top_records and persisted:
        top_records = list(persisted.get("top") or [])
    if not any(
        _slot_matches(rec, body.start_iso, body.end_iso) for rec in top_records
    ):
        # A modified client tried to book a time we never proposed —
        # reject rather than trust the client's ISO timestamps. This
        # is the guardrail that keeps /book from becoming an arbitrary
        # calendar-write endpoint.
        logger.warning(
            "schedule.book.slot_mismatch",
            user=store.user_id,
            token_prefix=body.confirmation_token[:8],
            supplied_start=body.start_iso[:32],
        )
        raise HTTPException(status_code=400, detail="slot_not_offered")

    try:
        start = datetime.fromisoformat(body.start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(body.end_iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad_iso_timestamp") from exc
    if end <= start:
        raise HTTPException(status_code=400, detail="end_before_start")

    booked = await book_event(
        store,
        summary=body.summary,
        start=start,
        end=end,
        reason=f"chat:{body.activity_type}",
    )

    # Success — drop pending state from both stores and record the
    # idempotency key so a retry is a fast 409 rather than a duplicate
    # calendar entry.
    _pending_bookings.pop(body.confirmation_token, None)
    if persisted:
        profile.pop(PENDING_FIND_KEY, None)
        await store.profile.write(profile)
    if x_idempotency_key:
        _booked_idempotency[x_idempotency_key] = now_s

    return {"event_id": booked.event_id, "html_link": booked.html_link, "origin": "level"}
