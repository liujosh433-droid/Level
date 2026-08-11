"""Map calendar events → activity kind for Today card illustrations.

Prefer AI ``calendar_role_by_summary`` / care role. Keyword matching is only a
thin visual fallback when no role is known yet — not care classification.
"""

from __future__ import annotations

import re

from level_core.schemas.care import CareRoleId

# AI care role → illustration kind (structured mapping, not title keywords).
_ROLE_TO_KIND: dict[CareRoleId, str] = {
    CareRoleId.CHILD_CARE: "school",
    CareRoleId.ELDER_CARE: "family",
    CareRoleId.PAID_WORK: "work",
    CareRoleId.SELF_RECOVERY: "medical",
    CareRoleId.HOUSEHOLD_LOGISTICS: "home",
    CareRoleId.PARTNER_COPARENT: "family",
}

# Visual-only fallback when AI has not tagged the title yet.
_KIND_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("sports", ("soccer", "football", "baseball", "basketball", "practice", "game", "swim")),
    ("school", ("school", "pickup", "drop-off", "drop off", "homework", "pta")),
    ("medical", ("dentist", "doctor", "clinic", "therapy", "appointment")),
    ("food", ("dinner", "lunch", "brunch", "breakfast", "coffee")),
    ("work", ("work", "email", "standup", "stand-up", "1:1", "meeting", "sprint", "deadline")),
    ("family", ("co-parent", "coparent", "family", "handoff")),
    ("travel", ("flight", "airport", "travel", "hotel")),
    ("home", ("grocery", "errand", "laundry", "chores")),
]


def kind_from_care_role(role: str | CareRoleId | None) -> str | None:
    if role is None or role == "":
        return None
    try:
        rid = role if isinstance(role, CareRoleId) else CareRoleId(str(role))
    except ValueError:
        return None
    return _ROLE_TO_KIND.get(rid)


def infer_activity_kind(
    summary: str,
    *,
    care_role: str | CareRoleId | None = None,
) -> str:
    """Prefer AI care role; else thin visual keyword fallback; else generic."""
    from_role = kind_from_care_role(care_role)
    if from_role:
        return from_role
    text = re.sub(r"\s+", " ", (summary or "").strip().lower())
    if not text:
        return "generic"
    for kind, words in _KIND_KEYWORDS:
        for w in words:
            if w in text:
                return kind
    return "generic"


ACTIVITY_COLORS: dict[str, str] = {
    "sports": "#16a34a",
    "school": "#2563eb",
    "work": "#ea580c",
    "medical": "#dc2626",
    "family": "#eab308",
    "food": "#db2777",
    "home": "#0d9488",
    "meeting": "#9333ea",
    "travel": "#0891b2",
    "generic": "#64748b",
}


def activity_color(kind: str) -> str:
    return ACTIVITY_COLORS.get(kind, ACTIVITY_COLORS["generic"])


__all__ = [
    "ACTIVITY_COLORS",
    "activity_color",
    "infer_activity_kind",
    "kind_from_care_role",
]
