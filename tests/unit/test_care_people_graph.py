"""Care people inference + responsibilities graph."""

from __future__ import annotations

import pytest

from level_core.profile.synthesize import (
    adjust_care_profile_from_note,
    build_care_graph,
    extract_people_from_calendar_title,
    group_events_by_care_role,
    infer_care_profile,
    merge_people_into_care_profile,
    people_from_note,
    people_mentions_from_facts,
)
from level_core.schemas.care import CareProfile, CareRoleId, CareRoleState
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Fact, FactType


def test_extract_jordans_soccer() -> None:
    children, elders, partners = extract_people_from_calendar_title("Jordan's soccer practice")
    assert "Jordan" in children
    assert elders == []
    assert partners == []


def test_extract_pickup_with_jordan() -> None:
    children, _, _ = extract_people_from_calendar_title("Pickup with Jordan")
    assert "Jordan" in children


def test_extract_dash_name_still_works() -> None:
    children, _, _ = extract_people_from_calendar_title("School pickup — Maya")
    assert "Maya" in children


def test_unnamed_kid_cues_create_role_without_people() -> None:
    events = [
        {"summary": "School pickup", "start": "2026-08-11T15:00:00+00:00"},
        {"summary": "Soccer practice", "start": "2026-08-12T16:00:00+00:00"},
        {"summary": "Daycare drop-off", "start": "2026-08-13T08:00:00+00:00"},
        {"summary": "Standup", "start": "2026-08-11T09:00:00+00:00"},
    ]
    profile, _facts = infer_care_profile(events, user_id="u1")
    child = next(r for r in profile.roles if r.role_id is CareRoleId.CHILD_CARE)
    assert child.people == []
    assert "your kids" in (child.evidence_summaries[0] if child.evidence_summaries else "")


def test_jordans_pattern_fills_people_on_infer() -> None:
    events = [
        {"summary": "Jordan's soccer", "start": "2026-08-11T16:00:00+00:00"},
        {"summary": "Jordan's dentist", "start": "2026-08-12T10:00:00+00:00"},
        {"summary": "Pickup with Jordan", "start": "2026-08-13T15:00:00+00:00"},
    ]
    profile, _ = infer_care_profile(events, user_id="u1")
    child = next(r for r in profile.roles if r.role_id is CareRoleId.CHILD_CARE)
    assert "Jordan" in child.people


def test_coparent_people_from_handoff() -> None:
    events = [
        {"summary": "Handoff with Alex", "start": "2026-08-11T18:00:00+00:00"},
        {"summary": "School pickup", "start": "2026-08-12T15:00:00+00:00"},
        {"summary": "Soccer practice", "start": "2026-08-13T16:00:00+00:00"},
        {"summary": "Daycare drop-off", "start": "2026-08-14T08:00:00+00:00"},
    ]
    profile, _ = infer_care_profile(events, user_id="u1")
    partner = next(r for r in profile.roles if r.role_id is CareRoleId.PARTNER_COPARENT)
    assert "Alex" in partner.people


def test_relationship_fact_merges_people() -> None:
    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                salience=0.9,
                people=[],
                status=BulletStatus.PENDING,
            )
        ],
    )
    facts = [
        Fact(
            user_id="u1",
            type=FactType.RELATIONSHIP,
            statement="My daughter Maya has Thursday pickup after school.",
            salience=0.9,
        )
    ]
    mentions = people_mentions_from_facts(facts)
    assert CareRoleId.CHILD_CARE in mentions
    assert "Maya" in mentions[CareRoleId.CHILD_CARE]
    merged = merge_people_into_care_profile(profile, mentions)
    child = next(r for r in merged.roles if r.role_id is CareRoleId.CHILD_CARE)
    assert "Maya" in child.people


def test_note_adjust_writes_people() -> None:
    profile = CareProfile(user_id="u1", roles=[])
    updated = adjust_care_profile_from_note(profile, "My kid Jordan needs protected pickup time")
    child = next(r for r in updated.roles if r.role_id is CareRoleId.CHILD_CARE)
    assert "Jordan" in child.people
    named = people_from_note("Partner Alex can take Tuesday handoffs")
    assert "Alex" in named.get(CareRoleId.PARTNER_COPARENT, [])


