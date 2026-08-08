"""Bias taxonomy, per-turn bias events, aggregated bias profile, and manifesto.

Level's core learning signal is *which cognitive biases show up in the user's
framing of their decisions*. Over time this produces a persistent Bias
Profile — a numeric picture of the user's tendencies — which the Challenger
uses to push back more precisely in future sessions.

The Manifesto is a self-rewriting statement of what the user says they value,
regenerated from the union of `VALUE_STATEMENT` facts and reflected back at
the user when future decisions contradict past commitments.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from level_core.schemas.base import TraceableModel, _new_id


class BiasCategory(str, Enum):
    """Curated subset of the cognitive-bias literature.

    Chosen for relevance to personal decisions (as opposed to organizational
    or investment decisions). Full definitions live in
    ``level_core.bias.taxonomy``.
    """

    CONFIRMATION = "confirmation"                # cherry-picking evidence
    ANCHORING = "anchoring"                      # first option becomes default
    SUNK_COST = "sunk_cost"                      # can't quit because of past investment
    STATUS_QUO = "status_quo"                    # bias toward doing nothing
    OPTIMISM = "optimism"                        # underestimating downside
    PLANNING_FALLACY = "planning_fallacy"        # underestimating time / effort
    RECENCY = "recency"                          # overweighting recent events
    AVAILABILITY = "availability"                # overweighting easily-recalled examples
    NEGATIVITY = "negativity"                    # overweighting worst-case
    IDENTITY_PROTECTION = "identity_protection"  # rejecting evidence that threatens self-image
    PROJECTION = "projection"                    # assuming others share your priorities
    FRAMING = "framing"                          # decision phrased to bias choice
    SOCIAL_DESIRABILITY = "social_desirability"  # deciding based on what looks acceptable to others
    LONELINESS_URGENCY = "loneliness_urgency"    # deciding under emotional acute state
    CATASTROPHIZING = "catastrophizing"          # inflating small risks into disasters


class BiasDefinition(TraceableModel):
    """Static definition of a bias — loaded from ``bias/data/taxonomy.yaml``."""

    category: BiasCategory
    name: str
    short_description: str = Field(max_length=200)
    detection_hint: str = Field(
        description="What the Judge should look for in the user's framing to detect this bias.",
        max_length=500,
    )
    challenger_prompt: str = Field(
        description="A prompt fragment the Challenger uses when this bias is active.",
        max_length=500,
    )


class BiasScore(TraceableModel):
    """Numeric picture of a single bias's presence in the user's decisions."""

    category: BiasCategory
    ema: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Exponential moving average of per-turn intensity, smoothed over sessions.",
    )
    streak: int = Field(
        default=0,
        ge=0,
        description="How many consecutive sessions this bias has appeared with intensity > threshold.",
    )
    last_seen_at: datetime | None = None
    total_observations: int = Field(default=0, ge=0)


class BiasEvent(TraceableModel):
    """One instance of a bias detected by the Judge in a specific turn.

    Emitted by the Judge agent. Aggregated nightly into the ``BiasProfile``.
    """

    event_id: str = Field(default_factory=_new_id)
    user_id: str
    decision_id: str
    turn_id: str

    category: BiasCategory
    intensity: float = Field(
        ge=0.0,
        le=1.0,
        description="How strongly the bias was present in this turn.",
    )
    evidence: str = Field(
        description="Judge's brief citation of what in the user's framing showed the bias.",
        max_length=500,
    )
    challenger_response_addressed_it: bool = Field(
        default=False,
        description="Whether the Challenger's questions in this turn addressed the bias.",
    )


class BiasProfile(TraceableModel):
    """Aggregate bias picture for a user — the persistent learning state."""

    user_id: str
    scores: list[BiasScore] = Field(default_factory=list)

    session_count: int = Field(default=0, ge=0)
    last_updated_at: datetime | None = None


class Manifesto(TraceableModel):
    """A self-rewriting statement of what the user says they value.

    Regenerated after each session as a synthesis of the user's
    ``VALUE_STATEMENT`` and ``COMMITMENT`` facts. The current version is
    surfaced to the Challenger so it can catch contradictions with past
    commitments.
    """

    version: int = Field(default=1, ge=1)
    user_id: str

    statement: str = Field(
        description="First-person Markdown text — the user's evolving manifesto.",
        min_length=20,
        max_length=4000,
    )
    source_fact_ids: list[str] = Field(
        default_factory=list,
        description="Fact ids that contributed to this version — enables citation in Challenger prompts.",
    )
    change_summary: str | None = Field(
        default=None,
        description="Diff-style summary of what changed since the previous version.",
    )


__all__ = [
    "BiasCategory",
    "BiasDefinition",
    "BiasEvent",
    "BiasProfile",
    "BiasScore",
    "Manifesto",
]
