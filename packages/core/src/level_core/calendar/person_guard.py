"""Deterministic post-LLM guard for RoleAgent's proposed care people.

Catches "Grocery" or "Commute" hallucinations before they reach the
user. Runs O(1) per proposed row via frozenset lookups - no LLM, no
Firestore hit, no regex.

Three signals drive the verdict:

  RESPONSIBILITY_WORDS  - "these are activities, not humans" (drop rule).
                           Built from OBVIOUS_SIGNALS (`calendar/enrich.py`)
                           plus a small set of common non-activity nouns
                           that don't have their own ActivityType.

  FAMILY_RELATION_WORDS - "these are always humans, no Google evidence
                           needed". Mom, Papa, Grandma, etc.

  attendee_token_union()- "Google confirmed these humans have accounts on
                           at least one event". Strongest positive signal
                           because Google won't schedule an event with
                           grocery@... as an attendee.

Ordering: drop rule wins first, then either positive signal accepts. If
neither positive signal fires but the name isn't a responsibility word,
we keep the row but mark it `uncertain` so the caller can log it - the
user still has Not-me as the last correction path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from level_core.calendar.enrich import OBVIOUS_SIGNALS
from level_core.schemas import CachedEvent


# Words extracted from OBVIOUS_SIGNALS plus common "responsibility" nouns
# that don't have an ActivityType entry today. Every entry here is a token
# where "person named X" is definitely wrong.
_EXTRA_RESPONSIBILITY_WORDS: frozenset[str] = frozenset(
    {
        # Chores / errands
        "grocery",
        "groceries",
        "errand",
        "errands",
        "laundry",
        "chores",
        "cleanup",
        "trash",
        # Meals
        "lunch",
        "dinner",
        "breakfast",
        "brunch",
        "snack",
        "coffee",
        # Work
        "work",
        "commute",
        "meeting",
        "call",
        "sync",
        "standup",
        "office",
        "focus",
        "review",
        "kickoff",
        "retro",
        "planning",
        "sprint",
        "backlog",
        "onsite",
        "offsite",
        # Personal / places
        "gym",
        "workout",
        "walk",
        "yoga",
        "shower",
        "sleep",
        "nap",
        "bath",
        "reading",
        "study",
        "class",
        "trip",
        "break",
        "flight",
        "drive",
        "shopping",
        "haircut",
        "carpool",
    }
)


def _phrases_from_signals() -> set[str]:
    """Split every OBVIOUS_SIGNALS phrase into words (>=3 chars, alpha)."""
    words: set[str] = set()
    for _activity, phrases in OBVIOUS_SIGNALS:
        for phrase in phrases:
            for token in phrase.split():
                cleaned = "".join(ch for ch in token if ch.isalpha()).lower()
                if len(cleaned) >= 3:
                    words.add(cleaned)
    return words


RESPONSIBILITY_WORDS: frozenset[str] = frozenset(
    _phrases_from_signals() | _EXTRA_RESPONSIBILITY_WORDS
)


# Family-relation labels that show up as raw names in calendars ("Papa
# pickup", "Mom therapy"). Google rarely has these as attendees, so we
# accept them without cross-check.
FAMILY_RELATION_WORDS: frozenset[str] = frozenset(
    {
        "mom",
        "mama",
        "mommy",
        "mother",
        "dad",
        "dada",
        "daddy",
        "father",
        "papa",
        "grandma",
        "grandmother",
        "granny",
        "nana",
        "nonna",
        "oma",
        "abuela",
        "grandpa",
        "grandfather",
        "poppa",
        "papi",
        "nonno",
        "opa",
        "abuelo",
        "auntie",
        "aunt",
        "uncle",
        "tio",
        "tia",
        "sister",
        "brother",
        "sis",
        "bro",
        "stepmom",
        "stepdad",
        "stepmother",
        "stepfather",
        "husband",
        "wife",
        "spouse",
        "partner",
    }
)


@dataclass(frozen=True)
class NameVerdict:
    kept: bool
    reason: str = ""


def _tokenize(name: str) -> tuple[str, ...]:
    """Lower-cased alphabetic tokens for one proposed name."""
    if not name:
        return ()
    parts = name.strip().split()
    out: list[str] = []
    for token in parts:
        cleaned = "".join(ch for ch in token if ch.isalpha()).lower()
        if cleaned:
            out.append(cleaned)
    return tuple(out)


def is_responsibility_word(name: str) -> bool:
    """True when any word in the name is a known responsibility token."""
    tokens = _tokenize(name)
    if not tokens:
        return True  # empty / all-punctuation - drop.
    return any(tok in RESPONSIBILITY_WORDS for tok in tokens)


def is_family_relation_word(name: str) -> bool:
    tokens = _tokenize(name)
    return any(tok in FAMILY_RELATION_WORDS for tok in tokens)


def attendee_token_union(events: Iterable[CachedEvent]) -> frozenset[str]:
    """Union of `attendee_tokens` across every cached event.

    Google's attendees are real humans with accounts, so tokens here are
    the strongest positive signal available. O(events); already in memory.
    """
    out: set[str] = set()
    for e in events:
        for tok in e.attendee_tokens or ():
            cleaned = tok.strip().lower()
            if cleaned:
                out.add(cleaned)
    return frozenset(out)


def evaluate_proposed_name(name: str, *, attendees: frozenset[str]) -> NameVerdict:
    """Post-LLM verdict for one proposed care person.

    Order matters:
      1. Drop rule wins first  (responsibility word or empty => reject).
      2. Positive fast path    (attendee_tokens OR family relation => accept).
      3. Uncertain             (kept, but caller may log for later review).
    """
    if not name or not name.strip():
        return NameVerdict(kept=False, reason="empty_name")
    if is_responsibility_word(name):
        return NameVerdict(kept=False, reason="responsibility_word")
    tokens = _tokenize(name)
    if any(tok in attendees for tok in tokens):
        return NameVerdict(kept=True, reason="attendee_confirmed")
    if is_family_relation_word(name):
        return NameVerdict(kept=True, reason="family_word")
    return NameVerdict(kept=True, reason="uncertain")
