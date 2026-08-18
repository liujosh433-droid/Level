"""Care Profile — caregiver role load model.

Level's twist: competing caregiver roles (not generic life domains). The
Care Profile is inferred from calendar patterns, mutated by Keep / Not me,
and is the primary grounding for role-theft challenges.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum

from pydantic import Field

from level_core.schemas.base import TraceableModel, _new_id, _now_utc
from level_core.schemas.profile import BulletStatus

# Drop meta, retrospective, or non-actionable conflict blurbs.
_VAGUE_CONFLICT = re.compile(
    r"(indicates?(?:\s+that)?|other obligations|level is watching|"
    r"\bcollision day\b|watch for care collisions|tension level|"
    r"conflict with other|potential conflict|may indicate|"
    r"creating a conflict|previously scheduled|were previously|"
    r"were scheduled|has been scheduled|have been scheduled|"
    r"\bpaid_work\b|\bchild_care\b|\belder_care\b|\bself_recovery\b|"
    r"\bhousehold_logistics\b|\bpartner_coparent\b)",
    re.IGNORECASE,
)
_CONFLICT_PREFIX = re.compile(
    r"^(?:tension|care collision|conflict|heads up)\s*[:—\-]\s*",
    re.IGNORECASE,
)


def clean_conflict_summaries(items: list[str] | None) -> list[str]:
    """Keep short, actionable conflict lines; drop observations and jargon."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in items or []:
        s = _CONFLICT_PREFIX.sub("", " ".join((raw or "").strip().split()))
        if len(s) < 16 or _VAGUE_CONFLICT.search(s):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:200])
        if len(out) >= 4:
            break
    return out


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

# Contacts page "You" person — not a CareRoleId graph role.
SELF_CARE_ROLE = "self"


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


class SchoolAnchor(TraceableModel):
    """Institutional school contacts for one care person — never a friend roster."""

    attendance_email: str = Field(default="", max_length=200)
    teacher_email: str = Field(default="", max_length=200)
    teacher_label: str = Field(default="", max_length=80)


class CareContact(TraceableModel):
    """A named role the caregiver can email — teacher, doctor, or a type they add."""

    contact_id: str = Field(default_factory=_new_id)
    role: str = Field(min_length=1, max_length=40, description="Teacher, Doctor, …")
    name: str = Field(default="", max_length=80)
    email: str = Field(default="", max_length=200)


class UsualWindow(TraceableModel):
    """A locked or proposed repeating obligation owned by one CarePerson."""

    usual_id: str = Field(default_factory=_new_id)
    person_id: str = Field(min_length=1, max_length=64)
    label: str = Field(max_length=120)
    weekday: int = Field(ge=0, le=6)
    start_minute: int = Field(default=15 * 60, ge=0, le=24 * 60)
    end_minute: int = Field(default=16 * 60, ge=0, le=24 * 60)
    hit_count: int = Field(default=0, ge=0)
    miss_count: int = Field(default=0, ge=0)
    last_seen_on: str | None = Field(
        default=None,
        description="YYYY-MM-DD of last matching instance.",
    )
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    evidence: str = Field(default="", max_length=240)
    evidence_titles: list[str] = Field(
        default_factory=list,
        description="Event titles the model already assigned as instances.",
    )
    exceptions: list[str] = Field(
        default_factory=list,
        description="YYYY-MM-DD dates marked 'this week is different'.",
    )
    status: BulletStatus = BulletStatus.PENDING


class CarePerson(TraceableModel):
    """One human the caregiver holds — kids, elders, partner; zero-to-many."""

    person_id: str = Field(default_factory=_new_id)
    display_name: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list)
    their_relation: str = Field(
        default="",
        max_length=48,
        description="Who they are to the user (child, parent, …).",
    )
    your_role: str = Field(
        default="",
        max_length=48,
        description="How the user stands toward them (parent, caregiver). Never adult child.",
    )
    care_role_id: str = Field(default="child_care", max_length=40)
    status: BulletStatus = BulletStatus.PENDING
    school: SchoolAnchor | None = None
    contacts: list[CareContact] = Field(
        default_factory=list,
        description="Emailable roles for this person (teacher, doctor, …).",
    )
    usuals: list[UsualWindow] = Field(default_factory=list)


