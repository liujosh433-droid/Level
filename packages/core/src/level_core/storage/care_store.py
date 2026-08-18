"""Higher-level helpers over UserStore for common care flows.

The `UserStore` repos are dumb CRUD. This module holds the ordering rules
that always apply: assign IDs, resolve aliases, mark status transitions.
"""

from __future__ import annotations

import uuid

from level_core.schemas import (
    ActivityType,
    CarePerson,
    CareRelation,
    HourBand,
    NegativeAgent,
    NegativeFeedback,
    Priority,
    Reminder,
    ReminderMatch,
    Usual,
    UsualStatus,
    Weekday,
)
from level_core.schemas.care import role_for_relation
from level_core.storage.base import UserStore


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def find_person_by_name(
    store: UserStore, name: str
) -> CarePerson | None:
    """Case-insensitive match on display_name OR any alias."""
    lower = name.lower().strip()
    if not lower:
        return None
    for existing in await store.people.list():
        if existing.display_name.lower().strip() == lower:
            return existing
        if any(a.lower().strip() == lower for a in existing.aliases):
            return existing
    return None


MIN_ALIAS_LEN = 2


def _clean_aliases(aliases: list[str] | None) -> list[str]:
    """Reject aliases too short to safely match (single letters like 'N' 'T')."""
    if not aliases:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases:
        stripped = a.strip()
        if len(stripped) < MIN_ALIAS_LEN:
            continue
        key = stripped.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(stripped)
    return out


async def propose_person(
    store: UserStore,
    *,
    display_name: str,
    relation: CareRelation,
    aliases: list[str] | None = None,
    is_self: bool = False,
    source_span: str | None = None,
) -> CarePerson:
    """Idempotent by name/alias. Never overwrites a person the user has already
    corrected (`kept` or `not_me`). If we already proposed the person but with a
    different relation, upgrade the row in-place instead of creating a duplicate.
    Aliases shorter than MIN_ALIAS_LEN are dropped to prevent substring
    false-positives ("N" matching "sync", "standup", ...).
    """
    safe_aliases = _clean_aliases(aliases)
    existing = await find_person_by_name(store, display_name)
    if existing is not None:
        if existing.status != "proposed":
            # User already touched this person - respect their classification.
            return existing
        if existing.relation == relation and existing.is_self == is_self:
            return existing
        return await store.people.upsert(
            existing.model_copy(
                update={
                    "relation": relation,
                    "care_role_id": role_for_relation(relation),
                    "is_self": is_self,
                }
            )
        )
    person = CarePerson(
        person_id=new_id("p"),
        display_name=display_name.strip(),
        relation=relation,
        care_role_id=role_for_relation(relation),
        aliases=safe_aliases,
        is_self=is_self,
        status="proposed",
        source_span=source_span,
    )
    return await store.people.upsert(person)


async def set_person_status(store: UserStore, person_id: str, status: str) -> CarePerson | None:
    person = await store.people.get(person_id)
    if not person:
        return None
    updated = person.model_copy(update={"status": status})
    return await store.people.upsert(updated)


async def propose_usual(
    store: UserStore,
    *,
    person_id: str,
    weekday: Weekday,
    hour_band: HourBand,
    activity_type: ActivityType,
    display_summary: str,
    source_event_uids: list[str],
    confidence: float,
) -> Usual:
    usual_id = Usual.compose_id(person_id, weekday, hour_band)
    existing = await store.usuals.get(usual_id)
    if existing and existing.status == UsualStatus.NOT_ME:
        return existing
    payload = Usual(
        usual_id=usual_id,
        person_id=person_id,
        weekday=weekday,
        hour_band=hour_band,
        activity_type=activity_type,
        display_summary=display_summary,
        source_event_uids=source_event_uids,
        confidence=confidence,
        status=existing.status if existing else UsualStatus.PROPOSED,
    )
    return await store.usuals.upsert(payload)


async def sync_usuals(
    store: UserStore, fresh_usual_ids: set[str]
) -> int:
    """Delete stale `proposed` usuals not in the fresh candidate set.

    `kept` and `not_me` usuals are user-owned and are always preserved.
    This is what stops stale attributions (e.g. an old Nova usual whose
    source events now correctly point to Me) from lingering forever
    under a different composite key.
    """
    removed = 0
    for u in await store.usuals.list():
        if u.status != UsualStatus.PROPOSED:
            continue
        if u.usual_id in fresh_usual_ids:
            continue
        await store.usuals.delete(u.usual_id)
        removed += 1
    return removed


async def set_usual_status(store: UserStore, usual_id: str, status: UsualStatus) -> Usual | None:
    usual = await store.usuals.get(usual_id)
    if not usual:
        return None
    return await store.usuals.upsert(usual.model_copy(update={"status": status}))


async def add_priority(
    store: UserStore,
    *,
    text: str,
    weight: int = 3,
    activity_types: list[ActivityType] | None = None,
    source_span: str | None = None,
) -> Priority:
    prio = Priority(
        priority_id=new_id("prio"),
        text=text.strip(),
        weight=weight,
        activity_types=activity_types or [],
        source="chat",
        source_span=source_span,
    )
    return await store.priorities.upsert(prio)


async def add_reminder(
    store: UserStore,
    *,
    text: str,
    person_id: str | None,
    activity_type: ActivityType,
    lead_minutes: int = 60,
    source_span: str | None = None,
) -> Reminder:
    reminder = Reminder(
        reminder_id=new_id("rem"),
        text=text.strip(),
        match=ReminderMatch(person_id=person_id, activity_type=activity_type),
        lead_minutes=lead_minutes,
        source_span=source_span,
    )
    return await store.reminders.upsert(reminder)


async def record_negative(
    store: UserStore,
    *,
    agent: NegativeAgent,
    field: str,
    value: str,
    reason: str | None = None,
) -> NegativeFeedback:
    neg = NegativeFeedback(
        negative_id=new_id("neg"),
        agent=agent,
        field=field,
        value=value,
        reason=reason,
    )
    return await store.negatives.upsert(neg)


async def recent_negatives(
    store: UserStore, *, agent: NegativeAgent, limit: int = 20
) -> list[NegativeFeedback]:
    all_negs = [n for n in await store.negatives.list() if n.agent == agent]
    all_negs.sort(key=lambda n: n.created_at, reverse=True)
    return all_negs[:limit]