def test_build_care_graph_star_with_helper() -> None:
    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                salience=0.92,
                people=["Jordan"],
                status=BulletStatus.ACCEPTED,
            ),
            CareRoleState(
                role_id=CareRoleId.ELDER_CARE,
                label="Elder care",
                salience=0.8,
                people=["Mom"],
                status=BulletStatus.ACCEPTED,
            ),
            CareRoleState(
                role_id=CareRoleId.PAID_WORK,
                    label="Work/Job",
                salience=0.85,
                status=BulletStatus.ACCEPTED,
            ),
            CareRoleState(
                role_id=CareRoleId.PARTNER_COPARENT,
                label="Co-parent / partner",
                salience=0.7,
                people=["Alex"],
                status=BulletStatus.PENDING,
            ),
        ],
        calendar_role_by_summary={
            "jordan's soccer": "child_care",
            "school pickup": "child_care",
            "standup": "paid_work",
            "mom visit": "elder_care",
            "work sync": "paid_work",
        },
    )
    events = [
        {"summary": "Jordan's soccer", "start": None},
        {"summary": "School pickup", "start": None},
        {"summary": "Standup", "start": None},
        {"summary": "Mom visit", "start": None},
        {"summary": "Work sync", "start": None},
    ]
    graph = build_care_graph(profile, events=events)
    assert graph is not None
    assert graph.center.label == "You"
    assert graph.center.color == "#3DB8A0"
    root_labels = {n.label for n in graph.roots}
    labels = {n.label for n in graph.nodes}
    assert "You" in root_labels
    assert "Alex" in root_labels  # co-parent is a caregiver root
    assert "Jordan" in labels
    assert "Mom" in labels
    assert "Work" in labels
    assert "Alex" not in labels  # not a dependent satellite
    jordan = next(n for n in graph.nodes if n.label == "Jordan")
    assert jordan.color == "#5B8EC9"
    assert jordan.event_count == 2  # soccer + pickup
    work = next(n for n in graph.nodes if n.label == "Work")
    assert work.color == "#5A7A8C"
    assert work.event_count >= 1
    assert all(e.color for e in graph.edges)
    assert any(e.role_id == "child_care" for e in graph.edges)
    relations = {(e.from_id, e.to_id, e.relation) for e in graph.edges}
    assert any(r == "holds" for _, _, r in relations)
    assert any(r == "can_help" for _, _, r in relations)
    helper = next(n for n in graph.roots if n.kind == "helper")
    assert helper.hint and "share" in helper.hint.lower()
    assert graph.categories
    assert any(c.role_id == "child_care" and c.event_count >= 2 for c in graph.categories)


def test_note_no_coparent_rejects_role() -> None:
    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.PARTNER_COPARENT,
                label="Co-parent / partner",
                salience=0.8,
                people=["Alex"],
                status=BulletStatus.PENDING,
            )
        ],
    )
    updated = adjust_care_profile_from_note(profile, "There's no co-parent — I'm a solo parent.")
    partner = next(r for r in updated.roles if r.role_id is CareRoleId.PARTNER_COPARENT)
    assert partner.status is BulletStatus.REJECTED
    assert partner.people == []
    assert partner.salience <= 0.2


def test_note_no_coparent_does_not_unreject() -> None:
    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.PARTNER_COPARENT,
                label="Co-parent / partner",
                salience=0.15,
                status=BulletStatus.REJECTED,
            )
        ],
    )
    # Affirmative-looking keyword must not undo Not me / "no co-parent".
    updated = adjust_care_profile_from_note(profile, "Partner handoff is not a thing for me")
    partner = next(r for r in updated.roles if r.role_id is CareRoleId.PARTNER_COPARENT)
    # "not a thing" isn't our negation regex; rejected status must still stick.
    assert partner.status is BulletStatus.REJECTED


