"""Calendar commitment proposals — add / check availability before writing."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from level_core.schemas.base import LevelModel, TraceableModel, _new_id


class CommitmentKind(str, Enum):
    """What the user is asking Level to do with their schedule."""

    ADD = "add"
    AVAILABILITY = "availability"
    SCHOOL_SEND = "school_send"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    EXPIRED = "expired"


class Weekday(str, Enum):
    MO = "MO"
    TU = "TU"
    WE = "WE"
    TH = "TH"
    FR = "FR"
    SA = "SA"
    SU = "SU"


class CalendarConflict(LevelModel):
    summary: str
    start: str | None = None
    end: str | None = None
    label: str = ""


class FreeSlot(LevelModel):
    start: str
    end: str
    label: str = ""


class EventDraft(LevelModel):
    """Normalized event the user wants to add or check."""

    title: str = "Untitled"
    by_days: list[Weekday] = Field(default_factory=list)
    local_date: str | None = Field(
        default=None,
        description="YYYY-MM-DD for one-off events (availability / single add).",
    )
    local_time: str = Field(default="18:00", description="HH:MM 24h local start.")
    duration_minutes: int = Field(default=60, ge=15, le=24 * 60)
    timezone: str = "America/Los_Angeles"
    notes: str = ""
    recurring: bool = False


class CommitmentCitation(LevelModel):
    fact_id: str
    quote: str


class CommitmentProposal(TraceableModel):
    """Pending schedule change or availability answer awaiting user reaction."""

    proposal_id: str = Field(default_factory=_new_id)
    user_id: str
    kind: CommitmentKind
    status: ProposalStatus = ProposalStatus.PENDING

    user_text: str = ""
    draft: EventDraft
    summary: str = Field(
        default="",
        description="Human-readable restatement of what Level understood.",
    )
    level_message: str = Field(
        default="",
        description="Warm pushback / availability answer (may suggest alternatives).",
    )
    conflicts: list[CalendarConflict] = Field(default_factory=list)
    free_slots: list[FreeSlot] = Field(default_factory=list)
    citations: list[CommitmentCitation] = Field(default_factory=list)
    recommended_action: str = Field(
        default="confirm",
        description="confirm | revise | decline — Level's suggested next step.",
    )

    google_event_id: str | None = None
    resolved_at: datetime | None = None

    # School paper / sick-day — send only when the user presses Send.
    to_email: str = ""
    email_subject: str = ""
    email_body: str = ""
    person_ids: list[str] = Field(default_factory=list)
    cancel_event_ids: list[str] = Field(default_factory=list)
    hold_on_calendar: bool = False  # unused; kept so stored proposals still load


__all__ = [
    "CalendarConflict",
    "CommitmentCitation",
    "CommitmentKind",
    "CommitmentProposal",
    "EventDraft",
    "FreeSlot",
    "ProposalStatus",
    "Weekday",
]
