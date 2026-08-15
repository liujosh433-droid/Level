"""Usual-gap arithmetic — constructed people only, no production name literals."""

from __future__ import annotations

from datetime import date, datetime, timezone

from level_core.calendar.school import (
    events_for_person_on_date,
    match_contacts_by_role,
    match_people_by_names,
    school_send_target,
)
from level_core.calendar.usuals import apply_usual_resolution, find_usual_gaps
from level_core.profile.people_usuals import hydrate_people_from_roles
from level_core.schemas.care import (
    CareContact,
    CarePerson,
    CareProfile,
    CareRoleId,
    CareRoleState,
    SchoolAnchor,
    UsualWindow,
    default_contact_roles,
    ensure_self_care_person,
    held_care_people,
    locked_usuals,
)
from level_core.schemas.profile import BulletStatus


def _person(
    *,
    name: str,
    person_id: str,
    weekday: int,
    start_minute: int = 15 * 60,
    status: BulletStatus = BulletStatus.ACCEPTED,
    usual_status: BulletStatus = BulletStatus.ACCEPTED,
    exceptions: list[str] | None = None,
) -> CarePerson:
    return CarePerson(
        person_id=person_id,
        display_name=name,
        your_role="parent",
        their_relation="child",
        care_role_id="child_care",
        status=status,
        usuals=[
            UsualWindow(
                usual_id=f"u-{person_id}",
                person_id=person_id,
                label=f"{name} window",
                weekday=weekday,
                start_minute=start_minute,
                end_minute=start_minute + 60,
                status=usual_status,
                exceptions=exceptions or [],
                evidence_titles=[f"{name} window"],
            )
        ],
    )


def _care(*people: CarePerson) -> CareProfile:
    return CareProfile(
        user_id="u-test",
        people_profiles=list(people),
        calendar_person_by_summary={
            f"{p.display_name.lower()} window": p.person_id for p in people
        },
    )


def _event(summary: str, when: datetime, *, person_id: str | None = None) -> dict:
    row: dict[str, str | None] = {
        "id": f"ev-{summary}",
        "summary": summary,
        "start": when.isoformat(),
        "status": "confirmed",
    }
    if person_id:
        row["person_id"] = person_id
    return row


class TestUsualGaps:
    def test_only_missing_person_gaps(self) -> None:
        # Thursday 2026-08-13
        day = date(2026, 8, 13)
        when = datetime(2026, 8, 13, 22, 0, tzinfo=timezone.utc)  # 15:00 PT
        alpha = _person(name="Alpha", person_id="p-a", weekday=3)
        beta = _person(name="Beta", person_id="p-b", weekday=3)
        care = _care(alpha, beta)
        events = [_event("Beta window", when, person_id="p-b")]
        gaps = find_usual_gaps(care=care, events=events, on_dates=[day])
        assert [g.person_id for g in gaps] == ["p-a"]
        assert gaps[0].usual_id == "u-p-a"
        assert "Alpha" in gaps[0].banner()
        assert "Beta" not in gaps[0].banner()

    def test_exception_suppresses_that_instance(self) -> None:
        day = date(2026, 8, 13)
        alpha = _person(
            name="Alpha",
            person_id="p-a",
            weekday=3,
            exceptions=["2026-08-13"],
        )
        beta = _person(name="Beta", person_id="p-b", weekday=3)
        care = _care(alpha, beta)
        gaps = find_usual_gaps(care=care, events=[], on_dates=[day])
        assert [g.person_id for g in gaps] == ["p-b"]

    def test_not_me_on_one_does_not_kill_the_other(self) -> None:
        day = date(2026, 8, 13)
        alpha = _person(name="Alpha", person_id="p-a", weekday=3)
        beta = _person(name="Beta", person_id="p-b", weekday=3)
        care = apply_usual_resolution(_care(alpha, beta), usual_id="u-p-a", action="not_me")
        gaps = find_usual_gaps(care=care, events=[], on_dates=[day])
        assert [g.person_id for g in gaps] == ["p-b"]
        rejected = next(u for p in care.people_profiles for u in p.usuals if u.usual_id == "u-p-a")
        assert rejected.status is BulletStatus.REJECTED

    def test_pending_usual_does_not_gap(self) -> None:
        day = date(2026, 8, 13)
        alpha = _person(
            name="Alpha",
            person_id="p-a",
            weekday=3,
            usual_status=BulletStatus.PENDING,
        )
        gaps = find_usual_gaps(care=_care(alpha), events=[], on_dates=[day])
        assert gaps == []

    def test_keep_then_gap(self) -> None:
        day = date(2026, 8, 13)
        alpha = _person(
            name="Alpha",
            person_id="p-a",
            weekday=3,
            usual_status=BulletStatus.PENDING,
        )
        care = apply_usual_resolution(_care(alpha), usual_id="u-p-a", action="keep")
        gaps = find_usual_gaps(care=care, events=[], on_dates=[day])
        assert len(gaps) == 1
        assert gaps[0].person_id == "p-a"