def test_build_care_graph_omits_rejected_coparent() -> None:
    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                salience=0.9,
                people=["Jordan"],
                status=BulletStatus.ACCEPTED,
            ),
            CareRoleState(
                role_id=CareRoleId.PARTNER_COPARENT,
                label="Co-parent / partner",
                salience=0.1,
                people=["Alex"],
                status=BulletStatus.REJECTED,
            ),
        ],
    )
    events = [
        {"summary": "Handoff with Alex", "start": None},
        {"summary": "Jordan's soccer", "start": None},
        {"summary": "School pickup", "start": None},
    ]
    graph = build_care_graph(profile, events=events)
    assert graph is not None
    labels = {n.label for n in graph.nodes}
    assert "Jordan" in labels
    assert "Alex" not in labels
    assert "Co-parent" not in labels
    assert not any(n.kind == "helper" for n in graph.nodes)
    assert not any(c.role_id == "partner_coparent" for c in graph.categories)


def test_reconcile_exclusive_people_mom_stays_elder() -> None:
    from level_core.profile.care_infer_llm import reconcile_exclusive_people

    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                salience=0.95,
                people=["Jordan", "Mom"],
                status=BulletStatus.ACCEPTED,
            ),
            CareRoleState(
                role_id=CareRoleId.ELDER_CARE,
                label="Elder care",
                salience=0.8,
                people=["Mom"],
                status=BulletStatus.ACCEPTED,
            ),
        ],
    )
    fixed = reconcile_exclusive_people(profile)
    child = next(r for r in fixed.roles if r.role_id is CareRoleId.CHILD_CARE)
    elder = next(r for r in fixed.roles if r.role_id is CareRoleId.ELDER_CARE)
    assert "Mom" not in child.people
    assert "Jordan" in child.people
    assert "Mom" in elder.people
    graph = build_care_graph(fixed, events=[
        {"summary": "Pharmacy pickup — Mom's meds", "start": None},
        {"summary": "Jordan's soccer", "start": None},
    ])
    assert graph is not None
    mom_nodes = [n for n in graph.nodes if n.label == "Mom"]
    assert len(mom_nodes) == 1
    assert mom_nodes[0].kind == "elder"


def test_holistic_collapses_papa_dad_robert_aliases() -> None:
    """Calendar may say Papa / Dad / Robert — Care Profile keeps one elder."""
    from level_core.profile.care_infer_llm import (
        CareHolisticInfer,
        CarePersonAssign,
        CareRoleInfer,
        care_profile_from_holistic,
    )

    inferred = CareHolisticInfer(
        roles=[
            CareRoleInfer(
                role_id="elder_care",
                salience=0.9,
                weekly_load_hours=10,
                people=["Papa", "Dad", "Robert"],
                evidence="Dialysis and day program",
                present=True,
            ),
            CareRoleInfer(
                role_id="child_care",
                salience=0.85,
                weekly_load_hours=14,
                people=["Nova", "Theo"],
                evidence="Preschool and elementary",
                present=True,
            ),
        ],
        people=[
            CarePersonAssign(
                name="Robert",
                role="elder_care",
                evidence="Nephrology + dialysis titles",
                also_known_as=["Papa", "Dad", "Robert Chen"],
                relationship="parent",
            ),
            CarePersonAssign(
                name="Nova",
                role="child_care",
                evidence="BrightStart",
                relationship="child",
            ),
            CarePersonAssign(
                name="Theo",
                role="child_care",
                evidence="Westlake",
                relationship="child",
            ),
        ],
        events=[],
        conflicts=[],
        facts=[],
    )
    care, _ = care_profile_from_holistic(
        user_id="u1", inferred=inferred, previous=None, event_titles=[]
    )
    elder = next(r for r in care.roles if r.role_id is CareRoleId.ELDER_CARE)
    assert elder.people == ["Robert"]
    assert "Papa" not in elder.people
    assert "Dad" not in elder.people
    assert care.person_relationships.get("Robert") == "parent"
    assert care.person_relationships.get("Theo") == "child"
    graph = build_care_graph(care, events=[])
    assert graph is not None
    assert graph.center.shape == "star"
    assert any(r.id == "you" for r in graph.roots)
    robert = next(n for n in graph.nodes if n.label == "Robert")
    assert robert.shape == "circle"
    assert robert.relationship == "parent"
    theo = next(n for n in graph.nodes if n.label == "Theo")
    assert theo.relationship == "child"
    # Caregiver stars are roots — not mixed into dependent nodes.
    assert all((n.shape or "circle") != "star" for n in graph.nodes)