class CareHelper(TraceableModel):
    """Someone who occasionally helps with a care load (friend, neighbor) — not a held role."""

    name: str = Field(max_length=80)
    helps: list[str] = Field(
        default_factory=list,
        description="Display names of people they help with.",
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
    # AI-inferred how each named person relates to the caregiver (parent, child, …).
    person_relationships: dict[str, str] = Field(
        default_factory=dict,
        description="Canonical person name → short relationship phrase from Care infer.",
    )
    people_profiles: list[CarePerson] = Field(
        default_factory=list,
        description="First-class care people; usuals and school contacts live here.",
    )
    calendar_person_by_summary: dict[str, str] = Field(
        default_factory=dict,
        description="Normalized calendar title → person_id (AI event assign).",
    )
    calendar_routine_by_summary: dict[str, str] = Field(
        default_factory=dict,
        description="AI-inferred routine per calendar title (pickup, school, activity, clinic).",
    )


class CareGraphNode(TraceableModel):
    """One node in the Profile responsibilities graph."""

    id: str
    label: str = Field(max_length=100)
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
    shape: str = Field(
        default="circle",
        description="star = caregiver / helper; circle = dependent or domain load",
    )
    relationship: str | None = Field(
        default=None,
        max_length=48,
        description="AI relationship phrase for person nodes (parent, child, …).",
    )


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
    """Care responsibilities graph: caregiver roots + dependent / load nodes."""

    center: CareGraphNode
    # Caregiver roots (You + co-parents/helpers). Includes center; UI lays these out first.
    roots: list[CareGraphNode] = Field(default_factory=list)
    # Dependents + domain loads (not caregiver roots).
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


def is_self_person(person: CarePerson) -> bool:
    """True for the caregiver's own Contacts row (doctor, not a held dependent)."""
    return (person.care_role_id or "").strip().lower() == SELF_CARE_ROLE


def default_contact_roles(care_role_id: str) -> list[str]:
    """Empty-row defaults on Contacts: kids get Teacher+Doctor; You/elders get Doctor."""
    rid = (care_role_id or "").strip().lower()
    if rid == CareRoleId.CHILD_CARE.value:
        return ["Teacher", "Doctor"]
    return ["Doctor"]


def seed_contacts(care_role_id: str) -> list[CareContact]:
    return [CareContact(role=role) for role in default_contact_roles(care_role_id)]


def active_care_people(profile: CareProfile | None) -> list[CarePerson]:
    """People Retriever / gap scan may use (not rejected). Includes the self row."""
    if profile is None:
        return []
    return [p for p in profile.people_profiles if p.status is not BulletStatus.REJECTED]


def held_care_people(profile: CareProfile | None) -> list[CarePerson]:
    """Dependents only — kids, elders, others. Excludes the caregiver's self row."""
    return [p for p in active_care_people(profile) if not is_self_person(p)]


def ensure_self_care_person(
    care: CareProfile,
    display_name: str,
) -> tuple[CareProfile, CarePerson]:
    """Idempotent: one ACCEPTED self person so Contacts always has a You section."""
    name = " ".join((display_name or "").split())[:80] or "You"
    for person in care.people_profiles:
        if not is_self_person(person) or person.status is BulletStatus.REJECTED:
            continue
        if name != "You" and person.display_name.strip().lower() in {"you", ""}:
            updated = person.model_copy(update={"display_name": name})
            people = [
                updated if p.person_id == updated.person_id else p
                for p in care.people_profiles
            ]
            care = care.model_copy(
                update={
                    "people_profiles": people,
                    "version": int(care.version or 1) + 1,
                    "updated_at": _now_utc(),
                }
            )
            return care, updated
        return care, person
    person = CarePerson(
        display_name=name,
        their_relation="self",
        care_role_id=SELF_CARE_ROLE,
        status=BulletStatus.ACCEPTED,
        contacts=seed_contacts(SELF_CARE_ROLE),
    )
    care = care.model_copy(
        update={
            "people_profiles": [person, *care.people_profiles],
            "version": int(care.version or 1) + 1,
            "updated_at": _now_utc(),
        }
    )
    return care, person


def locked_usuals(profile: CareProfile | None) -> list[tuple[CarePerson, UsualWindow]]:
    """Keep'd usuals on held people — the only ones that may gap-nag."""
    out: list[tuple[CarePerson, UsualWindow]] = []
    for person in held_care_people(profile):
        for usual in person.usuals:
            if usual.status in {BulletStatus.ACCEPTED, BulletStatus.EDITED}:
                out.append((person, usual))
    return out


def pending_usuals(profile: CareProfile | None) -> list[tuple[CarePerson, UsualWindow]]:
    """Proposed usuals waiting for Keep / Not me."""
    out: list[tuple[CarePerson, UsualWindow]] = []
    for person in held_care_people(profile):
        for usual in person.usuals:
            if usual.status is BulletStatus.PENDING:
                out.append((person, usual))
    return out


def derive_person_relationships(people: list[CarePerson]) -> dict[str, str]:
    """Keep the graph's name→relation map in sync with people_profiles."""
    return {
        p.display_name: (p.their_relation or "")[:48]
        for p in people
        if p.status is not BulletStatus.REJECTED and p.their_relation and not is_self_person(p)
    }


def care_profile_snippet(profile: CareProfile | None, *, max_chars: int = 800) -> str:
    """Compact grounding block for agents."""
    roles = active_care_roles(profile)
    if not roles:
        return ""
    lines: list[str] = ["Care roles I provide (caregiver load):"]
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
    "SELF_CARE_ROLE",
    "CareGraph",
    "CareGraphCategory",
    "CareGraphEdge",
    "CareContact",
    "CareGraphNode",
    "CareHelper",
    "CarePerson",
    "CareProfile",
    "CareRoleId",
    "CareRoleState",
    "ProtectedWindow",
    "SchoolAnchor",
    "UsualWindow",
    "active_care_people",
    "active_care_roles",
    "care_profile_snippet",
    "clean_conflict_summaries",
    "default_contact_roles",
    "derive_person_relationships",
    "ensure_self_care_person",
    "held_care_people",
    "is_self_person",
    "locked_usuals",
    "pending_usuals",
    "seed_contacts",
]