class TestSchoolMatch:
    def test_match_by_alias_not_literal(self) -> None:
        person = CarePerson(
            person_id="p-1",
            display_name="Gamma",
            aliases=["G"],
            school=SchoolAnchor(attendance_email="office@school.example"),
        )
        care = CareProfile(user_id="u-test", people_profiles=[person])
        found = match_people_by_names(care, ["g"])
        assert [p.person_id for p in found] == ["p-1"]
        email, _ = school_send_target(found[0])
        assert email == "office@school.example"

    def test_match_teacher_role_from_spoken_phrase(self) -> None:
        person = CarePerson(
            person_id="p-1",
            display_name="Gamma",
            contacts=[
                CareContact(role="Teacher", name="Ms. Lane", email="t@school.example"),
                CareContact(role="Doctor", name="Dr. Ng", email="d@clinic.example"),
            ],
        )
        hits = match_contacts_by_role(person, "her teacher")
        assert [c.email for c in hits] == ["t@school.example"]
        email, label = school_send_target(person, role="teacher")
        assert email == "t@school.example"
        assert "Lane" in label

    def test_teacher_note_is_courteous(self) -> None:
        from level_core.calendar.school import draft_contact_note

        person = CarePerson(person_id="p-1", display_name="Gamma")
        contact = CareContact(role="Teacher", name="Ms. Lane", email="t@school.example")
        subj, body = draft_contact_note(
            person=person,
            on_date=date(2026, 8, 13),
            contact=contact,
            body="she's sick",
            from_name="Parent Example",
        )
        assert "Gamma" in subj
        assert "Dear Ms. Lane" in body
        assert "please excuse" in body.lower()
        assert "understanding" in body.lower()
        assert "she's sick" not in body.lower()
        assert "Best," in body
        assert "Parent" in body

    def test_cancel_only_tagged_person_events(self) -> None:
        day = date(2026, 8, 13)
        when = datetime(2026, 8, 13, 22, 0, tzinfo=timezone.utc)
        alpha = _person(name="Alpha", person_id="p-a", weekday=3)
        beta = _person(name="Beta", person_id="p-b", weekday=3)
        care = _care(alpha, beta)
        events = [
            _event("Alpha window", when, person_id="p-a") | {"id": "ev-a"},
            _event("Beta window", when, person_id="p-b") | {"id": "ev-b"},
        ]
        mine = events_for_person_on_date(
            care=care, events=events, person=alpha, on_date=day
        )
        assert [e.get("id") for e in mine] == ["ev-a"]


def test_contact_defaults_by_role() -> None:
    assert default_contact_roles("child_care") == ["Teacher", "Doctor"]
    assert default_contact_roles("elder_care") == ["Doctor"]
    assert default_contact_roles("self") == ["Doctor"]
    care = CareProfile(user_id="u-test")
    care, self_row = ensure_self_care_person(care, "Parent Example")
    care, again = ensure_self_care_person(care, "Parent Example")
    assert again.person_id == self_row.person_id
    assert self_row.care_role_id == "self"
    assert [c.role for c in self_row.contacts] == ["Doctor"]
    assert held_care_people(care) == []
    kid = CarePerson(
        display_name="Kid Example",
        care_role_id="child_care",
        status=BulletStatus.ACCEPTED,
    )
    care = care.model_copy(update={"people_profiles": [*care.people_profiles, kid]})
    assert [p.display_name for p in held_care_people(care)] == ["Kid Example"]


def test_self_usuals_do_not_gap() -> None:
    care, self_row = ensure_self_care_person(CareProfile(user_id="u-test"), "Parent Example")
    usual = UsualWindow(
        usual_id="u-self",
        person_id=self_row.person_id,
        label="clinic",
        weekday=0,
        status=BulletStatus.ACCEPTED,
    )
    self_row = self_row.model_copy(update={"usuals": [usual]})
    care = care.model_copy(update={"people_profiles": [self_row]})
    assert locked_usuals(care) == []


def test_hydrate_people_from_care_roles() -> None:
    care = CareProfile(
        user_id="u-test",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                salience=0.9,
                people=["Alpha", "Beta"],
            ),
            CareRoleState(
                role_id=CareRoleId.ELDER_CARE,
                label="Elder care",
                salience=0.8,
                people=["Gamma"],
            ),
        ],
        person_relationships={"Alpha": "child", "Gamma": "parent"},
    )
    next_care = hydrate_people_from_roles(care)
    by_name = {p.display_name: p for p in held_care_people(next_care)}
    assert set(by_name) == {"Alpha", "Beta", "Gamma"}
    assert by_name["Alpha"].care_role_id == "child_care"
    assert by_name["Gamma"].care_role_id == "elder_care"
    assert [c.role for c in by_name["Alpha"].contacts] == ["Teacher", "Doctor"]
    assert [c.role for c in by_name["Gamma"].contacts] == ["Doctor"]
    assert by_name["Alpha"].their_relation == "child"
    assert by_name["Gamma"].their_relation == "parent"
    again = hydrate_people_from_roles(next_care)
    assert again.version == next_care.version
    generic = CareProfile(
        user_id="u-test",
        roles=[
            CareRoleState(
                role_id=CareRoleId.ELDER_CARE,
                label="Elder care",
                salience=0.8,
                people=["Elder care"],
            )
        ],
    )
    assert hydrate_people_from_roles(generic).people_profiles == []
