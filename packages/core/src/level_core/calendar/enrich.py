"""Enrichment step after `refresh_agenda`:

  1. Classify any unclassified events via ActivityAgent (batched, cached forever).
  2. Resolve matched_person_ids from `care_people` aliases (deterministic).
  3. Rematch active reminders on structured equality (person_id, activity_type).

All three are safe to run whenever `fingerprint_changed=True` (or after
add-a-reminder / add-a-person chat turns).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from level_core.agents.activity import run as activity_run
from level_core.calendar.person_match import person_matches
from level_core.observability import get_logger
from level_core.schemas import ActivityType, CachedEvent, Reminder
from level_core.storage.base import UserStore

logger = get_logger(__name__)

# Maximum concurrent activity-classification LLM calls per refresh.
# Sized so a fresh 500-event calendar finishes cold classification in
# ~2-3s instead of ~5s (v1: 4-way, ~5s). We can go this wide because
# ActivityAgent is Gemma-eligible (see `_GEMMA_ELIGIBLE` in
# `agents/base.py`) - if AI Studio 429s, Tier-3 Gemma keeps the wave
# moving without eating additional Gemini quota. Per-user hourly cap
# (60/hr) still bounds total spend.
CLASSIFY_CONCURRENCY = 8


@dataclass
class EnrichResult:
    classified: int
    people_matched: int
    reminders_matched: int


async def enrich_agenda(store: UserStore) -> EnrichResult:
    """Classify + person-match + reminder-match in one in-memory sweep.

    Previously this made THREE `agenda.list()` reads (one before classify,
    one before person-match, one before reminder-match) plus two full
    `upsert_many` passes on the whole collection - at Firestore scale
    that's ~3N doc reads on every profile refresh. Now we read once,
    keep the in-memory view up to date as we mutate, and only write the
    events that actually changed.
    """
    events = await store.agenda.list()
    all_people = await store.people.list()
    reminders = [r for r in await store.reminders.list() if r.status == "active"]

    classified, events = await _classify_unseen(store, events)

    # A person the user marked "not_me" must NOT keep matching events -
    # their name is exactly what they rejected. Include self even when
    # status is "proposed" so bootstrap works before the user confirms.
    people = [p for p in all_people if p.status != "not_me"]
    self_id = next((p.person_id for p in people if p.is_self), None)

    people_updates: list[CachedEvent] = []
    for i, event in enumerate(events):
        matches: list[str] = [p.person_id for p in people if person_matches(event, p)]
        if not matches and self_id:
            matches = [self_id]
        sorted_matches = sorted(set(matches))
        if sorted_matches != event.matched_person_ids:
            new_event = event.model_copy(update={"matched_person_ids": sorted_matches})
            events[i] = new_event
            people_updates.append(new_event)
    if people_updates:
        await store.agenda.upsert_many(people_updates)
    people_matched = len(people_updates)

    reminder_updates: list[CachedEvent] = []
    for event in events:
        matched: list[str] = []
        for r in reminders:
            if _reminder_matches(r, event):
                matched.append(r.reminder_id)
        matched.sort()
        if matched != event.matched_reminder_ids:
            reminder_updates.append(
                event.model_copy(update={"matched_reminder_ids": matched})
            )
    if reminder_updates:
        await store.agenda.upsert_many(reminder_updates)
    reminders_matched = len(reminder_updates)

    logger.info(
        "calendar.enrich.done",
        user=store.user_id,
        classified=classified,
        people_matched=people_matched,
        reminders_matched=reminders_matched,
    )
    return EnrichResult(
        classified=classified,
        people_matched=people_matched,
        reminders_matched=reminders_matched,
    )


async def _classify_unseen(
    store: UserStore, events: list[CachedEvent]
) -> tuple[int, list[CachedEvent]]:
    """Classify unseen events and return (count, updated in-memory events).

    Returning the updated list lets the outer `enrich_agenda` skip a
    redundant `agenda.list()` after this pass.
    """
    unseen = [e for e in events if e.activity_type is None and e.summary]
    if not unseen:
        return 0, events

    ai_by_id: dict[str, ActivityType] = {}
    ai_span_by_id: dict[str, str] = {}
    ai_available = True
    batches = [unseen[i : i + 25] for i in range(0, len(unseen), 25)]
    semaphore = asyncio.Semaphore(CLASSIFY_CONCURRENCY)

    async def _run_one(batch: list[CachedEvent]) -> Any:
        payload = [{"event_id": e.event_id, "summary": e.summary} for e in batch]
        async with semaphore:
            return await activity_run(store=store, events=payload)

    try:
        results = await asyncio.gather(
            *(_run_one(b) for b in batches), return_exceptions=True
        )
        for res in results:
            if isinstance(res, Exception):
                logger.warning(
                    "calendar.classify.batch_failed", error=str(res)[:200]
                )
                continue
            if not res or not res.value:
                continue
            for row in res.value.classifications:  # type: ignore[union-attr]
                ai_by_id[row.event_id] = row.activity_type
                ai_span_by_id[row.event_id] = row.source_span
    except Exception as exc:  # noqa: BLE001 - AI is best-effort
        ai_available = False
        logger.warning("calendar.classify.ai_unavailable", error=str(exc)[:200])

    classified: list[CachedEvent] = []
    updated_by_id: dict[str, CachedEvent] = {}
    for e in unseen:
        new_type = ai_by_id.get(e.event_id)
        span = ai_span_by_id.get(e.event_id, "")
        resolved: ActivityType | None
        if new_type and (not span or span in e.summary):
            resolved = new_type
        else:
            resolved = heuristic_activity(e.summary)
        # If the AI was reachable but chose not to classify this event, accept
        # OTHER as a floor. If the AI was unreachable, leave activity_type None
        # so the next enrich pass gets another chance.
        if resolved is None and ai_available:
            resolved = ActivityType.OTHER
        if resolved is None:
            continue
        new_event = e.model_copy(
            update={
                "activity_type": resolved,
                "classified_at": datetime.utcnow(),
            }
        )
        classified.append(new_event)
        updated_by_id[new_event.event_id] = new_event
    if classified:
        await store.agenda.upsert_many(classified)
    updated_events = [updated_by_id.get(e.event_id, e) for e in events]
    return len(classified), updated_events


async def reclassify_all(store: UserStore) -> int:
    """Reset every cached event's classification and re-run enrichment.

    Useful after tweaking heuristics or connecting a smarter classifier.
    """
    events = await store.agenda.list()
    reset_rows = [
        e.model_copy(update={"activity_type": None, "classified_at": None})
        for e in events
        if e.activity_type is not None
    ]
    await store.agenda.upsert_many(reset_rows)
    await enrich_agenda(store)
    return len(reset_rows)


# Tiny, deliberately-narrow floor of *unambiguous* signals.
#
# Philosophy: Gemini Flash is the classifier — it handles nuance ("Chart
# review block" is work, not medical; "Papa neurology f/u" is medical;
# "Working lunch" is work). This map only exists to (a) give instant
# results before the first AI batch returns and (b) keep a floor when the
# AI is briefly unavailable. Every entry here should be a phrase where a
# human would *always* agree, no matter the context. Prefer AI over
# guessing.
OBVIOUS_SIGNALS: tuple[tuple[ActivityType, tuple[str, ...]], ...] = (
    (ActivityType.SPORTS_SOCCER, ("soccer",)),
    (ActivityType.SPORTS_BASKETBALL, ("basketball",)),
    (ActivityType.SPORTS_SWIM, ("swim lesson", "swim practice", "swim meet", "swim")),
    (ActivityType.SCHOOL_PICKUP, ("pickup", "pick-up", "aftercare")),
    (ActivityType.SCHOOL_DROPOFF, ("dropoff", "drop-off", "(drop)")),
    (ActivityType.MEDICAL_THERAPY, ("therapy", "therapist")),
    (ActivityType.MEDICAL_APPT, ("dentist", "dental", "pediatric", "doctor visit", "dr appt", "appt")),
    (ActivityType.WORK, ("1:1", "standup", "all hands", "all-hands", "sprint review", "sprint planning")),
    (ActivityType.COMMUTE, ("commute",)),
    (ActivityType.PERSONAL, ("grocery", "meal prep", "laundry")),
)


def heuristic_activity(summary: str) -> ActivityType | None:
    """Instant, high-precision floor for unambiguous titles.

    Returns None on anything else so the AI classifier can do the real work
    (nuanced titles like "Chart review block" or "Papa \u2014 neurology f/u"
    are exactly what Gemini Flash is good at).
    """
    if not summary:
        return None
    lower = summary.lower().strip()
    if lower == "work" or lower.startswith("work "):
        return ActivityType.WORK
    for activity, phrases in OBVIOUS_SIGNALS:
        if any(phrase in lower for phrase in phrases):
            return activity
    return None


def _heuristic_activity(summary: str) -> ActivityType | None:
    return heuristic_activity(summary)


def _reminder_matches(reminder: Reminder, event: CachedEvent) -> bool:
    # OTHER is the "couldn't classify" bucket for leftover calendar titles
    # (Lunch, errands, …). A reminder that landed there has no real join key,
    # so attaching it would flag every leftover event.
    if reminder.match.activity_type == ActivityType.OTHER:
        return False
    if event.activity_type != reminder.match.activity_type:
        return False
    if reminder.match.person_id and reminder.match.person_id not in event.matched_person_ids:
        return False
    return True
