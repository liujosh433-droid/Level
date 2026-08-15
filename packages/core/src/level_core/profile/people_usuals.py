"""Merge AI-proposed people + usuals onto a Care Profile.

Keep / Not me wins. No regex clustering — the model names people and usuals;
this module only matches records and preserves locks.
"""

from __future__ import annotations

from level_core.calendar.routines import normalize_routine
from level_core.calendar.usuals import series_usuals_from_agenda
from level_core.schemas.base import _now_utc
from level_core.schemas.care import (
    CARE_ROLE_LABELS,
    CarePerson,
    CareProfile,
    CareRoleId,
    UsualWindow,
    derive_person_relationships,
    is_self_person,
    seed_contacts,
)
from level_core.schemas.profile import BulletStatus


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _hours_to_minutes(hour: int | None, default: int) -> int:
    if hour is None:
        return default
    return max(0, min(23, int(hour))) * 60


def person_name_keys(person: CarePerson) -> set[str]:
    keys = {_norm(person.display_name)}
    keys.update(_norm(a) for a in person.aliases if a)
    keys.discard("")
    return keys


def find_person_by_name(
    people: list[CarePerson],
    raw: str,
    *,
    alias_map: dict[str, str] | None = None,
) -> CarePerson | None:
    name = (raw or "").strip()
    if alias_map:
        name = alias_map.get(name.lower(), name)
    key = _norm(name)
    if not key:
        return None
    for person in people:
        if key in person_name_keys(person) or key == person.person_id.lower():
            return person
    return None


