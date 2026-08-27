"""Match a chat email request to saved contacts (no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from level_core.schemas import CarePerson, Contact, ContactKind

_KIND_WORDS: list[tuple[ContactKind, re.Pattern[str]]] = [
    (ContactKind.TEACHER, re.compile(r"\b(?:teachers?|homeroom|instructor)\b", re.I)),
    (ContactKind.DOCTOR, re.compile(r"\b(?:doctors?|dr\.?|pediatrician|physician)\b", re.I)),
    (ContactKind.COACH, re.compile(r"\b(?:coaches|coach)\b", re.I)),
]

_EMAIL_WORD = re.compile(r"\be-?mails?\b", re.I)
_NOTE_WORD = re.compile(r"\b(?:notes?|messages?)\b", re.I)
_SICK_NOTE = re.compile(
    r"\b(?:sick\s+notes?|absence\s+notes?|excuse(?:d)?\s+(?:notes?|absence)|"
    r"school\s+notes?)\b",
    re.I,
)
_SENDISH = re.compile(r"\b(?:send|write|draft|email|message|text)\b", re.I)
_CONTACT_ROLE = re.compile(
    r"\b(?:teachers?|homeroom|doctors?|dr\.?|pediatrician|coaches?|coach)\b",
    re.I,
)
_TELL_ROLE = re.compile(
    r"(?:tell|let)\s+(?:her|him|them|their)\s+(?:teacher|doctor|coach|dr\.?)\s+know",
    re.I,
)

_ORDINAL = {
    "first": 0,
    "1": 0,
    "1st": 0,
    "second": 1,
    "2": 1,
    "2nd": 1,
    "third": 2,
    "3": 2,
    "3rd": 2,
}


@dataclass(frozen=True)
class EmailCandidate:
    contact: Contact
    person: CarePerson | None

    @property
    def label(self) -> str:
        who = self.person.display_name if self.person else "someone"
        kind = self.contact.kind.value
        return f"{self.contact.name} ({who}'s {kind})"


@dataclass(frozen=True)
class EmailResolve:
    """match = one contact; ask = user must pick; none = missing data."""

    status: str
    candidates: list[EmailCandidate]
    reply: str


def is_email_request(message: str) -> bool:
    """True for mail-shaped asks, including 'send Nova's teacher a sick note'."""
    text = message or ""
    if _EMAIL_WORD.search(text) or _SICK_NOTE.search(text) or _TELL_ROLE.search(text):
        return True
    if _CONTACT_ROLE.search(text) and (_SENDISH.search(text) or _NOTE_WORD.search(text)):
        return True
    if _SENDISH.search(text) and _NOTE_WORD.search(text):
        return True
    return False


def kinds_in_message(message: str) -> list[ContactKind]:
    found: list[ContactKind] = []
    for kind, pat in _KIND_WORDS:
        if pat.search(message) and kind not in found:
            found.append(kind)
    return found


def people_mentioned(message: str, people: list[CarePerson]) -> list[CarePerson]:
    text = message or ""
    hits: list[CarePerson] = []
    ranked = sorted(
        (p for p in people if (p.status or "") != "not_me"),
        key=lambda p: len(p.display_name),
        reverse=True,
    )
    for person in ranked:
        names = [person.display_name, *(person.aliases or [])]
        if any(_name_in_text(text, n) for n in names if len(n.strip()) >= 2):
            hits.append(person)
    return hits


def _name_in_text(text: str, name: str) -> bool:
    token = re.escape(name.strip())
    return bool(re.search(rf"\b{token}(?:['’]s)?\b", text, re.I))


