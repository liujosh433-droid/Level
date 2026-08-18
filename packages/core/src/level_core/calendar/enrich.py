"""Enrichment step after `refresh_agenda`:

  1. Classify any unclassified events via ActivityAgent (batched, cached forever).
  2. Resolve matched_person_ids from `care_people` aliases (deterministic).
  3. Rematch active reminders on structured equality (person_id, activity_type).

All three are safe to run whenever `fingerprint_changed=True` (or after
add-a-reminder / add-a-person chat turns).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from level_core.observability import get_logger
from level_core.schemas import ActivityType, CachedEvent, Reminder
from level_core.storage.base import UserStore

logger = get_logger(__name__)


@dataclass
class EnrichResult:
    classified: int
    people_matched: int
    reminders_matched: int


async def enrich_agenda(store: UserStore) -> EnrichResult:
    events = await store.agenda.list()
    people = await store.people.list()
    reminders = [r for r in await store.reminders.list() if r.status == "active"]

    classified = await _classify_unseen(store, events)
    events = await store.agenda.list()

    people_matched = 0
    for event in events:
        matches: list[str] = []
        lower = event.summary.lower()
        for p in people:
            if any(name.lower() in lower for name in [p.display_name, *p.aliases]) or any(t.lower() in {n.lower() for n in [p.display_name, *p.aliases]} for t in event.attendee_tokens):
                matches.append(p.person_id)
        sorted_matches = sorted(set(matches))
        if sorted_matches != event.matched_person_ids:
            await store.agenda.upsert(
                event.model_copy(update={"matched_person_ids": sorted_matches})
            )
            people_matched += 1

    events = await store.agenda.list()

    reminders_matched = 0
    for event in events:
        matched: list[str] = []
        for r in reminders:
            if _reminder_matches(r, event):
                matched.append(r.reminder_id)
        matched.sort()
        if matched != event.matched_reminder_ids:
            await store.agenda.upsert(
                event.model_copy(update={"matched_reminder_ids": matched})
            )
            reminders_matched += 1

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


async def _classify_unseen(store: UserStore, events: list[CachedEvent]) -> int:
    unseen = [e for e in events if e.activity_type is None and e.summary]
    if not unseen:
        return 0

    from level_core.agents.activity import run as activity_run

    ai_by_id: dict[str, ActivityType] = {}
    ai_span_by_id: dict[str, str] = {}
    ai_available = True
    try:
        batches = [unseen[i : i + 25] for i in range(0, len(unseen), 25)]
        for batch in batches:
            payload = [{"event_id": e.event_id, "summary": e.summary} for e in batch]
            result = await activity_run(store=store, events=payload)
            if not result.value:
                continue
            for row in result.value.classifications:  # type: ignore[union-attr]
                ai_by_id[row.event_id] = row.activity_type
                ai_span_by_id[row.event_id] = row.source_span
    except Exception as exc:  # noqa: BLE001 - AI is best-effort
        ai_available = False
        logger.warning("calendar.classify.ai_unavailable", error=str(exc)[:200])

    total = 0
    for e in unseen:
        new_type = ai_by_id.get(e.event_id)
        span = ai_span_by_id.get(e.event_id, "")
        resolved: ActivityType | None
        if new_type and (not span or span in e.summary):
            resolved = new_type
        else:
            resolved = _heuristic_activity(e.summary)
        # If the AI was reachable but chose not to classify this event, accept
        # OTHER as a floor. If the AI was unreachable, leave activity_type None
        # so the next enrich pass gets another chance.
        if resolved is None and ai_available:
            resolved = ActivityType.OTHER
        if resolved is None:
            continue
        await store.agenda.upsert(
            e.model_copy(
                update={
                    "activity_type": resolved,
                    "classified_at": datetime.utcnow(),
                }
            )
        )
        total += 1
    return total


async def reclassify_all(store: UserStore) -> int:
    """Reset every cached event's classification and re-run enrichment.

    Useful after tweaking heuristics or connecting a smarter classifier.
    """
    events = await store.agenda.list()
    reset = 0
    for e in events:
        if e.activity_type is not None:
            await store.agenda.upsert(
                e.model_copy(update={"activity_type": None, "classified_at": None})
            )
            reset += 1
    await enrich_agenda(store)
    return reset


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
    (ActivityType.SPORTS_SWIM, ("swim lesson", "swim practice", "swim meet")),
    (ActivityType.SCHOOL_PICKUP, ("pickup", "pick-up", "aftercare")),
    (ActivityType.SCHOOL_DROPOFF, ("dropoff", "drop-off", "(drop)")),
    (ActivityType.MEDICAL_THERAPY, ("therapy", "therapist")),
    (ActivityType.MEDICAL_APPT, ("dentist", "dental", "pediatric", "doctor visit", "dr appt", "appt")),
    (ActivityType.WORK, ("1:1", "standup", "all hands", "all-hands", "sprint review", "sprint planning")),
)


def _heuristic_activity(summary: str) -> ActivityType | None:
    """Instant, high-precision floor for unambiguous titles.

    Returns None on anything else so the AI classifier can do the real work
    (nuanced titles like "Chart review block" or "Papa \u2014 neurology f/u"
    are exactly what Gemini Flash is good at).
    """
    if not summary:
        return None
    lower = summary.lower()
    for activity, phrases in OBVIOUS_SIGNALS:
        if any(phrase in lower for phrase in phrases):
            return activity
    return None


def _reminder_matches(reminder: Reminder, event: CachedEvent) -> bool:
    if event.activity_type != reminder.match.activity_type:
        return False
    if reminder.match.person_id and reminder.match.person_id not in event.matched_person_ids:
        return False
    return True
