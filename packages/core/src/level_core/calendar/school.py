"""School-paper and sick-day helpers — shared SchoolAnchor + send targets.

AI names the person and extracts the slip. This module only matches records
and fills copy templates. It does not invent a spare adult or a friend roster.
"""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from level_core.calendar.usuals import (
    DEFAULT_TZ,
    event_belongs_to_person,
    parse_event_start,
)
from level_core.profile.people_usuals import find_person_by_name
from level_core.schemas.care import (
    CareContact,
    CarePerson,
    CareProfile,
    SchoolAnchor,
    active_care_people,
)
from level_core.schemas.commitment import CommitmentKind, CommitmentProposal, EventDraft


class SchoolPaperExtract(BaseModel):
    """Structured slip fields — filled by Gemini, never by title regex."""

    deadline: str = ""
    to_email: str = ""
    subject: str = ""
    body: str = ""
    person_name: str = ""
    title: str = ""
    hold_label: str = ""


class SickDayParse(BaseModel):
    """Which care people the check-in named as home sick."""

    is_sick_day: bool = False
    person_names: list[str] = Field(default_factory=list)
    ambiguous: bool = False


def match_people_by_names(
    care: CareProfile | None,
    names: list[str],
) -> list[CarePerson]:
    """Resolve display names / aliases against ``people_profiles``. No literals."""
    if care is None or not names:
        return []
    people = active_care_people(care)
    found: list[CarePerson] = []
    seen: set[str] = set()
    for raw in names:
        person = find_person_by_name(people, raw)
        if person is None or person.person_id in seen:
            continue
        seen.add(person.person_id)
        found.append(person)
    return found


