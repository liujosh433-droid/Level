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


def resolve_person_ids(event: CachedEvent, people: list[CarePerson]) -> list[str]:
    """Every care person this event is about.

    A title like "Nova + Theo dropoff" must yield both kids, not whichever
    `matched_person_ids` happened to sort first. Falls back to self only
    when nobody is named (Work, commute, grocery).

    The cache (`event.matched_person_ids`) is a HINT, not gospel. Two
    subtle failure modes it must survive:

      1. Stale [self_id] fallback: person X was added to the roster
         AFTER an enrichment pass stamped `[self_id]` on an event
         whose summary names X. Trusting the cache would leave the
         event tagged as Me forever until the next enrich_agenda run.
      2. Person promoted from proposed -> kept: same cache should
         still map to that person; the cache holds up.

    So: only trust the cache when it names at least one non-self
    person. Otherwise, re-run `person_matches` and prefer real named
    people over the self fallback.
    """
    known = {p.person_id for p in people}
    self_ids = {p.person_id for p in people if p.is_self}

    if event.matched_person_ids:
        cached = [pid for pid in event.matched_person_ids if pid in known]
        cached_has_non_self = any(pid not in self_ids for pid in cached)
        if cached and cached_has_non_self:
            return list(dict.fromkeys(cached))
        # Fall through: cache is empty (after pruning removed people) or
        # only points to self, both of which are ambiguous. Re-match.

    matched = [p.person_id for p in people if person_matches(event, p)]
    if matched:
        # A named person always wins over the self fallback. This is
        # what breaks the "School drop-off - Jordan tagged as Me" bug
        # when the cache says [self_id] but Jordan is now in the roster.
        non_self = [pid for pid in matched if pid not in self_ids]
        return list(dict.fromkeys(non_self or matched))

    for p in people:
        if p.is_self:
            return [p.person_id]
    return []


def resolve_person_id(event: CachedEvent, people: list[CarePerson]) -> str | None:
    """Pick a primary person for this event. See `resolve_person_ids`."""
    ids = resolve_person_ids(event, people)
    return ids[0] if ids else None
