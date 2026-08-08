"""Challenger — Level's core agent. Asks the hard clarifying question.

The Challenger is the star of the demo. Its behavior is:

- WARM, never cold. Every question comes from care, not judgment.
- SPECIFIC, never generic. Every claim about the user's past cites a fact_id.
- ONE THING AT A TIME. Max three questions per turn; fewer is better.
- NEVER FLATTERS. No "great question", no "you're doing amazing".
- NEVER PRESCRIBES. Asks questions; does not tell the user what to do.
- NEVER MORALIZES. Engages this decision as framed; does not lecture.

Every claim about the user's past MUST cite a fact_id from the retrieval
pool. The outbound guardrail (:mod:`level_core.guardrails.outbound`)
enforces this post-hoc — hallucinated fact_ids are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from level_core.agents.base import AgentOutputModel, PROMPT_VERSION, parse_output, prompt_sha
from level_core.bias.taxonomy import BIAS_TAXONOMY
from level_core.identity.auth import default_service_account_for
from level_core.models.base import GeminiClient, GenerationRequest
from level_core.observability.tracer import traced
from level_core.schemas.agent import AgentVersion
from level_core.schemas.bias import BiasProfile
from level_core.schemas.decision import DecisionFrame
from level_core.schemas.signal import Fact
from level_core.schemas.turn import ChallengeQuestion, Citation, Turn

if TYPE_CHECKING:
    from level_core.config import Settings

NAME = "challenger"
VERSION = f"{PROMPT_VERSION}.0.0"
DESCRIPTION = "Asks the hard, cited, warmly-adversarial question."


ChallengeType = Literal[
    "assumption",
    "counterexample",
    "value_alignment",
    "time_horizon",
    "reversibility",
    "precedent",
    "framing",
]


SYSTEM_INSTRUCTION = """\
You are Level. You help a busy caregiver think more clearly about decisions
they're already thinking about.

Your job is NOT to be helpful the way most AI is helpful. You are the friend
who won't let them off the hook. You ask the hard clarifying question — the
one their people-pleasing coworker or agreeable AI would not ask.

TONE CONTRACT (non-negotiable):
- WARM, not cold. Every question comes from care, not judgment.
- SPECIFIC, not generic. Cite specific facts from their past when you
  challenge a claim. Never say "have you considered..." without a real,
  cited reason to consider it.
- ONE THING AT A TIME. Ask up to 3 questions per response. Fewer is better.
- SHORT. A good question is 1-2 sentences.
- NEVER FLATTER. Never say "great question", "you're doing amazing", or any
  variant. That's what everyone else does.
- NEVER PRESCRIBE. You ask questions, you don't tell them what to do.
- NEVER MORALIZE. You don't lecture about balance, self-care, or being a
  good parent. You engage with THIS decision as they've framed it.

GROUNDING (non-negotiable):
Every claim you make about the user's past MUST cite at least one fact_id
from the retrieved evidence. If you cite it, the quote must appear
near-verbatim in the corresponding fact statement. Do not invent facts.
If the retrieved evidence is insufficient to make a specific challenge, ask
the user for the missing context instead of guessing.

FORMAT:
Output STRICTLY as JSON matching the schema. No prose outside the JSON.
"""


PROMPT = """\
Decision frame:
- Subject: {subject}
- Options: {options}
- Stakes: {stakes}
- Time pressure: {time_pressure}
- Horizon: {horizon}
- Reversibility: {reversibility}

Retrieved evidence — you may ONLY cite these fact_ids:
{fact_list}

Retrieval coverage note (Retriever's honest assessment):
{coverage_note}

Known tensions in their own record (use for value_alignment / counterexample):
{contradictions}

The user's current manifesto snippet (what they've said they value; may be
empty):
---
{manifesto_snippet}
---

The user's active biases (from prior sessions — biases with EMA > 0.35):
{active_biases}

Prior turns in this session (most recent last):
{prior_turns}

The user just said:
---
{user_text}
---

Return JSON matching:

{{
  "questions": [
    {{
      "question": string, 1-2 sentences ending in "?",
      "citations": [
        {{ "fact_id": one of the ids above, "quote": short excerpt, "relevance": 0..1 }}
      ],
      "challenge_type": one of {allowed_challenge_types}
    }},
    ... up to 3
  ]
}}

Guidance for choosing challenge_type:
- assumption: user is assuming something they haven't checked.
- counterexample: there is evidence against their framing in their own past.
- value_alignment: this contradicts something they said they valued.
- time_horizon: they're not thinking on the right timescale.
- reversibility: they're treating a reversible decision as one-shot, or vice versa.
- precedent: they've faced a very similar decision before.
- framing: the way they've phrased it is loading the answer.

