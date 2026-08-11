"""Care Profile — caregiver role load model.

Level's twist: competing caregiver roles (not generic life domains). The
Care Profile is inferred from calendar patterns, mutated by Keep / Not me,
and is the primary grounding for role-theft challenges.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from level_core.schemas.base import TraceableModel, _new_id, _now_utc
from level_core.schemas.profile import BulletStatus


class CareRoleId(str, Enum):
    CHILD_CARE = "child_care"
    ELDER_CARE = "elder_care"
    PAID_WORK = "paid_work"
    SELF_RECOVERY = "self_recovery"
    HOUSEHOLD_LOGISTICS = "household_logistics"
    PARTNER_COPARENT = "partner_coparent"


# Display labels for UI + manifesto (caregiver-native language).
CARE_ROLE_LABELS: dict[CareRoleId, str] = {
    CareRoleId.CHILD_CARE: "Child care",
    CareRoleId.ELDER_CARE: "Elder care",
    CareRoleId.PAID_WORK: "Work/Job",
    CareRoleId.SELF_RECOVERY: "Self & recovery",
    CareRoleId.HOUSEHOLD_LOGISTICS: "Household logistics",
    CareRoleId.PARTNER_COPARENT: "Co-parent / partner",
}

# Stable hex colors for graph nodes + directed edges (by care role).
CARE_ROLE_COLORS: dict[CareRoleId, str] = {
    CareRoleId.CHILD_CARE: "#5B8EC9",
    CareRoleId.ELDER_CARE: "#B87AA0",
    CareRoleId.PAID_WORK: "#5A7A8C",
    CareRoleId.SELF_RECOVERY: "#6A9E78",
    CareRoleId.HOUSEHOLD_LOGISTICS: "#A09060",
    CareRoleId.PARTNER_COPARENT: "#D4A05A",
}
CARE_YOU_COLOR = "#3DB8A0"


class ProtectedWindow(TraceableModel):
    """A recurring or sticky time block tied to a care role (e.g. Thu pickup)."""

    window_id: str = Field(default_factory=_new_id)
    label: str = Field(max_length=120)
    # 0=Mon … 6=Sun when known; None = any / unknown weekday.
    weekday: int | None = Field(default=None, ge=0, le=6)
    start_hour: int | None = Field(default=None, ge=0, le=23)
    end_hour: int | None = Field(default=None, ge=0, le=23)
    evidence: str | None = Field(default=None, max_length=200)


class CareRoleState(TraceableModel):
    """One competing caregiver role and its current load / protection status."""

    role_id: CareRoleId
    label: str = Field(max_length=80)
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    weekly_load_hours: float = Field(default=0.0, ge=0.0, le=168.0)
    protected_windows: list[ProtectedWindow] = Field(default_factory=list)
    status: BulletStatus = BulletStatus.PENDING
    source_fact_ids: list[str] = Field(default_factory=list)
    evidence_summaries: list[str] = Field(default_factory=list)
    # Optional person anchors (Maya, Mom) for challenge specificity.
    people: list[str] = Field(default_factory=list)


class CareHelper(TraceableModel):
    """Someone who occasionally helps with a care load (friend, neighbor) — not a held role."""

    name: str = Field(max_length=80)
    helps: list[str] = Field(
        default_factory=list,
        description="People they help with (e.g. Jordan).",
    )
    hint: str = Field(
        default="Occasionally helps with care",
        max_length=160,
    )
    # CareRoleId value for coloring / which load they share (usually child_care).
    helps_role: str = Field(default="child_care", max_length=40)


class CareProfile(TraceableModel):
    """Living care-load snapshot for one caregiver."""

    user_id: str
    roles: list[CareRoleState] = Field(default_factory=list)
    version: int = 1
    updated_at: datetime = Field(default_factory=_now_utc)
    conflict_summaries: list[str] = Field(
        default_factory=list,
        description="Role-theft tensions inferred from overlapping loads/windows.",
    )
    # Holistic LLM classification: normalized calendar title → CareRoleId value.
    calendar_role_by_summary: dict[str, str] = Field(
        default_factory=dict,
        description="AI-inferred care-role category per calendar title (for graph grouping).",
    )
    # Occasional helpers (friends/neighbors) — edges point at care recipients, not You→holds.
    helpers: list[CareHelper] = Field(default_factory=list)


class CareGraphNode(TraceableModel):
    """One node in the Profile responsibilities graph."""

    id: str
    label: str = Field(max_length=80)
    kind: str = Field(
        description="you | child | elder | work | recovery | logistics | helper | domain",
    )
    hint: str | None = Field(default=None, max_length=160)
    role_id: str | None = Field(
        default=None,
        description="CareRoleId value when this node maps to a care role.",
    )
    color: str = Field(default="#8aa4b0", max_length=16)
    event_count: int = Field(default=0, ge=0)


class CareGraphEdge(TraceableModel):
    """Directed edge: You holds X, or helper can_help child."""

    from_id: str
    to_id: str
    relation: str = Field(description="holds | carries | can_help | coordinates")
    role_id: str | None = Field(default=None)
    color: str = Field(default="#8aa4b0", max_length=16)


class CareGraphCategory(TraceableModel):
    """Calendar events grouped into a care-role category for the legend."""

    role_id: str
    label: str
    color: str
    event_count: int = Field(default=0, ge=0)


class CareGraph(TraceableModel):
    """Star graph of responsibilities derived from a Care Profile + calendar."""

    center: CareGraphNode
    nodes: list[CareGraphNode] = Field(default_factory=list)
    edges: list[CareGraphEdge] = Field(default_factory=list)
    categories: list[CareGraphCategory] = Field(default_factory=list)


def active_care_roles(profile: CareProfile | None) -> list[CareRoleState]:
    """Roles Retriever/Challenger may cite (not rejected)."""
    if profile is None:
        return []
    return [
        r
        for r in profile.roles
        if r.status is not BulletStatus.REJECTED and r.salience >= 0.35
    ]


def care_profile_snippet(profile: CareProfile | None, *, max_chars: int = 800) -> str:
    """Compact grounding block for agents."""
    roles = active_care_roles(profile)
    if not roles:
        return ""
    lines: list[str] = ["Care roles you hold (caregiver load):"]
    for role in sorted(roles, key=lambda r: r.salience, reverse=True)[:6]:
        people = f" ({', '.join(role.people)})" if role.people else ""
        windows = ""
        if role.protected_windows:
            w = role.protected_windows[0]
            windows = f" — e.g. {w.label}"
        lines.append(
            f"- {role.label}{people}: salience {role.salience:.2f}, "
            f"~{role.weekly_load_hours:.0f}h/wk{windows}"
        )
    if profile and profile.conflict_summaries:
        lines.append("Role conflicts:")
        for c in profile.conflict_summaries[:3]:
            lines.append(f"- {c}")
    text = "\n".join(lines)
    return text[:max_chars]


__all__ = [
    "CARE_ROLE_COLORS",
    "CARE_ROLE_LABELS",
    "CARE_YOU_COLOR",
    "CareGraph",
    "CareGraphCategory",
    "CareGraphEdge",
    "CareGraphNode",
    "CareHelper",
    "CareProfile",
    "CareRoleId",
    "CareRoleState",
    "ProtectedWindow",
    "active_care_roles",
    "care_profile_snippet",
]
