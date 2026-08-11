"""Durable profile snapshot the user can review after messy-data ingest."""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from level_core.schemas.base import TraceableModel, _new_id


class BulletCategory(str, Enum):
    ROLE = "role"
    VALUE = "value"
    PRIORITY = "priority"
    COMMITMENT = "commitment"
    CONSTRAINT = "constraint"
    LOAD = "load"
    RELATIONSHIP = "relationship"
    CONTRADICTION = "contradiction"


class BulletStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"


class ProfileBullet(TraceableModel):
    bullet_id: str = Field(default_factory=_new_id)
    category: BulletCategory
    text: str = Field(min_length=8, max_length=400)
    status: BulletStatus = BulletStatus.PENDING
    source_fact_ids: list[str] = Field(default_factory=list)
    care_role_id: str | None = Field(
        default=None,
        description="CareRoleId when this bullet projects a caregiver role.",
    )


class Contradiction(TraceableModel):
    contradiction_id: str = Field(default_factory=_new_id)
    user_id: str
    topic: str = Field(max_length=80)
    fact_id_a: str
    fact_id_b: str
    summary: str = Field(min_length=10, max_length=400)
    status: BulletStatus = BulletStatus.PENDING


class ProfileSnapshot(TraceableModel):
    """Synthesized view of who the user is — for veto + agent grounding."""

    user_id: str
    bullets: list[ProfileBullet] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    needs_review: bool = True
    fact_count: int = 0


__all__ = [
    "BulletCategory",
    "BulletStatus",
    "Contradiction",
    "ProfileBullet",
    "ProfileSnapshot",
]
