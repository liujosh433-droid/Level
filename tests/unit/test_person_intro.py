from __future__ import annotations

from level_core.schemas import CareRelation
from level_core.storage.care_store import parse_person_intro, relation_from_phrase


def test_alex_is_my_coparent_helper() -> None:
    parsed = parse_person_intro("Alex is my occasional co-parent helper")
    assert parsed == ("Alex", CareRelation.COPARENT)


def test_add_alex_as_coparent() -> None:
    parsed = parse_person_intro("Need to add a new role, Alex as co-parent")
    assert parsed == ("Alex", CareRelation.COPARENT)


def test_robert_is_kid_not_dad() -> None:
    parsed = parse_person_intro("Robert is my kid, not my dad")
    assert parsed == ("Robert", CareRelation.CHILD)


def test_nephew_not_child() -> None:
    parsed = parse_person_intro("Sam is my nephew, not my child")
    assert parsed == ("Sam", CareRelation.OTHER)


def test_unrelated_message_is_ignored() -> None:
    assert parse_person_intro("what's crowding this week?") is None
    assert parse_person_intro("prioritize elder care") is None


def test_relation_from_phrase_coparent_beats_parent() -> None:
    assert relation_from_phrase("occasional co-parent helper") is CareRelation.COPARENT
    assert relation_from_phrase("my dad") is CareRelation.ELDER