def resolve_email_targets(
    message: str,
    people: list[CarePerson],
    contacts: list[Contact],
    history: list[dict[str, str]] | None = None,
) -> EmailResolve:
    kinds = kinds_in_message(message)
    mentioned = people_mentioned(message, people)
    if not mentioned and history:
        prior = " ".join(t.get("text", "") for t in history if t.get("role") == "user")
        mentioned = people_mentioned(prior, people)

    pool = list(contacts)
    if mentioned:
        ids = {p.person_id for p in mentioned}
        pool = [c for c in pool if c.person_id in ids]
    if kinds:
        pool = [c for c in pool if c.kind in kinds]

    people_by_id = {p.person_id: p for p in people}
    candidates = [
        EmailCandidate(contact=c, person=people_by_id.get(c.person_id))
        for c in pool
    ]

    if len(candidates) == 1:
        only = candidates[0]
        if not only.contact.email:
            return EmailResolve(
                status="none",
                candidates=candidates,
                reply=(
                    f"I have {only.label}, but no email address yet. "
                    "Add it on Contacts and I\u2019ll draft this."
                ),
            )
        return EmailResolve(status="match", candidates=candidates, reply="")

    if len(candidates) > 1:
        listed = "\n".join(f"\u2022 {c.label}" for c in candidates[:8])
        who = mentioned[0].display_name if len(mentioned) == 1 else None
        if who and kinds:
            lead = f"I have a few {kinds[0].value}s for {who}. Which one should I write?"
        elif kinds:
            lead = f"I have a few {kinds[0].value}s. Which one should I write?"
        elif who:
            lead = f"I have a few contacts for {who}. Which one should I write?"
        else:
            lead = "I have a few people I can email. Which one?"
        return EmailResolve(status="ask", candidates=candidates, reply=f"{lead}\n\n{listed}")

    if mentioned and kinds:
        who = mentioned[0].display_name
        kind = kinds[0].value
        return EmailResolve(
            status="none",
            candidates=[],
            reply=f"I don\u2019t have a {kind} for {who} yet. Add one on Contacts and I\u2019ll draft this.",
        )
    if mentioned:
        who = mentioned[0].display_name
        return EmailResolve(
            status="none",
            candidates=[],
            reply=f"I don\u2019t have contacts for {who} yet. Add them on Contacts and I\u2019ll draft this.",
        )
    if kinds:
        kind = kinds[0].value
        return EmailResolve(
            status="none",
            candidates=[],
            reply=f"I don\u2019t have a {kind} saved yet. Add one on Contacts and I\u2019ll draft this.",
        )
    if not contacts:
        return EmailResolve(
            status="none",
            candidates=[],
            reply="I don\u2019t have contacts saved yet. Add someone on Contacts and I can draft from chat.",
        )
    listed = "\n".join(
        f"\u2022 {EmailCandidate(c, people_by_id.get(c.person_id)).label}"
        for c in contacts[:8]
    )
    return EmailResolve(
        status="none",
        candidates=[],
        reply=f"Who should I email?\n\n{listed}",
    )


# Titlecase name shape: leading capital, at least 2 chars total, allow
# internal hyphens or apostrophes ("O'Brien", "Mary-Kate") but not
# ALLCAPS acronyms ("PT", "NYC") - those confuse the guard on things
# like "PT with Helen" being a person.
_TITLECASE_NAME = re.compile(
    r"\b[A-Z][a-z][A-Za-z'\-]*\b",
)

# Titlecase words that are *not* proper names but often show up
# mid-sentence. Extending this list is preferable to loosening the
# guard - the cost of a false positive here is a spurious
# clarification bubble, which is exactly what the guard exists to
# produce. Better to grow the stop-list than to draft an email
# about a made-up person.
_NON_PERSON_TITLECASE: frozenset[str] = frozenset(
    word.lower()
    for word in (
        # Salutations (either as own word or the "Ms" of "Ms. Anna"
        # after we strip the period).
        "Ms", "Mr", "Mrs", "Dr", "Prof", "Sir", "Madam", "Miss",
        # Chat-shaped words that could sit at a sentence start.
        "Hi", "Hello", "Hey", "Yes", "No", "Ok", "Okay", "Sure",
        "Thanks", "Thank", "Please", "Sorry", "Nope",
        "The", "This", "That", "There", "Those", "These",
        "It", "It's", "He", "She", "They", "We", "You", "I",
        "And", "Or", "But", "So", "Also",
        "When", "Where", "Why", "What", "Who", "How", "Which",
        "Can", "Could", "Would", "Should", "Will", "Must",
        # Time words.
        "Today", "Tomorrow", "Yesterday", "Tonight", "Now", "Later",
        "Soon", "Never", "Always", "Every", "Any",
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday",
        "Mon", "Tue", "Tues", "Wed", "Thu", "Thur", "Thurs", "Fri",
        "Sat", "Sun",
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
        "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep",
        "Sept", "Oct", "Nov", "Dec",
        "AM", "PM", "Am", "Pm",  # AM/PM titlecase variants
        # Product / app.
        "Level",
        # Role-shaped nouns that a caregiver might Titlecase.
        "School", "Work", "Home", "Office", "Church", "Practice",
        "Class", "Gym", "Ballet", "Soccer", "Piano",
    )
)


