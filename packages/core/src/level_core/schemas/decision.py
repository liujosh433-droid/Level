"""A Decision is a session-scoped question the user is thinking through.

Every session starts with the user posing a decision. The Framer produces
a ``DecisionFrame`` — a precise, canonical restatement — which is then
shared across turns as the anchor for retrieval and challenge.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field

from level_core.schemas.base import TraceableModel, _new_id


class DecisionStatus(str, Enum):
    OPEN = "open"                # actively being discussed
    PAUSED = "paused"            # user stepped away; may return
    RESOLVED = "resolved"        # user made a choice
    ABANDONED = "abandoned"      # user gave up / decided not to decide
    DEGRADED = "degraded"        # system couldn't produce a useful session


class DecisionFrame(TraceableModel):
    """The Framer's canonical restatement of the user's decision.

    Structured intentionally: a decision is a *choice among options*, so we
    force the model to enumerate options rather than emit prose. This makes
    the downstream Retriever's job much easier (retrieval per option) and
    the Judge's evaluation more precise ("did the user consider all options
    the Framer surfaced?").
    """

    subject: str = Field(
        description="A single-sentence topic, e.g. 'switching kids to a new school'.",
        min_length=8,
        max_length=200,
    )
    options: list[str] = Field(
        default_factory=list,
        description="The choices the user is weighing, in the user's own framing.",
        min_length=2,
        max_length=8,
    )
    stakes: str = Field(
        description="What's at stake — why this matters to the user, per the user's stated context.",
        min_length=8,
        max_length=500,
    )
    time_pressure: Literal["low", "medium", "high"] = "medium"
    horizon: Literal["days", "weeks", "months", "years"] = "weeks"
    reversibility: Literal["reversible", "hard_to_reverse", "irreversible"] = "hard_to_reverse"


class Decision(TraceableModel):
    """A user's decision under consideration."""

    decision_id: str = Field(default_factory=_new_id)
    user_id: str

    frame: DecisionFrame | None = Field(
        default=None,
        description="Set by the Framer at session start. Null while a session is being opened.",
    )

    status: DecisionStatus = DecisionStatus.OPEN
    opened_at: datetime | None = None
    resolved_at: datetime | None = None

    resolution_note: str | None = Field(
        default=None,
        description="Free-text note about what the user actually decided (if resolved).",
    )
    chosen_option: str | None = Field(
        default=None,
        description="If resolved, which of the frame's options the user picked.",
    )
    origin: str | None = Field(
        default=None,
        description="How the decision was opened: user | async_role_theft | async_usual_gap.",
    )
    trigger_label: str | None = Field(
        default=None,
        description="Human-readable collision / trigger for unsolicited challenges.",
    )


__all__ = ["Decision", "DecisionFrame", "DecisionStatus"]
