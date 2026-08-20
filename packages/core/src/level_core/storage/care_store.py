"""Higher-level helpers over UserStore for common care flows.

The `UserStore` repos are dumb CRUD. This module holds the ordering rules
that always apply: assign IDs, resolve aliases, mark status transitions.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

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


_NAME = r"(?P<name>[A-Za-z][A-Za-z'.\-]+(?:\s+[A-Za-z][A-Za-z'.\-]+){0,2})"
_INTRO_PATTERNS = (
    re.compile(rf"^{_NAME}\s+is my\s+(?P<rest>.+?)\.?$", re.I | re.DOTALL),
    re.compile(
        rf"\badd(?:ing)?\s+(?:a\s+new\s+(?:role|person),?\s*)?{_NAME}\s+as\s+(?P<rest>.+)",
        re.I,
    ),
    re.compile(rf"{_NAME}\s+as\s+(?:an?\s+|my\s+)?(?P<rest>co[-\s]?parents?\b.*)", re.I),
)


def relation_from_phrase(phrase: str) -> CareRelation | None:
    """Map 'occasional co-parent helper' / 'kid, not my dad' onto CareRelation."""
    positive = re.split(r"\bnot\b", phrase, maxsplit=1, flags=re.I)[0]
    lower = positive.lower()
    if re.search(r"\bco[-\s]?parent", lower):
        return CareRelation.COPARENT
    if re.search(r"\b(?:husband|wife|spouse|partner|boyfriend|girlfriend)\b", lower):
        return CareRelation.PARTNER
    if re.search(r"\b(?:kids?|child|son|daughter)\b", lower):
        return CareRelation.CHILD
    if re.search(
        r"\b(?:dad|mom|father|mother|grandma|grandpa|grandmother|grandfather|elder|parents?)\b",
        lower,
    ):
        return CareRelation.ELDER
    if re.search(r"\b(?:me|myself)\b", lower):
        return CareRelation.SELF
    if re.search(
        r"\b(?:nephew|niece|friend|helper|nanny|babysitter|colleague|coworker)\b",
        lower,
    ):
        return CareRelation.OTHER
    return None


@dataclass(frozen=True)
class ParsedReminder:
    text: str
    activity_type: ActivityType
    person_display_name: str | None
    source_span: str


_REMINDER_LEAD = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?"
    r"(?:"
    r"remind\s+me\s+(?:to\s+|about\s+)?"
    r"|don['\u2019]t\s+forget\s+(?:to\s+)?"
    r"|i\s+(?:keep\s+|always\s+)?(?:forget(?:ting)?|forgot)\s+(?:to\s+)?"
    r")",
    re.IGNORECASE,
)

_REMINDER_QUESTION = re.compile(
    r"\bremind\s+me\s+(?:what|when|where|if|whether|who)\b",
    re.IGNORECASE,
)

_REMINDER_ACTIVITY_HINTS: list[tuple[re.Pattern[str], ActivityType]] = [
    (re.compile(r"\b(?:soccer)\b", re.I), ActivityType.SPORTS_SOCCER),
    (re.compile(r"\bbasketball\b", re.I), ActivityType.SPORTS_BASKETBALL),
    (re.compile(r"\bswim(?:ming)?\b", re.I), ActivityType.SPORTS_SWIM),
    (re.compile(r"\bsports?\b", re.I), ActivityType.SPORTS_OTHER),
    (
        re.compile(
            r"\bdrop(?:ping)?(?:\s+\w+){0,2}\s+off\b|\bdrop[\s-]?off\b",
            re.I,
        ),
        ActivityType.SCHOOL_DROPOFF,
    ),
    (
        re.compile(
            r"\bpick(?:ing)?(?:\s+\w+){0,2}\s+up\b|\bpick[\s-]?up\b",
            re.I,
        ),
        ActivityType.SCHOOL_PICKUP,
    ),
    (re.compile(r"\bschool\b", re.I), ActivityType.SCHOOL_EVENT),
    (re.compile(r"\btherapy\b", re.I), ActivityType.MEDICAL_THERAPY),
    (re.compile(r"\b(?:dentist|doctor|medical|appointment)\b", re.I), ActivityType.MEDICAL_APPT),
    (re.compile(r"\b(?:elder|dad|mom|father|mother|parent|family)\b", re.I), ActivityType.FAMILY),
    (
        re.compile(
            r"\b(?:meetings?|standup|stand-up|1:1|one[- ]on[- ]ones?|calls?|work)\b",
            re.I,
        ),
        ActivityType.WORK,
    ),
]

_TRAILING_ACTIVITY_CONTEXT = re.compile(
    r"\s+(?:"
    r"when\s+i\s+(?:drop(?:ping)?(?:\s+\w+){0,2}\s+off|pick(?:ing)?(?:\s+\w+){0,2}\s+up)"
    r"|(?:to|at|for|before|during|on)\s+(?:all\s+)?"
    r"(?:my\s+|our\s+)?"
    r"(?:meetings?|work(?:\s+(?:events?|days?))?|calls?|standups?"
    r"|drop[\s-]?offs?|pick[\s-]?ups?)"
    r")\s*$",
    re.IGNORECASE,
)

_POSSESSIVE_ITEM = re.compile(
    r"^([A-Za-z][A-Za-z'-]+)'s\s+(.+)$",
)

_ASK_FOR_REMINDER_ITEM = re.compile(
    r"thing you might forget",
    re.IGNORECASE,
)

_FOLLOWUP_SKIP = re.compile(
    r"\b(?:book|schedule|email|prioritize|cancel|move|find a time)\b|\?",
    re.IGNORECASE,
)


def activity_hint_from_text(*parts: str) -> ActivityType:
    blob = " ".join(p for p in parts if p)
    for pattern, activity in _REMINDER_ACTIVITY_HINTS:
        if pattern.search(blob):
            return activity
    return ActivityType.OTHER


def _title_reminder_item(item: str) -> str:
    cleaned = item.strip(" .!")
    if not cleaned:
        return cleaned
    return cleaned[0].upper() + cleaned[1:]


def parse_reminder(message: str) -> ParsedReminder | None:
    """Pull a reminder from 'remind me to bring a charger to my meetings'."""
    text = " ".join(message.strip().split())
    if not text or _REMINDER_QUESTION.search(text):
        return None
    match = _REMINDER_LEAD.search(text)
    if not match:
        return None
    rest = text[match.end() :].strip(" .!")
    if len(rest) < 2:
        return None
    if rest.lower() in {"later", "tomorrow", "please", "something", "it"}:
        return None
    return _reminder_from_item(rest, source_span=rest[:200])


def parse_reminder_followup(
    message: str, history: list[dict[str, str]] | None
) -> ParsedReminder | None:
    """After Level asked for the item, accept a short noun like 'a charger'."""
    if not history:
        return None
    last_assistant = next(
        (t.get("text") or "" for t in reversed(history) if t.get("role") == "assistant"),
        "",
    )
    if not _ASK_FOR_REMINDER_ITEM.search(last_assistant):
        return None
    item = " ".join(message.strip().split()).strip(" .!")
    if len(item) < 2 or _FOLLOWUP_SKIP.search(item) or _REMINDER_LEAD.search(item):
        return None
    prior_user = " ".join(
        t.get("text") or "" for t in history if t.get("role") == "user"
    )
    return _reminder_from_item(item, source_span=item[:200], extra_context=prior_user)


def _reminder_from_item(
    rest: str, *, source_span: str, extra_context: str = ""
) -> ParsedReminder:
    activity = activity_hint_from_text(rest, extra_context)
    item = _TRAILING_ACTIVITY_CONTEXT.sub("", rest).strip() or rest
    person: str | None = None
    possessive = _POSSESSIVE_ITEM.match(item)
    if possessive:
        person = possessive.group(1)
        item = possessive.group(2).strip()
    return ParsedReminder(
        text=_title_reminder_item(item),
        activity_type=activity,
        person_display_name=person,
        source_span=source_span,
    )


def parse_person_intro(message: str) -> tuple[str, CareRelation] | None:
    """Pull (name, relation) from 'Alex is my co-parent' / 'add Alex as co-parent'."""
    text = " ".join(message.strip().split())
    if not text:
        return None
    for pattern in _INTRO_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        name = match.group("name").strip()
        relation = relation_from_phrase(match.group("rest") or "")
        if name and relation:
            return name, relation
    return None


def relation_label(relation: CareRelation) -> str:
    return {
        CareRelation.SELF: "you",
        CareRelation.CHILD: "kid",
        CareRelation.ELDER: "elder",
        CareRelation.COPARENT: "co-parent",
        CareRelation.PARTNER: "partner",
        CareRelation.OTHER: "other",
    }[relation]


async def upsert_kept_person(
    store: UserStore,
    *,
    display_name: str,
    relation: CareRelation,
    is_self: bool = False,
    source_span: str | None = None,
) -> CarePerson:
    person = await propose_person(
        store,
        display_name=display_name,
        relation=relation,
        is_self=is_self or relation == CareRelation.SELF,
        source_span=source_span,
    )
    return await store.people.upsert(
        person.model_copy(
            update={
                "display_name": display_name.strip(),
                "relation": relation,
                "care_role_id": role_for_relation(relation),
                "is_self": is_self or relation == CareRelation.SELF,
                "status": "kept",
            }
        )
    )


async def ensure_self_person(store: UserStore) -> CarePerson:
    """Work/commute/lunch have no name in the title. Usuals hang those on self."""
    existing = [
        p for p in await store.people.list() if p.is_self and (p.status or "") != "not_me"
    ]
    if existing:
        return existing[0]
    profile = await store.profile.read() or {}
    name = str(profile.get("display_name") or "").strip()
    if not name or name.lower() in {"you", "me", "self", "myself", "a parent"}:
        name = "You"
    person = await propose_person(
        store,
        display_name=name,
        relation=CareRelation.SELF,
        is_self=True,
        source_span="self",
    )
    if person.status == "proposed":
        updated = await set_person_status(store, person.person_id, "kept")
        return updated or person
    return person


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


async def delete_reminder(store: UserStore, reminder_id: str) -> bool:
    """Remove a reminder and detach it from every cached event."""
    existing = await store.reminders.get(reminder_id)
    if not existing:
        return False
    await record_negative(
        store,
        agent=NegativeAgent.REMINDER,
        field="text",
        value=existing.text,
    )
    events = await store.agenda.list()
    stripped = [
        e.model_copy(
            update={
                "matched_reminder_ids": [rid for rid in e.matched_reminder_ids if rid != reminder_id]
            }
        )
        for e in events
        if reminder_id in e.matched_reminder_ids
    ]
    await store.agenda.upsert_many(stripped)
    await store.reminders.delete(reminder_id)
    return True


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