def unknown_person_names(
    message: str,
    people: list[CarePerson],
    contacts: list[Contact],
) -> list[str]:
    """Return Titlecase name-shaped tokens in ``message`` that don't
    match any known person or contact.

    Purpose: catch the "email Ms. Anna that Jordan is sick" case
    where the caregiver names a subject person the roster doesn't
    know about. Without this, the EmailAgent obediently drafts an
    email about a made-up kid because the LLM has no way to know
    Jordan isn't real. With this, chat.py returns a clarification
    bubble BEFORE the LLM call: "I don't know a Jordan - did you
    mean Nova or Theo?".

    Design choices:
    - **Titlecase-only.** Lowercase words are ambiguous ("mom" could
      be Helen's alias or a common noun); the caller's caseless
      alias match in ``people_mentioned`` handles that path. We only
      trip on capitalised name-shaped tokens the user clearly meant
      as a proper noun.
    - **Contact-name subtraction.** "Ms. Anna" (the teacher) is a
      known contact; her first name shouldn't trigger. We split each
      Contact.name on whitespace and add every word (>= 2 chars) to
      the known set.
    - **Alias-aware.** CarePerson aliases count as known
      (``Dad`` -> Josh, ``Mom`` -> Helen).
    - **Return preserves original casing** so the clarification
      bubble echoes exactly what the user typed. Deduped
      case-insensitively, first occurrence wins.

    Returns an empty list when the message contains no unknown
    proper nouns - which is the common case. Callers should skip
    the guard entirely in that case.
    """
    if not message:
        return []

    known_lower: set[str] = set()
    for p in people:
        if (p.status or "") == "not_me":
            # A rejected person shouldn't paper over a new mention
            # of the same name (they told us not_me for a reason).
            continue
        if p.display_name:
            known_lower.add(p.display_name.strip().lower())
        for alias in p.aliases or ():
            alias = alias.strip()
            if len(alias) >= 2:
                known_lower.add(alias.lower())
    for c in contacts:
        # Contacts are stored as full names ("Ms. Anna", "Dr. Chen").
        # Split so the user's shortened form ("Anna", "Chen") also
        # matches as known.
        for word in re.split(r"\s+", c.name or ""):
            cleaned = word.strip(".,").lower()
            if len(cleaned) >= 2:
                known_lower.add(cleaned)

    seen_ci: set[str] = set()
    unknown: list[str] = []
    for m in _TITLECASE_NAME.finditer(message):
        token = m.group(0)
        # Strip trailing possessive ('s or straight ') so "Nova's" and
        # "Mom's" resolve to the same known-set entry as "Nova"/"Mom".
        # Do this on the LOOKUP key only - the surface form we echo
        # back to the user should preserve original casing/possessive
        # for the "did you mean" reply to feel natural.
        low = token.lower()
        low = re.sub(r"[\u2019']s$", "", low)
        if low in _NON_PERSON_TITLECASE:
            continue
        if low in known_lower:
            continue
        if low in seen_ci:
            continue
        seen_ci.add(low)
        unknown.append(token)
    return unknown


def pick_candidate(message: str, candidates: list[EmailCandidate]) -> EmailCandidate | None:
    text = (message or "").strip()
    if not text or not candidates:
        return None
    ordinal = _ORDINAL.get(text.lower())
    if ordinal is not None and ordinal < len(candidates):
        return candidates[ordinal]

    hits: list[EmailCandidate] = []
    for cand in candidates:
        names = [cand.contact.name]
        if cand.person:
            names.append(cand.person.display_name)
            names.extend(cand.person.aliases or [])
        if any(_name_in_text(text, n) for n in names if len(n.strip()) >= 2):
            hits.append(cand)
        elif cand.contact.kind.value in text.lower() and kinds_in_message(text):
            hits.append(cand)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        kinds = kinds_in_message(text)
        if kinds:
            narrowed = [h for h in hits if h.contact.kind in kinds]
            if len(narrowed) == 1:
                return narrowed[0]
    return None
