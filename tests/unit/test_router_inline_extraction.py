"""Router inline extraction: skip the specialist LLM call when the
router already produced a clean structured extraction, and fall back
to the specialist agent otherwise.

Before this change, `profile/priority` (and `profile/person_update`,
`reminder/add_reminder`) always ran two LLM calls back-to-back:
    ChatRouterAgent -> PriorityAgent
    ChatRouterAgent -> PersonEditAgent
    ChatRouterAgent -> ReminderAgent

Under quota pressure both cycled through the AI Studio -> Vertex 2.5
-> Gemma fallback ladder, producing a ~30s tail.

Now the router can fill inline_priority / inline_person_edit /
inline_reminder in its single Flash call, and the dispatcher skips
the specialist agent entirely. These tests lock that behavior + the
fallback (specialist still runs when inline is null).

Tests drive the whole chat dispatcher (`_dispatch_message`) with
faked ChatRouterAgent responses, then assert:
  (a) side effect happened (priority/person/reminder written), AND
  (b) the specialist agent was NEVER called (queued response left
      untouched -> proves inline shortcut ran).
"""

from __future__ import annotations

import pytest
from level_core.agents.fakes import _fakes, clear_fakes, is_faked, register_fake
from level_core.agents.router_cache import clear_cache as clear_router_cache
from level_core.schemas import CareRelation
from level_core.storage.care_store import propose_person, set_person_status

from level_api.routes.chat import _dispatch_message


@pytest.fixture(autouse=True)
def _reset_router_cache_and_fakes() -> None:
    """Router cache is process-wide; wipe it between tests to avoid
    hits from other tests polluting the fake response queue."""
    clear_router_cache()
    clear_fakes()


# Test messages are deliberately worded to bypass EVERY regex fast
# path (chit-chat, empathy, agenda lookup, calendar CRUD, reminder
# lead, person intro, priority lead+body), so the dispatcher is
# forced onto the router-LLM code path we actually want to exercise.
_MSG_PRIORITY = "my mother's wellbeing sits at the top of my life these days"
_MSG_PERSON = "please switch alex to be a coparent for me now"
_MSG_REMINDER = "put umbrella for rainy days on my mental checklist"


@pytest.mark.asyncio
async def test_priority_inline_skips_priority_agent(store) -> None:  # type: ignore[no-untyped-def]
    """Router fills inline_priority -> dispatcher writes directly.

    PriorityAgent fake response stays queued (never popped)."""
    register_fake(
        "ChatRouterAgent",
        {
            "path": "profile",
            "intent": "priority",
            "source_span": "my mother's wellbeing",
            "confidence": 0.95,
            "inline_priority": {
                "text": "Mom's wellbeing",
                "weight": 5,
                "activity_types": ["family"],
                "source_span": "my mother's wellbeing",
            },
        },
    )
    # Register PriorityAgent fake so if the fallback WERE called we'd
    # know: the queue would get popped. It should NOT be called.
    register_fake(
        "PriorityAgent",
        {
            "priority": {
                "text": "Should never be used",
                "weight": 1,
                "activity_types": [],
                "source_span": "my mother's wellbeing",
            }
        },
    )

    result = await _dispatch_message(store, _MSG_PRIORITY, history=[])

    assert result["path"] == "profile"
    assert result["intent"] == "priority"
    assert "Mom's wellbeing" in result["reply"]
    priorities = await store.priorities.list()
    assert any(p.text == "Mom's wellbeing" and p.weight == 5 for p in priorities)
    # Fallback specialist was NOT called: its queued fake is still there.
    assert is_faked("PriorityAgent"), (
        "PriorityAgent fake should still be queued - inline shortcut "
        "should have skipped calling it"
    )