def _usual_slot_key(person_id: str, weekday: int, start_minute: int) -> tuple[str, int, int]:
    band = (int(start_minute) // 60) * 60
    return (person_id, int(weekday), band)


def usual_id_for_slot(person_id: str, weekday: int, start_minute: int) -> str:
    """Stable id so a Today read and a later persist resolve the same slot."""
    pid, weekday_n, band = _usual_slot_key(person_id, weekday, start_minute)
    return f"u:{pid}:{weekday_n}:{band}"


def merge_people_and_usuals(
    *,
    previous: CareProfile | None,
    inferred: object,
    alias_map: dict[str, str],
    event_person_hints: dict[str, str],
) -> tuple[list[CarePerson], dict[str, str]]:
    """Return (people_profiles, calendar_person_by_summary).

    ``inferred`` is a ``CareHolisticInfer`` (typed loosely to avoid import cycle).
    """
    prev_people = list(previous.people_profiles) if previous else []
    prev_by_name: dict[str, CarePerson] = {}
    for person in prev_people:
        for key in person_name_keys(person):
            prev_by_name.setdefault(key, person)

    people: list[CarePerson] = []
    seen_ids: set[str] = set()
    raw_people = getattr(inferred, "people", None) or []
    for row in raw_people:
        name = (getattr(row, "name", None) or "").strip()
        if alias_map:
            name = alias_map.get(name.lower(), name)
        name = name.strip()
        if not name or len(name) > 80:
            continue
        old = prev_by_name.get(_norm(name))
        if old is None and _norm(name) in {"you", "me", "myself"}:
            continue
        aliases = [
            a.strip()
            for a in (getattr(row, "also_known_as", None) or [])
            if isinstance(a, str) and a.strip()
        ][:8]
        their = " ".join((getattr(row, "relationship", None) or "").split())[:48]
        yours = " ".join((getattr(row, "your_role", None) or "").split())[:48]
        role = (getattr(row, "role", None) or "child_care").strip() or "child_care"
        if old is not None:
            if old.person_id in seen_ids:
                continue
            seen_ids.add(old.person_id)
            if old.status is BulletStatus.REJECTED:
                people.append(old)
                continue
            merged_aliases = list(dict.fromkeys([*old.aliases, *aliases]))[:8]
            people.append(
                old.model_copy(
                    update={
                        "display_name": old.display_name
                        if old.status in {BulletStatus.ACCEPTED, BulletStatus.EDITED}
                        else name,
                        "aliases": merged_aliases,
                        "their_relation": old.their_relation
                        if is_self_person(old)
                        else (their or old.their_relation),
                        "your_role": old.your_role
                        if is_self_person(old)
                        else (yours or old.your_role),
                        "care_role_id": old.care_role_id
                        if is_self_person(old)
                        else (role or old.care_role_id),
                    }
                )
            )
        else:
            person = CarePerson(
                display_name=name,
                aliases=aliases,
                their_relation=their,
                your_role=yours,
                care_role_id=role,
            )
            if person.person_id in seen_ids:
                continue
            seen_ids.add(person.person_id)
            people.append(person)

    for old in prev_people:
        if old.person_id in seen_ids:
            continue
        # Keep locked or rejected people the model forgot.
        if old.status in {
            BulletStatus.ACCEPTED,
            BulletStatus.EDITED,
            BulletStatus.REJECTED,
        }:
            people.append(old)
            seen_ids.add(old.person_id)

    by_id = {p.person_id: p for p in people}
    # Apply proposed usuals onto current people.
    proposed = getattr(inferred, "usuals", None) or []
    for raw in proposed:
        person_name = (getattr(raw, "person", None) or "").strip()
        person = find_person_by_name(list(by_id.values()), person_name, alias_map=alias_map)
        if (
            person is None
            or person.status is BulletStatus.REJECTED
            or is_self_person(person)
        ):
            continue
        weekday = getattr(raw, "weekday", None)
        if weekday is None or not (0 <= int(weekday) <= 6):
            continue
        start_m = getattr(raw, "start_minute", None)
        if start_m is None:
            start_m = _hours_to_minutes(getattr(raw, "start_hour", None), 15 * 60)
        end_m = getattr(raw, "end_minute", None)
        if end_m is None:
            end_m = _hours_to_minutes(getattr(raw, "end_hour", None), int(start_m) + 60)
        start_m = max(0, min(24 * 60 - 15, int(start_m)))
        end_m = max(start_m + 15, min(24 * 60, int(end_m)))
        routine = normalize_routine(getattr(raw, "routine", None) or "")
        label = routine or " ".join((getattr(raw, "label", None) or "").split())[:120]
        if len(label) < 3:
            continue
        evidence = " ".join((getattr(raw, "evidence", None) or "").split())[:240]
        titles = [
            t.strip()
            for t in (getattr(raw, "evidence_titles", None) or [])
            if isinstance(t, str) and t.strip()
        ][:8]
        slot = _usual_slot_key(person.person_id, int(weekday), start_m)
        existing = next(
            (
                u
                for u in person.usuals
                if _usual_slot_key(u.person_id, u.weekday, u.start_minute) == slot
            ),
            None,
        )
        if existing is not None and existing.status is BulletStatus.REJECTED:
            continue
        if existing is not None and existing.status in {
            BulletStatus.ACCEPTED,
            BulletStatus.EDITED,
        }:
            updated = existing.model_copy(
                update={
                    "evidence": evidence or existing.evidence,
                    "evidence_titles": list(
                        dict.fromkeys([*existing.evidence_titles, *titles])
                    )[:8],
                    "confidence": max(existing.confidence, 0.7),
                }
            )
            next_usuals = [
                updated if u.usual_id == existing.usual_id else u for u in person.usuals
            ]
        elif existing is not None:
            updated = existing.model_copy(
                update={
                    "label": label,
                    "start_minute": start_m,
                    "end_minute": end_m,
                    "evidence": evidence or existing.evidence,
                    "evidence_titles": list(
                        dict.fromkeys([*existing.evidence_titles, *titles])
                    )[:8],
                }
            )
            next_usuals = [
                updated if u.usual_id == existing.usual_id else u for u in person.usuals
            ]
        else:
            next_usuals = [
                *person.usuals,
                UsualWindow(
                    usual_id=usual_id_for_slot(person.person_id, int(weekday), start_m),
                    person_id=person.person_id,
                    label=label,
                    weekday=int(weekday),
                    start_minute=start_m,
                    end_minute=end_m,
                    evidence=evidence,
                    evidence_titles=titles,
                    status=BulletStatus.PENDING,
                ),
            ]
        person = person.model_copy(update={"usuals": next_usuals})
        by_id[person.person_id] = person

    people = list(by_id.values())
    person_by_summary = dict(event_person_hints)
    if previous:
        person_by_summary = {
            **previous.calendar_person_by_summary,
            **person_by_summary,
        }
    return people, person_by_summary


_GENERIC_PERSON_LABELS = frozenset(
    {
        "you",
        "me",
        "myself",
        "self",
        "child",
        "children",
        "kid",
        "kids",
        "elder",
        *(label.strip().lower() for label in CARE_ROLE_LABELS.values()),
    }
)
_ROLE_PEOPLE_IDS = (CareRoleId.CHILD_CARE, CareRoleId.ELDER_CARE)


def _relation_for(care: CareProfile, name: str, care_role_id: str) -> str:
    rels = care.person_relationships
    if name in rels and rels[name].strip():
        return rels[name].strip()[:48]
    key = _norm(name)
    for raw, val in rels.items():
        if _norm(raw) == key and val.strip():
            return val.strip()[:48]
    return "child" if care_role_id == CareRoleId.CHILD_CARE.value else "elder"


def hydrate_people_from_roles(care: CareProfile) -> CareProfile:
    """Copy child/elder names already on care roles into people_profiles.

    The graph reads ``CareRoleState.people``. Contacts reads ``people_profiles``.
    Older or heuristic profiles can have the first without the second. Does not
    invent names — only copies labels the Care Profile already holds.
    """
    people = list(care.people_profiles)
    added = False
    for role in care.roles:
        if role.status is BulletStatus.REJECTED:
            continue
        if role.role_id not in _ROLE_PEOPLE_IDS:
            continue
        care_role_id = role.role_id.value
        for raw in role.people:
            name = " ".join((raw or "").split())[:80]
            if not name or _norm(name) in _GENERIC_PERSON_LABELS:
                continue
            existing = find_person_by_name(people, name)
            if existing is not None:
                continue
            people.append(
                CarePerson(
                    display_name=name,
                    their_relation=_relation_for(care, name, care_role_id),
                    care_role_id=care_role_id,
                    status=BulletStatus.ACCEPTED,
                    contacts=seed_contacts(care_role_id),
                )
            )
            added = True
    if not added:
        return care
    rels = derive_person_relationships(people)
    return care.model_copy(
        update={
            "people_profiles": people,
            "person_relationships": {**care.person_relationships, **rels},
            "version": int(care.version or 1) + 1,
            "updated_at": _now_utc(),
        }
    )


def merge_series_usuals(
    care: CareProfile,
    events: list[dict[str, str | None]],
) -> CareProfile:
    """Lock repeating agenda slots onto Keep'd people. Does not invent people.

    Keep on the person is the lock — a clear series becomes an accepted usual.
    Rejected slots stay rejected.
    """
    series = series_usuals_from_agenda(care=care, events=events)
    if not series:
        return care
    by_id = {person.person_id: person for person in care.people_profiles}
    person_hints = dict(care.calendar_person_by_summary)
    changed = False
    for row in series:
        person = by_id.get(row.person_id)
        if person is None:
            continue
        slot = _usual_slot_key(person.person_id, row.weekday, row.start_minute)
        existing = next(
            (
                usual
                for usual in person.usuals
                if _usual_slot_key(usual.person_id, usual.weekday, usual.start_minute)
                == slot
            ),
            None,
        )
        if existing is not None and existing.status is BulletStatus.REJECTED:
            continue
        titles = list(
            dict.fromkeys(
                [*(existing.evidence_titles if existing else []), *row.evidence_titles]
            )
        )[:8]
        for title in titles:
            key = _norm(title)
            if key and person_hints.get(key) != person.person_id:
                person_hints[key] = person.person_id
                changed = True
        if existing is None:
            next_usuals = [
                *person.usuals,
                UsualWindow(
                    usual_id=usual_id_for_slot(
                        person.person_id, row.weekday, row.start_minute
                    ),
                    person_id=person.person_id,
                    label=row.label,
                    weekday=row.weekday,
                    start_minute=row.start_minute,
                    end_minute=row.end_minute,
                    evidence_titles=titles,
                    hit_count=row.hit_count,
                    last_seen_on=row.last_seen_on,
                    status=BulletStatus.ACCEPTED,
                ),
            ]
        elif (
            existing.status is BulletStatus.PENDING
            or titles != list(existing.evidence_titles)
            or existing.label != row.label
        ):
            status = (
                BulletStatus.ACCEPTED
                if existing.status is BulletStatus.PENDING
                else existing.status
            )
            next_usuals = [
                existing.model_copy(
                    update={
                        "label": row.label,
                        "end_minute": max(existing.end_minute, row.end_minute),
                        "evidence_titles": titles,
                        "hit_count": max(existing.hit_count, row.hit_count),
                        "last_seen_on": row.last_seen_on or existing.last_seen_on,
                        "status": status,
                    }
                )
                if usual.usual_id == existing.usual_id
                else usual
                for usual in person.usuals
            ]
        else:
            continue
        person = person.model_copy(update={"usuals": next_usuals})
        by_id[person.person_id] = person
        changed = True
    if not changed:
        return care
    return care.model_copy(
        update={
            "people_profiles": list(by_id.values()),
            "calendar_person_by_summary": person_hints,
            "version": int(care.version or 1) + 1,
            "updated_at": _now_utc(),
        }
    )


def attach_people_to_profile(
    profile: CareProfile,
    *,
    people: list[CarePerson],
    calendar_person_by_summary: dict[str, str],
) -> CareProfile:
    rels = derive_person_relationships(people)
    attached = profile.model_copy(
        update={
            "people_profiles": people,
            "calendar_person_by_summary": calendar_person_by_summary,
            "person_relationships": {**profile.person_relationships, **rels},
        }
    )
    return hydrate_people_from_roles(attached)


__all__ = [
    "attach_people_to_profile",
    "find_person_by_name",
    "hydrate_people_from_roles",
    "merge_people_and_usuals",
    "merge_series_usuals",
    "person_name_keys",
    "usual_id_for_slot",
]
