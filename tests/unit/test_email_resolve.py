"""Resolve chat email asks to saved contacts without an LLM."""

from __future__ import annotations

import pytest
from level_core.email.drafter import DraftContext, fill_placeholders, template_draft
from level_core.email.resolve import (
    EmailCandidate,
    is_email_request,
    pick_candidate,
    resolve_email_targets,
)
from level_core.schemas import CarePerson, CareRelation, Contact, ContactKind
from level_core.schemas.care import role_for_relation
from level_core.storage.care_store import propose_person


def _person(person_id: str, name: str, aliases: list[str] | None = None) -> CarePerson:
    rel = CareRelation.CHILD
    return CarePerson(
        person_id=person_id,
        display_name=name,
        relation=rel,
        care_role_id=role_for_relation(rel),
        aliases=aliases or [],
        status="kept",
    )


def _contact(
    contact_id: str,
    person_id: str,
    name: str,
    kind: ContactKind = ContactKind.TEACHER,
    email: str | None = "teacher@example-school.org",
) -> Contact:
    return Contact(
        contact_id=contact_id,
        person_id=person_id,
        kind=kind,
        name=name,
        email=email,
    )


def test_is_email_request() -> None:
    assert is_email_request("email Nova's teacher and tell her that my kid is sick today")
    assert is_email_request("Send an email to the doctor")
    assert is_email_request("send nova's teacher a school sick note today")
    assert is_email_request("write Nova's teacher a note")
    assert is_email_request("message the coach")
    assert not is_email_request("book Tuesday 7:45-8:22 dropoff")
    assert not is_email_request("let me know if Tuesday works")
    assert not is_email_request("Nova is sick today")


def test_sick_note_phrasing_resolves_teacher() -> None:
    nova = _person("p_nova", "Nova")
    resolved = resolve_email_targets(
        "send nova's teacher a school sick note today",
        [nova],
        [_contact("c1", "p_nova", "Ms. Rivera")],
    )
    assert resolved.status == "match"
    assert resolved.candidates[0].contact.name == "Ms. Rivera"


def test_single_teacher_for_named_kid() -> None:
    nova = _person("p_nova", "Nova")
    resolved = resolve_email_targets(
        "email Nova's teacher and tell her that my kid is sick today",
        [nova],
        [_contact("c1", "p_nova", "Ms. Rivera")],
    )
    assert resolved.status == "match"
    assert resolved.candidates[0].contact.name == "Ms. Rivera"


def test_multiple_teachers_asks_which() -> None:
    nova = _person("p_nova", "Nova")
    resolved = resolve_email_targets(
        "email Nova's teacher",
        [nova],
        [
            _contact("c1", "p_nova", "Ms. Rivera"),
            _contact("c2", "p_nova", "Mr. Kim", email="kim@example-school.org"),
        ],
    )
    assert resolved.status == "ask"
    assert "Ms. Rivera" in resolved.reply
    assert "Mr. Kim" in resolved.reply


def test_pick_candidate_by_name() -> None:
    nova = _person("p_nova", "Nova")
    cands = [
        EmailCandidate(_contact("c1", "p_nova", "Ms. Rivera"), nova),
        EmailCandidate(_contact("c2", "p_nova", "Mr. Kim", email="kim@example.edu"), nova),
    ]
    picked = pick_candidate("Ms. Rivera", cands)
    assert picked is not None
    assert picked.contact.name == "Ms. Rivera"


def test_missing_teacher_points_at_contacts() -> None:
    nova = _person("p_nova", "Nova")
    resolved = resolve_email_targets("email Nova's teacher", [nova], [])
    assert resolved.status == "none"
    assert "Contacts" in resolved.reply


def test_history_resolves_her_teacher() -> None:
    nova = _person("p_nova", "Nova")
    resolved = resolve_email_targets(
        "email her teacher that she is sick today",
        [nova],
        [_contact("c1", "p_nova", "Ms. Rivera")],
        history=[{"role": "user", "text": "Nova stayed home this morning"}],
    )
    assert resolved.status == "match"
    assert resolved.candidates[0].contact.name == "Ms. Rivera"


@pytest.mark.asyncio
async def test_chat_drafts_when_one_teacher(store) -> None:  # type: ignore[no-untyped-def]
    from level_api.routes.chat import _handle_email_request

    nova = await propose_person(store, display_name="Nova", relation=CareRelation.CHILD)
    await store.contacts.upsert(_contact("c_riv", nova.person_id, "Ms. Rivera"))
    result = await _handle_email_request(
        store,
        "email Nova's teacher and tell her that my kid is sick today",
        [],
    )
    assert result["path"] == "email"
    draft = result.get("email_draft") or {}
    assert draft.get("contact_name") == "Ms. Rivera"
    assert draft.get("to") == "teacher@example-school.org"
    assert "Draft for" in result["reply"]
    assert "[" not in (draft.get("body") or "")
    assert "[" not in (draft.get("subject") or "")


@pytest.mark.asyncio
async def test_chat_asks_when_two_teachers(store) -> None:  # type: ignore[no-untyped-def]
    from level_api.routes.chat import _handle_email_request

    nova = await propose_person(store, display_name="Nova", relation=CareRelation.CHILD)
    await store.contacts.upsert(_contact("c1", nova.person_id, "Ms. Rivera"))
    await store.contacts.upsert(
        _contact("c2", nova.person_id, "Mr. Kim", email="kim@example-school.org")
    )
    result = await _handle_email_request(store, "email Nova's teacher", [])
    assert result.get("needs_confirm") is True
    assert "email_draft" not in result
    assert "Ms. Rivera" in result["reply"]
    assert "Mr. Kim" in result["reply"]


def test_fill_placeholders_uses_real_name_and_date() -> None:
    ctx = DraftContext(signer_name="Anna Mokkapati", today="Tuesday, August 18, 2026")
    filled = fill_placeholders(
        "Nova is out on [Current Date].\n\nSincerely,\n[Your name]",
        ctx,
    )
    assert "[Current Date]" not in filled
    assert "[Your name]" not in filled
    assert "Tuesday, August 18, 2026" in filled
    assert filled.endswith("Anna Mokkapati")


def test_template_draft_has_no_brackets() -> None:
    ctx = DraftContext(signer_name="Anna", today="Tuesday, August 18, 2026")
    draft = template_draft(
        contact_display_name="Ms. Mocha",
        kid_display_name="Nova",
        extra_notes="email Nova's teacher and tell her that my kid is sick today",
        ctx=ctx,
    )
    assert "[" not in draft.body
    assert "Anna" in draft.body
    assert "August 18, 2026" in draft.body
