"""Shared activity_type enum.

One source of truth used by usuals, reminders, priority tags, and
agenda cache classification. Any agent that emits an activity_type MUST
draw from this enum; call_agent() validates via Pydantic.

`Category` is a coarser typed grouping over `ActivityType` for user-facing
views like "usuals missing this week". Categories are pure enum → enum
mappings - no keyword or regex matching on event text lives here or
downstream. Titles are only ever read by the AI classifier that assigns
an ActivityType in the first place (see agents/activity.py).
"""

from __future__ import annotations

from enum import StrEnum


class ActivityType(StrEnum):
    SPORTS_SOCCER = "sports.soccer"
    SPORTS_BASKETBALL = "sports.basketball"
    SPORTS_SWIM = "sports.swim"
    SPORTS_OTHER = "sports.other"
    SCHOOL_PICKUP = "school.pickup"
    SCHOOL_DROPOFF = "school.dropoff"
    SCHOOL_EVENT = "school.event"
    MEDICAL_APPT = "medical.appointment"
    MEDICAL_THERAPY = "medical.therapy"
    WORK = "work"
    FAMILY = "family"
    COMMUTE = "commute"
    PERSONAL = "personal"
    OTHER = "other"

    @property
    def category(self) -> Category:
        return _ACTIVITY_TO_CATEGORY[self]

    @property
    def load_bucket(self) -> LoadBucket:
        return _ACTIVITY_TO_LOAD_BUCKET[self]


class Category(StrEnum):
    SPORTS = "sports"
    PICKUP = "pickup"
    DROPOFF = "dropoff"
    SCHOOL = "school"
    MEDICAL = "medical"
    WORK = "work"
    FAMILY = "family"
    COMMUTE = "commute"
    PERSONAL = "personal"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _CATEGORY_LABEL[self]


class LoadBucket(StrEnum):
    """Coarse rollup used by the weekly load bar.

    Distinct from `Category` on purpose: the load bar wants big-picture
    "how much of my week is school vs. work vs. medical", so
    dropoff + pickup + school event all collapse into SCHOOL, and
    medical.appointment + medical.therapy collapse into MEDICAL.
    Category keeps them separate because the "usuals missing" view needs
    the actionable distinction (a missed dropoff is not a missed pickup).
    """

    SCHOOL = "school"
    SPORTS = "sports"
    MEDICAL = "medical"
    WORK = "work"
    FAMILY = "family"
    COMMUTE = "commute"
    PERSONAL = "personal"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _LOAD_BUCKET_LABEL[self]

    @property
    def color(self) -> str:
        return _LOAD_BUCKET_COLOR[self]


ALL_ACTIVITY_TYPES: tuple[ActivityType, ...] = tuple(ActivityType)


# Pure enum -> enum grouping. Not derived from event text; the AI has
# already assigned the ActivityType at this point.
_ACTIVITY_TO_CATEGORY: dict[ActivityType, Category] = {
    ActivityType.SPORTS_SOCCER: Category.SPORTS,
    ActivityType.SPORTS_BASKETBALL: Category.SPORTS,
    ActivityType.SPORTS_SWIM: Category.SPORTS,
    ActivityType.SPORTS_OTHER: Category.SPORTS,
    ActivityType.SCHOOL_PICKUP: Category.PICKUP,
    ActivityType.SCHOOL_DROPOFF: Category.DROPOFF,
    ActivityType.SCHOOL_EVENT: Category.SCHOOL,
    ActivityType.MEDICAL_APPT: Category.MEDICAL,
    ActivityType.MEDICAL_THERAPY: Category.MEDICAL,
    ActivityType.WORK: Category.WORK,
    ActivityType.FAMILY: Category.FAMILY,
    ActivityType.COMMUTE: Category.COMMUTE,
    ActivityType.PERSONAL: Category.PERSONAL,
    ActivityType.OTHER: Category.OTHER,
}


_CATEGORY_LABEL: dict[Category, str] = {
    Category.SPORTS: "Sports",
    Category.PICKUP: "Pickup",
    Category.DROPOFF: "Dropoff",
    Category.SCHOOL: "School",
    Category.MEDICAL: "Medical",
    Category.WORK: "Work",
    Category.FAMILY: "Family",
    Category.COMMUTE: "Commute",
    Category.PERSONAL: "Personal",
    Category.OTHER: "Other",
}


_ACTIVITY_TO_LOAD_BUCKET: dict[ActivityType, LoadBucket] = {
    ActivityType.SPORTS_SOCCER: LoadBucket.SPORTS,
    ActivityType.SPORTS_BASKETBALL: LoadBucket.SPORTS,
    ActivityType.SPORTS_SWIM: LoadBucket.SPORTS,
    ActivityType.SPORTS_OTHER: LoadBucket.SPORTS,
    ActivityType.SCHOOL_PICKUP: LoadBucket.SCHOOL,
    ActivityType.SCHOOL_DROPOFF: LoadBucket.SCHOOL,
    ActivityType.SCHOOL_EVENT: LoadBucket.SCHOOL,
    ActivityType.MEDICAL_APPT: LoadBucket.MEDICAL,
    ActivityType.MEDICAL_THERAPY: LoadBucket.MEDICAL,
    ActivityType.WORK: LoadBucket.WORK,
    ActivityType.FAMILY: LoadBucket.FAMILY,
    ActivityType.COMMUTE: LoadBucket.COMMUTE,
    ActivityType.PERSONAL: LoadBucket.PERSONAL,
    ActivityType.OTHER: LoadBucket.OTHER,
}


_LOAD_BUCKET_LABEL: dict[LoadBucket, str] = {
    LoadBucket.SCHOOL: "School",
    LoadBucket.SPORTS: "Sports",
    LoadBucket.MEDICAL: "Medical",
    LoadBucket.WORK: "Work",
    LoadBucket.FAMILY: "Family",
    LoadBucket.COMMUTE: "Commute",
    LoadBucket.PERSONAL: "Personal",
    LoadBucket.OTHER: "Other",
}


# Weekly load palette. Prior palette had WORK, COMMUTE, and OTHER all
# collapsing to the same gray-blue family (WORK #5a7380, COMMUTE and
# OTHER both #8aa4b0), and PERSONAL sat right on top of SPORTS teal -
# so a caregiver looking at the bar couldn't tell them apart at a
# glance. This palette walks the color wheel so each bucket has a
# distinct hue while keeping the muted, non-neon feel of the rest of
# the app (matching --signal-deep #217a6a's saturation profile).
_LOAD_BUCKET_COLOR: dict[LoadBucket, str] = {
    LoadBucket.SCHOOL: "#d99a4a",    # amber
    LoadBucket.SPORTS: "#3aa38a",    # green-teal
    LoadBucket.MEDICAL: "#d15b5b",   # rose
    LoadBucket.WORK: "#5b7cd8",      # indigo blue
    LoadBucket.FAMILY: "#e07a5b",    # warm coral
    LoadBucket.COMMUTE: "#9464c8",   # violet
    LoadBucket.PERSONAL: "#c88b9a",  # dusty pink
    LoadBucket.OTHER: "#8892a6",     # muted slate
}


def activity_category(activity_type: ActivityType | None) -> Category:
    """Return the coarser Category for an ActivityType, or OTHER if unknown."""
    if activity_type is None:
        return Category.OTHER
    return activity_type.category