def test_apply_holistic_inference_exclusive_and_hints() -> None:
    from level_core.profile.care_infer_llm import (
        CareEventAssign,
        CareHolisticInfer,
        CarePersonAssign,
        CareRoleInfer,
        apply_holistic_inference,
    )

    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                salience=0.9,
                people=["Jordan", "Mom"],
                status=BulletStatus.PENDING,
            ),
            CareRoleState(
                role_id=CareRoleId.ELDER_CARE,
                label="Elder care",
                salience=0.7,
                people=["Mom"],
                status=BulletStatus.PENDING,
            ),
        ],
    )
    inferred = CareHolisticInfer(
        roles=[
            CareRoleInfer(
                role_id="child_care",
                salience=0.9,
                weekly_load_hours=12,
                people=["Jordan"],
                evidence="School and soccer with Jordan",
                present=True,
            ),
            CareRoleInfer(
                role_id="elder_care",
                salience=0.8,
                weekly_load_hours=6,
                people=["Mom"],
                evidence="Pharmacy and check-ins for Mom",
                present=True,
            ),
            CareRoleInfer(
                role_id="paid_work",
                salience=0.85,
                weekly_load_hours=40,
                people=[],
                evidence="Weekday standups",
                present=True,
            ),
        ],
        people=[
            CarePersonAssign(name="Jordan", role="child_care", evidence="soccer"),
            CarePersonAssign(name="Mom", role="elder_care", evidence="pharmacy meds"),
        ],
        events=[
            CareEventAssign(summary="Pharmacy pickup — Mom's meds", role="elder_care"),
            CareEventAssign(summary="Jordan's soccer", role="child_care"),
            CareEventAssign(summary="Standup", role="paid_work"),
        ],
        conflicts=["Work standups crowd out late soccer pickup"],
        facts=["I hold school and sports for Jordan", "I stay close with Mom"],
    )
    titles = [
        "Pharmacy pickup — Mom's meds",
        "Jordan's soccer",
        "Standup",
    ]
    updated = apply_holistic_inference(profile, inferred, event_titles=titles)
    child = next(r for r in updated.roles if r.role_id is CareRoleId.CHILD_CARE)
    elder = next(r for r in updated.roles if r.role_id is CareRoleId.ELDER_CARE)
    assert "Mom" not in child.people
    assert "Jordan" in child.people
    assert "Mom" in elder.people
    assert updated.calendar_role_by_summary["pharmacy pickup — mom's meds"] == "elder_care"
    assert updated.calendar_role_by_summary["jordan's soccer"] == "child_care"
    counts = group_events_by_care_role(
        [
            {"summary": "Pharmacy pickup — Mom's meds", "start": None},
            {"summary": "Jordan's soccer", "start": None},
            {"summary": "Standup", "start": None},
        ],
        role_by_summary=updated.calendar_role_by_summary,
    )
    assert counts[CareRoleId.ELDER_CARE] == 1
    assert counts[CareRoleId.CHILD_CARE] == 1
    assert counts[CareRoleId.PAID_WORK] == 1


@pytest.mark.asyncio
async def test_infer_care_holistic_uses_gemini() -> None:
    import json

    from level_core.models.fakes import FakeGeminiClient, ScriptedResponse
    from level_core.profile.care_infer_llm import infer_care_holistic

    payload = {
        "roles": [
            {
                "role_id": "child_care",
                "salience": 0.9,
                "weekly_load_hours": 10,
                "people": ["Jordan"],
                "evidence": "soccer",
                "present": True,
            },
            {
                "role_id": "elder_care",
                "salience": 0.8,
                "weekly_load_hours": 5,
                "people": ["Mom"],
                "evidence": "meds",
                "present": True,
            },
        ],
        "people": [
            {"name": "Jordan", "role": "child_care", "evidence": "soccer"},
            {"name": "Mom", "role": "elder_care", "evidence": "meds"},
        ],
        "events": [
            {"summary": "Pharmacy pickup — Mom's meds", "role": "elder_care"},
            {"summary": "Jordan's soccer", "role": "child_care"},
        ],
        "conflicts": [],
        "facts": ["I hold care for Jordan", "I check on Mom"],
    }
    gemini = FakeGeminiClient.scripted(
        [ScriptedResponse(text=json.dumps(payload))]
    )
    out = await infer_care_holistic(
        gemini,
        events=[
            {"summary": "Pharmacy pickup — Mom's meds", "start": None},
            {"summary": "Jordan's soccer", "start": None},
        ],
    )
    assert out is not None
    names = {p.name: p.role for p in out.people}
    assert names["Mom"] == "elder_care"
    assert names["Jordan"] == "child_care"


