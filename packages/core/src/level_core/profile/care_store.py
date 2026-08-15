"""Single write path for the Care Profile.

Routes and jobs mutate through ``apply_care`` / ``save_care``. Keep / Not me
still wins inside the mutators; this module only versions the document and
derives role.people + person_relationships from ``people_profiles``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import TypeAlias

from level_core.errors import ConflictError
from level_core.memory.base import MemoryBank
from level_core.observability.logger import get_logger
from level_core.profile.people_usuals import merge_series_usuals
from level_core.profile.care_graph import invalidate_care_graph_cache
from level_core.schemas.care import (
    CareProfile,
    derive_person_relationships,
    held_care_people,
)

_logger = get_logger(__name__)

CareMutator: TypeAlias = Callable[
    [CareProfile], CareProfile | Awaitable[CareProfile]
]


def derive_care_views(care: CareProfile) -> CareProfile:
    """Keep graph maps in sync with people_profiles. Does not invent people."""
    rels = {**care.person_relationships, **derive_person_relationships(care.people_profiles)}
    names_by_role: dict[str, list[str]] = {}
    for person in held_care_people(care):
        rid = (person.care_role_id or "").strip().lower()
        if not rid:
            continue
        bucket = names_by_role.setdefault(rid, [])
        if person.display_name not in bucket:
            bucket.append(person.display_name)
    roles = []
    roles_changed = False
    for role in care.roles:
        rid = role.role_id.value if hasattr(role.role_id, "value") else str(role.role_id)
        names = names_by_role.get(str(rid).lower())
        if names and list(role.people) != names:
            roles.append(role.model_copy(update={"people": names}))
            roles_changed = True
        else:
            roles.append(role)
    if rels == care.person_relationships and not roles_changed:
        return care
    return care.model_copy(
        update={
            "person_relationships": rels,
            "roles": roles if roles_changed else care.roles,
        }
    )


async def load_care(memory: MemoryBank, user_id: str) -> CareProfile | None:
    return await memory.manifestos.get_care_profile(user_id=user_id)


async def save_care(
    memory: MemoryBank,
    profile: CareProfile,
    *,
    expected_version: int | None,
) -> CareProfile:
    """Persist ``profile`` if the stored version still matches ``expected_version``.

    ``expected_version is None`` means create — fails if a profile already exists.
    """
    stored = await load_care(memory, profile.user_id)
    if stored is None:
        if expected_version is not None:
            raise ConflictError("care_profile missing")
    elif expected_version is None or stored.version != expected_version:
        raise ConflictError("care_profile version conflict")
    profile = derive_care_views(profile)
    invalidate_care_graph_cache(profile.user_id)
    await memory.manifestos.save_care_profile(profile)
    return profile


async def apply_care(
    memory: MemoryBank,
    user_id: str,
    mutator: CareMutator,
    *,
    retries: int = 1,
) -> CareProfile | None:
    """Load, mutate, version-checked save. Retries once on conflict.

    Returns None when there is no Care Profile (mutator is not used to create).
    No-op mutators (same version) do not write.
    """
    care = await load_care(memory, user_id)
    if care is None:
        return None
    attempts = max(1, retries + 1)
    last_error: ConflictError | None = None
    for _ in range(attempts):
        expected = int(care.version or 1)
        next_care = mutator(care)
        if isawaitable(next_care):
            next_care = await next_care
        if int(next_care.version or 1) == expected:
            return care
        try:
            return await save_care(
                memory, next_care, expected_version=expected
            )
        except ConflictError as exc:
            last_error = exc
            care = await load_care(memory, user_id)
            if care is None:
                return None
            _logger.info("care_apply_retry", user_id=user_id)
    raise last_error or ConflictError("care_profile version conflict")


async def apply_care_or_create(
    memory: MemoryBank,
    user_id: str,
    mutator: CareMutator,
) -> CareProfile:
    """Like ``apply_care``, but creates an empty profile when none exists."""
    care = await apply_care(memory, user_id, mutator)
    if care is not None:
        return care
    created = CareProfile(user_id=user_id)
    next_care = mutator(created)
    if isawaitable(next_care):
        next_care = await next_care
    try:
        return await save_care(memory, next_care, expected_version=None)
    except ConflictError:
        applied = await apply_care(memory, user_id, mutator)
        if applied is None:
            raise ConflictError("care_profile create raced")
        return applied


async def apply_series_usuals(
    memory: MemoryBank,
    user_id: str,
    events: list[dict[str, str | None]],
) -> CareProfile | None:
    """Lock repeating agenda slots onto Keep'd people and persist if needed."""
    return await apply_care(
        memory, user_id, lambda care: merge_series_usuals(care, events)
    )


__all__ = [
    "apply_care",
    "apply_care_or_create",
    "apply_series_usuals",
    "derive_care_views",
    "load_care",
    "save_care",
]
