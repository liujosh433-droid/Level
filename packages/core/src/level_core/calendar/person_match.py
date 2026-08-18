"""Shared person <-> event matcher.

Both agenda enrichment and usual detection need to decide "is this event
about this person?". Naive substring matching on aliases blew up because
a single-letter alias like "N" (for Nova) matched "sync", "standup",
"Interdisciplinary" - poisoning most of the calendar.

Rules used here:
  1. Aliases of length < MIN_ALIAS_LEN are IGNORED entirely (rejects "N",
     "T", "M"...). The display_name is always used.
  2. All matches are WORD-BOUNDARY (`\b`), so "Dad" hits "call Dad" but
     not "sudden" or "standup".
  3. Attendee tokens (already first-name tokens from Google) match by
     exact (case-insensitive) equality, not substring.
  4. If nothing matches, callers should fall back to the self person -
     `resolve_person_id` does this automatically.

No text is inspected beyond exact-name matching - the AI has already
labelled the event's activity_type upstream.
"""

from __future__ import annotations

import re
from functools import lru_cache

from level_core.schemas import CachedEvent, CarePerson

MIN_ALIAS_LEN = 2


def useful_aliases(person: CarePerson) -> list[str]:
    """Aliases long enough to be matched safely. Short ones are dropped."""
    return [a for a in person.aliases if len(a.strip()) >= MIN_ALIAS_LEN]


@lru_cache(maxsize=1024)
def _boundary_pattern(token: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)


def person_matches(event: CachedEvent, person: CarePerson) -> bool:
    """Return True if `event` is confidently about `person`."""
    tokens = {person.display_name.strip().lower()}
    for a in useful_aliases(person):
        tokens.add(a.strip().lower())
    tokens.discard("")
    if not tokens:
        return False

    summary = event.summary or ""
    for tok in tokens:
        if _boundary_pattern(tok).search(summary):
            return True

    if event.attendee_tokens:
        attendee = {t.strip().lower() for t in event.attendee_tokens}
        if tokens & attendee:
            return True

    return False


def resolve_person_id(event: CachedEvent, people: list[CarePerson]) -> str | None:
    """Pick a person for this event.

    Order: (1) any cached match already on the event, (2) word-boundary
    match on display_name or long-enough alias, (3) the self person if
    one exists. Returns None only when no self is set.
    """
    if event.matched_person_ids:
        return event.matched_person_ids[0]
    for p in people:
        if person_matches(event, p):
            return p.person_id
    for p in people:
        if p.is_self:
            return p.person_id
    return None