@pytest.mark.asyncio
async def test_apply_note_rejects_coparent_via_ai() -> None:
    import json

    from level_core.models.fakes import FakeGeminiClient, ScriptedResponse
    from level_core.profile.care_infer_llm import apply_note_to_care_profile_ai

    profile = CareProfile(
        user_id="u1",
        roles=[
            CareRoleState(
                role_id=CareRoleId.PARTNER_COPARENT,
                label="Co-parent / partner",
                salience=0.7,
                people=["Alex"],
                status=BulletStatus.PENDING,
            )
        ],
    )
    payload = {
        "reply": "Got it — you're solo on parenting. I dropped co-parent.",
        "reject_roles": ["partner_coparent"],
        "accept_roles": [],
        "people": [],
        "evidence": "no co-parent, just me",
        "conflicts": [],
    }
    gemini = FakeGeminiClient.scripted([ScriptedResponse(text=json.dumps(payload))])
    result = await apply_note_to_care_profile_ai(profile, "no co-parent, just me", gemini=gemini)
    assert result is not None
    care, reply = result
    partner = next(r for r in care.roles if r.role_id is CareRoleId.PARTNER_COPARENT)
    assert partner.status is BulletStatus.REJECTED
    assert "solo" in reply.lower() or "co-parent" in reply.lower()


def test_cached_care_graph_reuses_when_unchanged() -> None:
    from level_core.profile.synthesize import (
        cached_care_graph,
        invalidate_care_graph_cache,
    )

    invalidate_care_graph_cache("u1")
    profile = CareProfile(
        user_id="u1",
        version=3,
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                salience=0.9,
                people=["Jordan"],
                status=BulletStatus.ACCEPTED,
            )
        ],
        calendar_role_by_summary={"jordan's soccer": "child_care"},
    )
    events = [{"summary": "Jordan's soccer", "start": None}]
    g1, _p1, dirty1 = cached_care_graph(profile, events)
    assert g1 is not None and dirty1 is False
    g2, _p2, dirty2 = cached_care_graph(profile, events)
    assert dirty2 is False
    assert g2 is g1  # same cached object
    # Agenda change busts cache.
    g3, _p3, _d3 = cached_care_graph(
        profile,
        [
            {"summary": "Jordan's soccer", "start": None},
            {"summary": "Standup", "start": None},
        ],
    )
    assert g3 is not None
    assert g3 is not g1


def test_week_load_complete_ai_catalog() -> None:
    """Full AI catalog → multi-role composition."""
    from datetime import datetime, timezone

    from level_core.profile.synthesize import build_week_role_load
    from level_core.schemas.care import CareProfile

    now = datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc)
    care = CareProfile(
        user_id="u",
        calendar_role_by_summary={
            "jordan's soccer": "child_care",
            "meeting": "paid_work",
            "mom checkup": "elder_care",
        },
    )
    events = [
        {
            "summary": "Jordan's soccer",
            "start": "2026-08-11T17:00:00-07:00",
            "end": "2026-08-11T18:00:00-07:00",
            "all_day": False,
        },
        {
            "summary": "Meeting",
            "start": "2026-08-12T09:00:00-07:00",
            "end": "2026-08-12T10:00:00-07:00",
            "all_day": False,
        },
        {
            "summary": "Mom checkup",
            "start": "2026-08-13T11:00:00-07:00",
            "end": "2026-08-13T12:00:00-07:00",
            "all_day": False,
        },
    ]
    load = build_week_role_load(care, events, now=now)
    roles = {r["role_id"] for r in load}
    assert "child_care" in roles
    assert "paid_work" in roles
    assert "elder_care" in roles
    assert "uncategorized" not in roles
    assert sum(int(r["percent"]) for r in load) == 100
    assert max(int(r["percent"]) for r in load) < 100


