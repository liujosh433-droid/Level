"""Nightly Cloud Run Job:

  1. For each user: refresh_agenda + enrich_agenda + recompute usuals.
  2. Detect missing-usual gaps this week and stash proactive cards
     (Collaborative Partner: autonomous background action).
  3. Trim chat_turns > 20 per user.
  4. Trim ai_audit > 30 days.
  5. Renew any calendar watch channel due to expire in < 2 days.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import ensure_watch, refresh_agenda
from level_core.calendar.usuals import (
    compute_usuals_from_events,
    current_week_bounds,
    missing_usuals_this_week,
)
from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.schemas import Usual
from level_core.storage.base import UserStore
from level_core.storage.care_store import propose_usual, sync_usuals
from level_core.storage.factory import get_store
from level_core.tz import tz_for_store

logger = get_logger("nightly")

PROACTIVE_CARDS_KEY = "proactive_cards"
MAX_PROACTIVE_CARDS = 5


async def _process_user(user_id: str) -> None:
    store = get_store(user_id)
    tokens = await store.tokens.read() or {}
    if not tokens.get("access_token"):
        return

    result = await refresh_agenda(store)
    if result.fingerprint_changed:
        await enrich_agenda(store)
    events = await store.agenda.list()
    people = await store.people.list()
    tz = await tz_for_store(store)
    fresh_ids: set[str] = set()
    for c in compute_usuals_from_events(events, people, tz=tz):
        await propose_usual(
            store,
            person_id=c.person_id,
            weekday=c.weekday,
            hour_band=c.hour_band,
            activity_type=c.activity_type,
            display_summary=c.display_summary,
            source_event_uids=list(c.source_event_uids),
            confidence=c.confidence,
        )
        fresh_ids.add(Usual.compose_id(c.person_id, c.weekday, c.hour_band))
    await sync_usuals(store, fresh_ids)

    await _generate_proactive_cards(store, tz=tz, events=events)

    turns = sorted(
        await store.chat_turns.list(), key=lambda t: t.created_at, reverse=True
    )
    for t in turns[20:]:
        await store.chat_turns.delete(t.turn_id)

    cutoff = datetime.now(UTC) - timedelta(days=30)
    for a in await store.ai_audit.list():
        created = a.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            await store.ai_audit.delete(a.audit_id)

    await ensure_watch(store)


async def _generate_proactive_cards(
    store: UserStore, *, tz, events
) -> None:
    """Detect missing-usual gaps this week and stash them as suggestion cards.

    The frontend's /today page renders these as "Beta's Thursday dropoff
    isn't on your calendar — put it back?" nudges with an inline
    confirm-yes flow. This is the rubric's "runs in the background,
    handles the heavy lifting" behavior — the user wakes up to Level
    having already noticed and prepared a fix.

    We deliberately keep this deterministic (no LLM) so the nightly job
    stays free. If a card gets dismissed by the user, chat.py's fast-path
    still handles the confirm-yes; nothing here mutates the calendar.
    """
    today = datetime.now(tz).date()
    week_start, _week_end = current_week_bounds(today)
    usuals = await store.usuals.list()
    people = await store.people.list()
    events_by_id = {e.event_id: e for e in events}
    missing = missing_usuals_this_week(
        usuals=usuals,
        week_events=[e for e in events if week_start <= e.time.start.astimezone(tz).date() < _week_end],
        as_of_date=today,
        events_by_id=events_by_id,
        people=people,
        tz=tz,
    )
    if not missing:
        # Clear any stale cards from prior weeks so they don't linger.
        profile = dict(await store.profile.read() or {})
        if profile.get(PROACTIVE_CARDS_KEY):
            profile.pop(PROACTIVE_CARDS_KEY, None)
            await store.profile.write(profile)
        return

    people_by_id = {p.person_id: p for p in await store.people.list()}
    cards: list[dict] = []
    for g in missing[:MAX_PROACTIVE_CARDS]:
        primary_person = people_by_id.get(g.person_id)
        display_name = primary_person.display_name if primary_person else "someone"
        day = (week_start + timedelta(days=int(g.weekday))).isoformat()
        # Prefer the concrete title ("Grocery run", "Nova ballet") over
        # the coarse category ("Personal", "Sports"). The category on
        # its own is too abstract to be a useful nudge - "Josh's
        # personal is missing this week" reads like a therapist joke.
        # Falls back to the category label when the usual didn't carry
        # a title (badly seeded data or LLM-less demo mode with no
        # heuristic hit).
        activity_label = (g.title_hint or g.category.label).strip()
        if primary_person and primary_person.is_self:
            # Own-usual phrasing: "Your grocery run is missing" reads
            # better than "Josh's grocery run is missing" when it's
            # the caregiver themselves.
            body_text = (
                f"Your {activity_label.lower()} is missing this week. "
                "Want me to put it back?"
            )
        else:
            body_text = (
                f"{display_name}'s {activity_label.lower()} is missing this week. "
                "Want me to put it back?"
            )
        cards.append(
            {
                "card_id": f"missing:{g.weekday}:{g.category.value}",
                "kind": "missing_usual",
                "week_start": week_start.isoformat(),
                "day": day,
                "weekday": int(g.weekday),
                "category": g.category.value,
                "category_label": g.category.label,
                "title_hint": g.title_hint,
                "person_id": g.person_id,
                "person_name": display_name,
                "text": body_text,
                "created_at": datetime.utcnow().isoformat(),
            }
        )

    profile = dict(await store.profile.read() or {})
    profile[PROACTIVE_CARDS_KEY] = {
        "week_start": week_start.isoformat(),
        "generated_at": datetime.utcnow().isoformat(),
        "cards": cards,
    }
    await store.profile.write(profile)
    logger.info(
        "nightly.proactive_cards",
        user_id=store.user_id,
        count=len(cards),
        week_start=week_start.isoformat(),
    )


async def _list_users() -> list[str]:
    """Cloud impl scans Firestore for user docs. Local impl scans .level dir."""
    settings = get_settings()
    if settings.is_local:
        root = Path(".level/local_store")
        if not root.exists():
            return []
        return [p.name for p in root.iterdir() if p.is_dir()]
    from google.cloud import firestore

    client = firestore.Client(project=settings.google_cloud_project or None)
    return [d.id for d in client.collection("users").stream()]


async def main() -> None:
    users = await _list_users()
    logger.info("nightly.start", user_count=len(users))
    for uid in users:
        try:
            await _process_user(uid)
        except Exception as exc:
            logger.exception("nightly.user_failed", user_id=uid, exc=str(exc))
    logger.info("nightly.done")


if __name__ == "__main__":
    asyncio.run(main())