def _norm_role(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def person_contacts(person: CarePerson) -> list[CareContact]:
    """Contacts on the person, plus SchoolAnchor rows if those emails aren't listed."""
    out = list(person.contacts)
    seen = {_norm_role(c.role) + "|" + (c.email or "").strip().lower() for c in out}
    school = person.school
    extras: list[tuple[str, str, str]] = []
    if school:
        if school.teacher_email:
            extras.append(("Teacher", school.teacher_label, school.teacher_email))
        if school.attendance_email:
            extras.append(("Attendance", "", school.attendance_email))
    for role, name, email in extras:
        key = _norm_role(role) + "|" + email.strip().lower()
        if key in seen:
            continue
        out.append(CareContact(role=role, name=name, email=email))
        seen.add(key)
    return out


def match_contacts_by_role(person: CarePerson, role: str) -> list[CareContact]:
    """Match 'teacher' / 'her teacher' to saved roles. No name literals."""
    want = _norm_role(role)
    for prefix in ("her ", "his ", "their ", "the ", "a ", "an "):
        if want.startswith(prefix):
            want = want[len(prefix) :]
    if not want:
        return []
    hits = [
        c
        for c in person_contacts(person)
        if c.email and (want == _norm_role(c.role) or want in _norm_role(c.role))
    ]
    return hits


def school_send_target(person: CarePerson, *, role: str = "") -> tuple[str, str]:
    """Return (email, label) for a role, else the first saved contact."""
    if role.strip():
        hits = match_contacts_by_role(person, role)
        if len(hits) == 1:
            hit = hits[0]
            label = (hit.name or hit.role).strip()
            return hit.email.strip(), label
        if len(hits) > 1:
            return "", "ambiguous"
        return "", ""
    contacts = [c for c in person_contacts(person) if c.email.strip()]
    if not contacts:
        return "", ""
    preferred = next(
        (c for c in contacts if _norm_role(c.role) in {"teacher", "attendance"}),
        contacts[0],
    )
    return preferred.email.strip(), (preferred.name or preferred.role).strip()


def attach_school_email(
    care: CareProfile,
    *,
    person_id: str,
    email: str,
    teacher_label: str = "",
) -> CareProfile:
    """Persist an institutional address onto the person. Empty email is a no-op."""
    address = (email or "").strip()
    if not address or "@" not in address:
        return care
    people: list[CarePerson] = []
    changed = False
    for person in care.people_profiles:
        if person.person_id != person_id:
            people.append(person)
            continue
        changed = True
        prev = person.school or SchoolAnchor()
        school = prev.model_copy(
            update={
                "attendance_email": address if not prev.attendance_email else prev.attendance_email,
                "teacher_email": prev.teacher_email or address,
                "teacher_label": (teacher_label or prev.teacher_label)[:80],
            }
        )
        contacts = list(person.contacts)
        role = "Teacher" if teacher_label or "teacher" in address else "Attendance"
        if not any(
            (c.email or "").strip().lower() == address.lower() for c in contacts
        ):
            contacts.append(
                CareContact(role=role, name=teacher_label[:80], email=address)
            )
        people.append(person.model_copy(update={"school": school, "contacts": contacts}))
    if not changed:
        return care
    return care.model_copy(update={"people_profiles": people})


def events_for_person_on_date(
    *,
    care: CareProfile,
    events: list[dict[str, str | None]],
    person: CarePerson,
    on_date: date,
    tz: ZoneInfo = DEFAULT_TZ,
) -> list[dict[str, str | None]]:
    """Today's events already tagged to this person (AI assign), not title regex."""
    out: list[dict[str, str | None]] = []
    for event in events:
        if (event.get("status") or "confirmed").lower() == "cancelled":
            continue
        if not event_belongs_to_person(event, person=person, care=care):
            continue
        start = parse_event_start(event.get("start"))
        if start is None:
            continue
        if start.astimezone(tz).date() != on_date:
            continue
        out.append(event)
    return out


def draft_contact_note(
    *,
    person: CarePerson,
    on_date: date,
    contact: CareContact | None = None,
    subject: str = "",
    body: str = "",
    reason: str = "",
    from_name: str = "",
) -> tuple[str, str]:
    """Courteous institutional note. Does not use the check-in one-liner as the letter."""
    who = person.display_name.strip() or "this student"
    day = on_date.strftime("%A, %B ") + str(on_date.day)
    role = (contact.role.strip() if contact and contact.role.strip() else "")
    hello = (contact.name.strip() if contact and contact.name.strip() else "") or role
    greeting = f"Dear {hello}," if hello else "Hello,"
    sign = (from_name or "").strip().split()[0] if from_name else ""
    closing = f"\nBest,\n{sign}\n" if sign else "\nThank you,\n"
    _ = (body, reason)
    role_l = role.lower()
    school = any(bit in role_l for bit in ("teacher", "attendance", "school", "office"))
    doctor = "doctor" in role_l or "pediatric" in role_l
    if school:
        subj = (subject or "").strip() or f"{who} will be absent today"
        text = (
            f"{greeting}\n\n"
            f"I'm writing to let you know that {who} will be absent from school "
            f"today ({day}) due to illness. Please excuse this absence.\n\n"
            f"Thank you for your understanding. Please let me know if you need "
            f"anything else from me.\n"
            f"{closing}"
        )
    elif doctor:
        subj = (subject or "").strip() or f"Note about {who} — {day}"
        text = (
            f"{greeting}\n\n"
            f"I'm writing to let you know that {who} isn't well today ({day}). "
            f"I wanted you to have a note on file.\n"
            f"{closing}"
        )
    else:
        subj = (subject or "").strip() or f"Note about {who} — {day}"
        text = (
            f"{greeting}\n\n"
            f"I'm writing with an update about {who} for today ({day}).\n"
            f"{closing}"
        )
    return subj[:200], text.strip() + "\n"


def draft_sick_note(
    *, person: CarePerson, on_date: date, from_name: str = ""
) -> tuple[str, str]:
    return draft_contact_note(
        person=person,
        on_date=on_date,
        reason="will be absent today",
        from_name=from_name,
    )


def build_school_send_proposal(
    *,
    user_id: str,
    user_text: str,
    people: list[CarePerson],
    to_email: str,
    subject: str,
    body: str,
    cancel_event_ids: list[str],
    level_message: str,
    hold_on_calendar: bool = False,
    hold_date: str | None = None,
    hold_title: str = "",
) -> CommitmentProposal:
    """Hold/Run proposal for an institutional send. Does not write until confirm."""
    names = ", ".join(p.display_name for p in people if p.display_name)
    summary = subject or (f"School note for {names}" if names else "School note")
    return CommitmentProposal(
        user_id=user_id,
        kind=CommitmentKind.SCHOOL_SEND,
        user_text=user_text[:2000],
        draft=EventDraft(
            title=(hold_title or summary)[:120],
            local_date=hold_date,
            local_time="08:00",
            duration_minutes=30,
            notes=body[:500],
        ),
        summary=summary[:240],
        level_message=level_message[:500],
        recommended_action="confirm",
        to_email=to_email,
        email_subject=subject[:200],
        email_body=body[:4000],
        person_ids=[p.person_id for p in people],
        cancel_event_ids=cancel_event_ids[:20],
        hold_on_calendar=hold_on_calendar,
    )


def draft_paper_hold_title(extract: SchoolPaperExtract, person: CarePerson | None) -> str:
    label = (extract.hold_label or extract.title or extract.subject or "").strip()
    if label:
        return label[:120]
    who = (person.display_name if person else "").strip()
    return f"School form{' — ' + who if who else ''}"[:120]


__all__ = [
    "SchoolPaperExtract",
    "SickDayParse",
    "attach_school_email",
    "build_school_send_proposal",
    "draft_contact_note",
    "draft_paper_hold_title",
    "draft_sick_note",
    "events_for_person_on_date",
    "match_contacts_by_role",
    "match_people_by_names",
    "person_contacts",
    "school_send_target",
]