def test_week_load_partial_catalog_not_100_percent_one_role() -> None:
    """Regression: only childcare tagged must NOT render as 100% childcare.

    Untagged week events stay visible as ``uncategorized`` so the bar reflects
    the whole week while background AI finishes the catalog.
    """
    from datetime import datetime, timezone

    from level_core.profile.synthesize import build_week_role_load
    from level_core.schemas.care import CareProfile

    now = datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc)
    care = CareProfile(
        user_id="u",
        calendar_role_by_summary={"jordan's soccer": "child_care"},
    )
    events = [
        {
            "summary": "Jordan's soccer",
            "start": "2026-08-11T17:00:00-07:00",
            "end": "2026-08-11T18:00:00-07:00",
            "all_day": False,
        },
        {
            "summary": "Meeting",
            "start": "2026-08-12T09:00:00-07:00",
            "end": "2026-08-12T10:00:00-07:00",
            "all_day": False,
        },
        {
            "summary": "Mom checkup",
            "start": "2026-08-13T11:00:00-07:00",
            "end": "2026-08-13T12:00:00-07:00",
            "all_day": False,
        },
    ]
    load = build_week_role_load(care, events, now=now)
    roles = {r["role_id"] for r in load}
    assert "child_care" in roles
    assert "uncategorized" in roles
    child = next(r for r in load if r["role_id"] == "child_care")
    assert int(child["percent"]) < 100
    assert sum(int(r["percent"]) for r in load) == 100
    assert max(int(r["percent"]) for r in load) < 100


def test_classify_and_group_calendar_events() -> None:
    from level_core.profile.synthesize import (
        classify_calendar_event,
        group_events_by_care_role,
    )

    # Offline heuristic classifier still exists for fallback/tests.
    assert classify_calendar_event("Jordan's soccer") is CareRoleId.CHILD_CARE
    assert classify_calendar_event("Standup with team") is CareRoleId.PAID_WORK
    assert classify_calendar_event("Meeting") is CareRoleId.PAID_WORK
    assert classify_calendar_event("Mom checkup") is CareRoleId.ELDER_CARE
    assert classify_calendar_event("Therapy session") is CareRoleId.SELF_RECOVERY
    # Offline fallback leaves classes uncategorized for holistic AI.
    assert classify_calendar_event("Night class") is None
    assert classify_calendar_event("Night class — career cert") is None
    assert classify_calendar_event("Evening course") is None
    # Without AI hints, grouping stays empty (AI catalog only).
    counts_empty = group_events_by_care_role(
        [
            {"summary": "Pickup", "start": None},
            {"summary": "Soccer", "start": None},
            {"summary": "1:1", "start": None},
        ]
    )
    assert counts_empty == {}
    # Untagged titles stay out — no regex gap-fill on the live path.
    counts_partial = group_events_by_care_role(
        [
            {"summary": "Pickup", "start": None},
            {"summary": "Soccer", "start": None},
            {"summary": "1:1", "start": None},
        ],
        role_by_summary={"pickup": "child_care"},
    )
    assert counts_partial == {CareRoleId.CHILD_CARE: 1}
    counts = group_events_by_care_role(
        [
            {"summary": "Pickup", "start": None},
            {"summary": "Soccer", "start": None},
            {"summary": "1:1", "start": None},
            {"summary": "Random coffee", "start": None},
        ],
        role_by_summary={
            "pickup": "child_care",
            "soccer": "child_care",
            "1:1": "paid_work",
        },
    )
    assert counts[CareRoleId.CHILD_CARE] == 2
    assert counts[CareRoleId.PAID_WORK] == 1
    assert CareRoleId.ELDER_CARE not in counts
