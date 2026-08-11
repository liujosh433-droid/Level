"""Map calendar titles → activity kind for Today card illustrations."""

from __future__ import annotations

import re

# kind → keyword fragments (matched against lowercased summary)
_KIND_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "sports",
        (
            "soccer",
            "football",
            "baseball",
            "basketball",
            "softball",
            "swim",
            "swimming",
            "practice",
            "game",
            "muay",
            "thai",
            "gym",
            "workout",
            "tennis",
            "track",
            "martial",
            "yoga",
            "run",
            "sport",
        ),
    ),
    (
        "school",
        (
            "school",
            "drop-off",
            "drop off",
            "pickup",
            "pick-up",
            "pick up",
            "classroom",
            "teacher",
            "pta",
            "homework",
            "preschool",
            "kindergarten",
            "class",
            "tutoring",
        ),
    ),
    (
        "medical",
        (
            "dentist",
            "doctor",
            "clinic",
            "pediatric",
            "therapy",
            "appointment",
            "checkup",
            "check-up",
            "hospital",
            "vaccine",
            "ultrasound",
            "orthodont",
        ),
    ),
    (
        "food",
        (
            "dinner",
            "lunch",
            "brunch",
            "breakfast",
            "coffee",
            "restaurant",
            "pizza",
            "meal",
            "eat",
            "dining",
        ),
    ),
    (
        "work",
        (
            "work",
            "email",
            "office",
            "standup",
            "stand-up",
            "deadline",
            "sprint",
            "shift",
            "payroll",
            "zoom",
            "sync",
            "1:1",
            "1-1",
            "meeting",
            "conference",
            "call",
            "interview",
            "review",
            "catch up",
            "catch-up",
            "okrs",
            "client",
        ),
    ),
    (
        "family",
        (
            "co-parent",
            "coparent",
            "handoff",
            "hand-off",
            "family",
            "kids",
            "jordan",
            "weekend with",
            "parent",
            "visitation",
        ),
    ),
    (
        "travel",
        (
            "flight",
            "airport",
            "travel",
            "trip",
            "drive to",
            "road trip",
            "hotel",
            "vacation",
        ),
    ),
    (
        "home",
        (
            "laundry",
            "chores",
            "clean",
            "grocery",
            "errand",
            "home",
            "house",
            "repair",
            "haircut",
        ),
    ),
]


def infer_activity_kind(summary: str) -> str:
    text = re.sub(r"\s+", " ", (summary or "").strip().lower())
    if not text:
        return "generic"
    for kind, words in _KIND_KEYWORDS:
        for w in words:
            if w in text:
                return kind
    return "generic"


# High-chroma hues spaced around the wheel — each category should read at a glance.
ACTIVITY_COLORS: dict[str, str] = {
    "sports": "#16a34a",  # green
    "school": "#2563eb",  # blue
    "work": "#ea580c",  # orange
    "medical": "#dc2626",  # red
    "family": "#eab308",  # yellow/gold
    "food": "#db2777",  # magenta
    "home": "#0d9488",  # teal
    "meeting": "#9333ea",  # purple
    "travel": "#0891b2",  # cyan
    "generic": "#64748b",  # slate
}


def activity_color(kind: str) -> str:
    return ACTIVITY_COLORS.get(kind, ACTIVITY_COLORS["generic"])


__all__ = ["ACTIVITY_COLORS", "activity_color", "infer_activity_kind"]
