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
