"""Care Profile write spine — versioned save, derive views, no invent."""

from __future__ import annotations

import pytest

from level_core.errors import ConflictError
from level_core.memory.fakes import build_in_memory_bank
from level_core.profile.care_store import (
    apply_care,
    apply_series_usuals,
    derive_care_views,
    save_care,
)
from level_core.profile.people_usuals import merge_series_usuals, usual_id_for_slot
from level_core.schemas.care import (
    CarePerson,
    CareProfile,
    CareRoleId,
    CareRoleState,
)
from level_core.schemas.profile import BulletStatus


def _child(name: str, person_id: str) -> CarePerson:
    return CarePerson(
        person_id=person_id,
        display_name=name,
        care_role_id="child_care",
        status=BulletStatus.ACCEPTED,
        their_relation="child",
        your_role="parent",
    )


@pytest.mark.asyncio
async def test_save_rejects_stale_version() -> None:
    memory = build_in_memory_bank()
    care = CareProfile(user_id="u-test", version=1)
    await save_care(memory, care, expected_version=None)
    await save_care(
        memory,
        care.model_copy(update={"version": 2}),
        expected_version=1,
    )
    with pytest.raises(ConflictError):
        await save_care(
            memory,
            care.model_copy(update={"version": 3, "conflict_summaries": ["y"]}),
            expected_version=1,
        )
    with pytest.raises(ConflictError):
        await save_care(
            memory, CareProfile(user_id="u-test", version=1), expected_version=None
        )


@pytest.mark.asyncio
async def test_apply_retries_after_conflict() -> None:
    memory = build_in_memory_bank()
    await save_care(
        memory, CareProfile(user_id="u-test", version=1), expected_version=None
    )
    calls = {"n": 0}

    async def _bump(current: CareProfile) -> CareProfile:
        calls["n"] += 1
        if calls["n"] == 1:
            await save_care(
                memory,
                current.model_copy(update={"version": int(current.version) + 1}),
                expected_version=current.version,
            )
        return current.model_copy(
            update={
                "version": int(current.version) + 1,
                "conflict_summaries": ["kept"],
            }
        )

    out = await apply_care(memory, "u-test", _bump)
    assert out is not None
    assert out.conflict_summaries == ["kept"]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_apply_noop_does_not_write() -> None:
    memory = build_in_memory_bank()
    care = CareProfile(user_id="u-test", version=4)
    await save_care(memory, care, expected_version=None)
    out = await apply_care(memory, "u-test", lambda current: current)
    assert out is not None
    assert out.version == 4


def test_derive_role_people_from_keepd_profiles() -> None:
    care = CareProfile(
        user_id="u-test",
        roles=[
            CareRoleState(
                role_id=CareRoleId.CHILD_CARE,
                label="Child care",
                people=["Old label"],
            )
        ],
        people_profiles=[_child("Alpha", "p-a"), _child("Beta", "p-b")],
    )
    derived = derive_care_views(care)
    child_role = next(r for r in derived.roles if r.role_id is CareRoleId.CHILD_CARE)
    assert child_role.people == ["Alpha", "Beta"]
    assert derived.person_relationships["Alpha"] == "child"


@pytest.mark.asyncio
async def test_series_usual_id_is_stable_across_persist() -> None:
    memory = build_in_memory_bank()
    care = CareProfile(
        user_id="u-test",
        people_profiles=[_child("Alpha", "p-a")],
        version=1,
    )
    await save_care(memory, care, expected_version=None)
    from datetime import datetime, timezone

    when_a = datetime(2026, 8, 6, 22, 0, tzinfo=timezone.utc)
    when_b = datetime(2026, 8, 13, 22, 0, tzinfo=timezone.utc)
    events = [
        {"summary": "Alpha window", "start": when_a.isoformat(), "status": "confirmed"},
        {"summary": "Alpha window", "start": when_b.isoformat(), "status": "confirmed"},
    ]
    projected = merge_series_usuals(care, events)
    persisted = await apply_series_usuals(memory, "u-test", events)
    assert persisted is not None
    proj_id = projected.people_profiles[0].usuals[0].usual_id
    saved_id = persisted.people_profiles[0].usuals[0].usual_id
    assert proj_id == saved_id
    assert proj_id == usual_id_for_slot("p-a", 3, 15 * 60)