@pytest.mark.asyncio
async def test_priority_no_inline_falls_back_to_priority_agent(store) -> None:  # type: ignore[no-untyped-def]
    """Router leaves inline_priority null -> PriorityAgent runs as fallback."""
    register_fake(
        "ChatRouterAgent",
        {
            "path": "profile",
            "intent": "priority",
            "source_span": "my mother's wellbeing",
            "confidence": 0.65,
            # inline_priority intentionally omitted (router wasn't sure)
        },
    )
    register_fake(
        "PriorityAgent",
        {
            "priority": {
                "text": "Walks and family time",
                "weight": 4,
                "activity_types": ["family"],
                "source_span": "my mother's wellbeing",
            }
        },
    )

    result = await _dispatch_message(store, _MSG_PRIORITY, history=[])

    assert result["path"] == "profile"
    assert result["intent"] == "priority"
    # Fallback ran -> the PriorityAgent fake was consumed.
    assert not is_faked("PriorityAgent"), (
        "PriorityAgent fake should have been consumed by the fallback"
    )
    priorities = await store.priorities.list()
    assert any("Walks and family time" in p.text for p in priorities)


@pytest.mark.asyncio
async def test_person_edit_inline_skips_person_edit_agent(store) -> None:  # type: ignore[no-untyped-def]
    """Router fills inline_person_edit -> dispatcher applies directly."""
    register_fake(
        "ChatRouterAgent",
        {
            "path": "profile",
            "intent": "person_update",
            "source_span": "switch alex to be a coparent",
            "confidence": 0.95,
            "inline_person_edit": {
                "action": "add",
                "target_name": "Alex",
                "new_relation": CareRelation.COPARENT.value,
                "source_span": "switch alex to be a coparent",
            },
        },
    )
    register_fake(
        "PersonEditAgent",
        {
            "edit": {
                "action": "add",
                "target_name": "SHOULD-NOT-BE-USED",
                "new_relation": CareRelation.COPARENT.value,
                "source_span": "switch alex to be a coparent",
            }
        },
    )

    result = await _dispatch_message(store, _MSG_PERSON, history=[])

    assert result["path"] == "profile"
    assert result["intent"] == "person_update"
    people = await store.people.list()
    assert any(p.display_name.lower() == "alex" for p in people)
    assert is_faked("PersonEditAgent"), (
        "PersonEditAgent fake should still be queued"
    )


@pytest.mark.asyncio
async def test_reminder_inline_skips_reminder_agent(store) -> None:  # type: ignore[no-untyped-def]
    """Router fills inline_reminder -> dispatcher saves directly."""
    theo = await propose_person(
        store, display_name="Theo", relation=CareRelation.CHILD
    )
    await set_person_status(store, theo.person_id, "kept")

    register_fake(
        "ChatRouterAgent",
        {
            "path": "reminder",
            "intent": "add_reminder",
            "source_span": "umbrella for rainy days",
            "confidence": 0.9,
            "inline_reminder": {
                "text": "Bring soccer shoes",
                "person_display_name": "Theo",
                "activity_type": "sports.soccer",
                "lead_minutes": 60,
                "source_span": "umbrella for rainy days",
            },
        },
    )
    register_fake(
        "ReminderAgent",
        {
            "reminder": {
                "text": "SHOULD-NOT-BE-USED",
                "activity_type": "sports.soccer",
                "source_span": "umbrella for rainy days",
            }
        },
    )

    result = await _dispatch_message(store, _MSG_REMINDER, history=[])

    assert result["path"] == "reminder"
    assert result["intent"] == "add_reminder"
    reminders = await store.reminders.list()
    assert any(r.text == "Bring soccer shoes" for r in reminders)
    assert is_faked("ReminderAgent"), (
        "ReminderAgent fake should still be queued"
    )


@pytest.mark.asyncio
async def test_reminder_no_inline_falls_back_to_reminder_agent(store) -> None:  # type: ignore[no-untyped-def]
    """No inline_reminder -> ReminderAgent runs and its output is saved."""
    register_fake(
        "ChatRouterAgent",
        {
            "path": "reminder",
            "intent": "add_reminder",
            "source_span": "umbrella for rainy days",
            "confidence": 0.6,
        },
    )
    register_fake(
        "ReminderAgent",
        {
            "reminder": {
                "text": "Bring the charger",
                "activity_type": "work",
                "source_span": "umbrella for rainy days",
            }
        },
    )

    result = await _dispatch_message(store, _MSG_REMINDER, history=[])

    assert result["path"] == "reminder"
    reminders = await store.reminders.list()
    assert any("charger" in r.text.lower() for r in reminders)
    assert not is_faked("ReminderAgent"), "fallback should have consumed the fake"
    # Belt-and-braces: fakes registry is truly empty for ReminderAgent
    assert not _fakes.get("ReminderAgent")