If coverage is poor and you would need to invent facts to challenge them,
return exactly ONE question of type "assumption" asking for the missing
context in first person: "Can you tell me more about ...?"
"""


@dataclass(slots=True)
class ChallengerInput:
    """Structured input the Challenger takes."""

    frame: DecisionFrame
    retrieved_facts: list[Fact]
    manifesto_snippet: str | None
    bias_profile: BiasProfile | None
    prior_turns: list[Turn] = field(default_factory=list)
    user_text: str = ""
    coverage_note: str = ""
    contradiction_summaries: list[str] = field(default_factory=list)

    @property
    def available_fact_ids(self) -> set[str]:
        return {f.fact_id for f in self.retrieved_facts}


class _ChallengerCitation(AgentOutputModel):
    fact_id: str
    quote: str = Field(max_length=300)
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)


class _ChallengerQuestion(AgentOutputModel):
    question: str = Field(min_length=10, max_length=500)
    citations: list[_ChallengerCitation] = Field(default_factory=list)
    challenge_type: ChallengeType


class _ChallengerOutput(AgentOutputModel):
    questions: list[_ChallengerQuestion] = Field(min_length=1, max_length=3)


_ALLOWED_CHALLENGE_TYPES: tuple[str, ...] = (
    "assumption",
    "counterexample",
    "value_alignment",
    "time_horizon",
    "reversibility",
    "precedent",
    "framing",
)


def _render_active_biases(profile: BiasProfile | None) -> str:
    if profile is None:
        return "(none — first session)"
    active = sorted(
        (s for s in profile.scores if s.ema >= 0.35),
        key=lambda s: s.ema,
        reverse=True,
    )
    if not active:
        return "(none active)"
    lines = []
    for score in active[:5]:
        definition = BIAS_TAXONOMY.get(score.category)
        name = definition.name if definition else score.category.value
        lines.append(
            f"- {name} (ema={score.ema:.2f}, streak={score.streak}, "
            f"total_observations={score.total_observations})"
        )
    return "\n".join(lines)


def _render_prior_turns(turns: list[Turn]) -> str:
    if not turns:
        return "(none)"
    lines = []
    for turn in turns[-4:]:
        if turn.user_text:
            lines.append(f"user: {turn.user_text.strip()[:400]}")
        for q in turn.challenger_questions:
            lines.append(f"level ({q.challenge_type}): {q.question.strip()[:400]}")
    return "\n".join(lines) or "(none)"


class Challenger:
    """The Challenger agent."""

    def __init__(self, gemini: GeminiClient, model_id: str) -> None:
        self._gemini = gemini
        self._model_id = model_id

    @traced("agent.challenger.run")
    async def run(self, input_: ChallengerInput) -> list[ChallengeQuestion]:
        fact_list = (
            "\n".join(
                f"- [{f.fact_id}] {f.type.value}: {f.statement}"
                for f in input_.retrieved_facts
            )
            or "(no facts retrieved)"
        )
        prompt = PROMPT.format(
            subject=input_.frame.subject,
            options=", ".join(input_.frame.options),
            stakes=input_.frame.stakes,
            time_pressure=input_.frame.time_pressure,
            horizon=input_.frame.horizon,
            reversibility=input_.frame.reversibility,
            fact_list=fact_list,
            coverage_note=input_.coverage_note or "(unknown)",
            contradictions=(
                "\n".join(f"- {s}" for s in input_.contradiction_summaries[:5])
                or "(none)"
            ),
            manifesto_snippet=input_.manifesto_snippet or "(none)",
            active_biases=_render_active_biases(input_.bias_profile),
            prior_turns=_render_prior_turns(input_.prior_turns),
            user_text=input_.user_text.strip() or "(user has just opened the session)",
            allowed_challenge_types=list(_ALLOWED_CHALLENGE_TYPES),
        )
        response = await self._gemini.generate(
            GenerationRequest(
                prompt=prompt,
                model_id=self._model_id,
                system_instruction=SYSTEM_INSTRUCTION,
                response_schema=_ChallengerOutput.model_json_schema(),
                temperature=0.35,
                max_output_tokens=2048,
                metadata={"agent": NAME, "version": VERSION},
            )
        )
        parsed = parse_output(NAME, response.text, _ChallengerOutput)
        provenance = f"{NAME}@{VERSION}"
        return [
            ChallengeQuestion(
                question=q.question,
                citations=[
                    Citation(
                        fact_id=c.fact_id,
                        quote=c.quote,
                        relevance=c.relevance,
                        written_by=provenance,
                    )
                    for c in q.citations
                ],
                challenge_type=q.challenge_type,
                written_by=provenance,
            )
            for q in parsed.questions
        ]


def build_version(settings: Settings) -> AgentVersion:
    return AgentVersion(
        name=NAME,
        version=VERSION,
        prompt_sha=prompt_sha(SYSTEM_INSTRUCTION + PROMPT),
        model_id=settings.reasoning_model,
        owner="level-team",
        service_account=(
            None if settings.is_local else default_service_account_for(NAME, settings.gcp_project)
        ),
        allowed_tools=["get_facts", "get_manifesto", "get_bias_profile"],
        description=DESCRIPTION,
    )


__all__ = [
    "DESCRIPTION",
    "Challenger",
    "ChallengerInput",
    "ChallengeType",
    "NAME",
    "PROMPT",
    "SYSTEM_INSTRUCTION",
    "VERSION",
    "build_version",
]
