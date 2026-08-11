"""A Turn is one round of exchange within a session.

Each Turn couples the user's utterance with Level's response, plus the
supporting evidence, the challenger's questions, and the bias events the
Judge observed. Turns are written to Firestore in real time so the web UI
can subscribe via ``onSnapshot`` and render as the challenger streams.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from level_core.schemas.base import TraceableModel, _new_id


class TurnRole(str, Enum):
    USER = "user"
    LEVEL = "level"


class TurnStatus(str, Enum):
    PENDING = "pending"          # user submitted; agents still running
    STREAMING = "streaming"      # challenger response is streaming to client
    COMPLETE = "complete"        # all agents finished, turn is final
    DEGRADED = "degraded"        # some agent failed; response is best-effort
    BLOCKED = "blocked"          # Model Armor refused; user sees a fallback message


class Citation(TraceableModel):
    """A reference from Level's response to a specific fact in the user's memory.

    Every claim the Challenger makes about the user's past MUST cite one or
    more facts. Uncited claims are considered hallucinations and rejected by
    the outbound guardrail.
    """

    fact_id: str = Field(description="The fact this citation refers to.")
    quote: str = Field(
        description="A short excerpt of the fact statement that appears in the response.",
        max_length=300,
    )
    relevance: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Retriever's assessment of how relevant this fact is to the decision.",
    )


class RetrievedEvidence(TraceableModel):
    """The Retriever's output — the evidence pool the Challenger draws from."""

    fact_ids: list[str] = Field(default_factory=list)
    manifesto_snippet: str | None = Field(
        default=None,
        description="The Manifesto section the Retriever thinks is most relevant.",
    )
    coverage_note: str | None = Field(
        default=None,
        description=(
            "Retriever's own assessment of how well it could ground this decision — "
            "important for downstream degradation ('not much context yet')."
        ),
    )
    contradiction_summaries: list[str] = Field(
        default_factory=list,
        description="Active tensions from the user's profile (commitment vs constraint, etc.).",
    )
    care_profile_snippet: str | None = Field(
        default=None,
        description="Compact caregiver role-load block for Challenger grounding.",
    )
    care_role_fact_ids: list[str] = Field(
        default_factory=list,
        description="Fact ids pinned from the Care Profile (role evidence).",
    )


class ChallengeQuestion(TraceableModel):
    """One of possibly several questions the Challenger poses in a turn.

    Structured (not free prose) so the UI can render them as distinct cards
    and the Judge can score each individually.
    """

    question: str = Field(min_length=10, max_length=500)
    citations: list[Citation] = Field(
        default_factory=list,
        description="Every claim about the user's past must cite at least one fact.",
    )
    challenge_type: str = Field(
        description=(
            "One of: role_theft, assumption, counterexample, value_alignment, "
            "time_horizon, reversibility, precedent, framing."
        ),
    )


class Turn(TraceableModel):
    """One turn in a session.

    A Turn typically starts with the user's utterance (``user_text``) and
    ends with Level's structured response (``challenger_questions`` +
    ``bias_event_ids``). Turns are the primary unit of session persistence.
    """

    turn_id: str = Field(default_factory=_new_id)
    user_id: str
    decision_id: str

    role: TurnRole
    status: TurnStatus = TurnStatus.PENDING

    user_text: str | None = Field(
        default=None,
        description="What the user said this turn (null for role=level turns).",
    )

    retrieved_evidence: RetrievedEvidence | None = None
    challenger_questions: list[ChallengeQuestion] = Field(default_factory=list)

    bias_event_ids: list[str] = Field(
        default_factory=list,
        description="Ids of BiasEvent docs the Judge produced from this turn.",
    )

    degradation_reason: str | None = Field(
        default=None,
        description="If status is DEGRADED or BLOCKED, why. Used for audit + monitoring.",
    )


__all__ = [
    "ChallengeQuestion",
    "Citation",
    "RetrievedEvidence",
    "Turn",
    "TurnRole",
    "TurnStatus",
]
