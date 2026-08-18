"""Care people schema: self, kids, elder, co-parent."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CareRelation(StrEnum):
    SELF = "self"
    CHILD = "child"
    ELDER = "elder"
    COPARENT = "coparent"
    PARTNER = "partner"
    OTHER = "other"


class CareRoleId(StrEnum):
    """Higher-level care role bucket used for UI grouping."""

    SELF = "self"
    KIDS = "kids"
    ELDER_CARE = "elder_care"
    OTHERS = "others"


def role_for_relation(relation: CareRelation) -> CareRoleId:
    return {
        CareRelation.SELF: CareRoleId.SELF,
        CareRelation.CHILD: CareRoleId.KIDS,
        CareRelation.ELDER: CareRoleId.ELDER_CARE,
        CareRelation.COPARENT: CareRoleId.OTHERS,
        CareRelation.PARTNER: CareRoleId.OTHERS,
        CareRelation.OTHER: CareRoleId.OTHERS,
    }[relation]


class CarePerson(BaseModel):
    person_id: str
    display_name: str
    relation: CareRelation
    care_role_id: CareRoleId
    aliases: list[str] = Field(default_factory=list)
    is_self: bool = False
    status: str = "proposed"  # proposed | kept | not_me
    source_span: str | None = None
    version: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)
