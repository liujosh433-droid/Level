"""Priority fast-path regex: LEAD form + BODY form coverage.

Priority statements come in two grammatical shapes in real caregiver
chat: a LEAD verb ("prioritize X") or a BODY phrase ("X takes
precedent"). Missing BODY was routing them to the router LLM +
PriorityAgent - two LLM calls, ~30s under quota pressure.

These tests lock the pattern coverage so future refactors don't
narrow it again.
"""

from __future__ import annotations

from level_api.routes.chat import _PRIORITY_BODY, _PRIORITY_LEAD


def _matches_priority(text: str) -> bool:
    return bool(_PRIORITY_LEAD.search(text) or _PRIORITY_BODY.search(text))


def test_lead_form_verbs() -> None:
    assert _matches_priority("prioritize elder care")
    assert _matches_priority("never miss Sunday physical therapy")
    assert _matches_priority("make sure to protect naptime")
    assert _matches_priority("please prioritize sports over work")
    assert _matches_priority("prefer walks over meetings")


def test_body_form_precedent() -> None:
    """The message that shipped this fix."""
    assert _matches_priority(
        "elder care with mom takes precedent over other activities"
    )
    assert _matches_priority(
        "elder care with mom takes precedence over other activities"
    )
    assert _matches_priority("Nova's checkup takes priority")


def test_body_form_natural_english() -> None:
    assert _matches_priority("kids pickup comes first")
    assert _matches_priority("family time matters more than work")
    assert _matches_priority("health matters most")
    assert _matches_priority("sports is the top priority")
    assert _matches_priority("Beta's therapy is the highest priority")
    assert _matches_priority("therapy is non-negotiable")
    assert _matches_priority("therapy is non negotiable")
    assert _matches_priority("family dinner no matter what")
    assert _matches_priority("nova above all else")
    assert _matches_priority("family trumps meetings")
    assert _matches_priority("family time is more important than work")


def test_priority_does_not_fire_on_unrelated_intents() -> None:
    assert not _matches_priority("hi")
    assert not _matches_priority("how are u")
    assert not _matches_priority("book Tuesday 2-3pm dentist")
    assert not _matches_priority("cancel Friday drop-off")
    assert not _matches_priority("what's on today")
    assert not _matches_priority("email Nova's teacher")
    assert not _matches_priority("remind me to bring the charger")
    assert not _matches_priority("I'm tired")
    assert not _matches_priority("move Thursday 3pm to Friday")
    # Fresh chit-chat that mentions priority-ish words but isn't:
    assert not _matches_priority("what do you consider important")
