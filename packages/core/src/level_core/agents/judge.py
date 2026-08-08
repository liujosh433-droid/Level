"""Judge — scores which cognitive biases showed up in the user's framing.

The Judge runs *after* the Challenger has responded. It reviews the full
turn (frame, user text, challenger questions) and emits zero or more
:class:`BiasEvent`s. These feed the persistent Bias Profile that future
Challenger runs use to push back more precisely.

The Judge does not talk to the user directly. Its output shapes future
Challenger behavior, not present-turn behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import Field

from level_core.agents.base import AgentOutputModel, PROMPT_VERSION, parse_output, prompt_sha
from level_core.bias.taxonomy import BIAS_TAXONOMY
from level_core.identity.auth import default_service_account_for
from level_core.models.base import GeminiClient, GenerationRequest
from level_core.observability.tracer import traced
from level_core.schemas.agent import AgentVersion
from level_core.schemas.bias import BiasCategory, BiasEvent
from level_core.schemas.decision import DecisionFrame
from level_core.schemas.turn import ChallengeQuestion

if TYPE_CHECKING:
    from level_core.config import Settings

NAME = "judge"
VERSION = f"{PROMPT_VERSION}.0.0"
DESCRIPTION = "Scores which cognitive biases showed up in the user's framing."


SYSTEM_INSTRUCTION = """\
You are the Judge agent inside Level. You review one turn of a decision
session and emit structured BiasEvent records for each bias you observe.

You are strict. You only report a bias if you can point to specific text in
the user's message that shows it. Do not report biases speculatively.
Do not double-count: if two biases have overlapping evidence, pick the one
with the strongest fit.

You never talk to the user. Your output shapes future Challenger behavior.
"""


PROMPT = """\
Decision frame the user is working through:
- Subject: {subject}
- Options: {options}
- Stakes: {stakes}

The user's message on this turn:
---
{user_text}
---

Questions the Challenger asked in response:
{challenger_questions}

Bias taxonomy — the ONLY categories you may use:
{taxonomy_summary}

Return JSON matching:

{{
  "events": [
    {{
      "category": one of the taxonomy category values above,
      "intensity": 0..1, how strongly the bias was present,
      "evidence": short excerpt of the user's own words that shows it,
      "challenger_response_addressed_it": boolean, whether at least one of
        the Challenger's questions engaged this bias directly.
    }},
    ... zero or more
  ]
}}

Rules:
- Only include biases where you can quote specific text from the user's
  message. If you can't quote it, don't include it.
- Intensity is 0.1 for a mild hint, 0.5 for clear presence, 0.9 for
  overwhelming presence.
- Cap total events at 5 per turn. If more than 5 apply, pick the strongest.
- If no bias is clearly present, return {{"events": []}}. Do not manufacture.
"""


@dataclass(slots=True)
class JudgeInput:
    """Structured input the Judge takes."""

    frame: DecisionFrame
    user_text: str
    challenger_questions: list[ChallengeQuestion] = field(default_factory=list)


class _JudgeEvent(AgentOutputModel):
    category: BiasCategory
    intensity: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(max_length=500)
    challenger_response_addressed_it: bool = False


class _JudgeOutput(AgentOutputModel):
    events: list[_JudgeEvent] = Field(default_factory=list, max_length=5)


def _render_taxonomy_summary() -> str:
    return "\n".join(
        f"- {definition.category.value}: {definition.short_description}"
        for definition in BIAS_TAXONOMY.values()
    )


def _render_challenger_questions(questions: list[ChallengeQuestion]) -> str:
    if not questions:
        return "(none)"
    return "\n".join(
        f"- ({q.challenge_type}) {q.question}" for q in questions
    )


class Judge:
    """The Judge agent."""

    def __init__(self, gemini: GeminiClient, model_id: str) -> None:
        self._gemini = gemini
        self._model_id = model_id

    @traced("agent.judge.run")
    async def run(
        self,
        input_: JudgeInput,
        *,
        user_id: str,
        decision_id: str,
        turn_id: str,
    ) -> list[BiasEvent]:
        prompt = PROMPT.format(
            subject=input_.frame.subject,
            options=", ".join(input_.frame.options),
            stakes=input_.frame.stakes,
            user_text=input_.user_text.strip() or "(user has just opened the session)",
            challenger_questions=_render_challenger_questions(input_.challenger_questions),
            taxonomy_summary=_render_taxonomy_summary(),
        )
        response = await self._gemini.generate(
            GenerationRequest(
                prompt=prompt,
                model_id=self._model_id,
                system_instruction=SYSTEM_INSTRUCTION,
                response_schema=_JudgeOutput.model_json_schema(),
                temperature=0.1,
                max_output_tokens=1024,
                metadata={"agent": NAME, "version": VERSION},
            )
        )
        parsed = parse_output(NAME, response.text, _JudgeOutput)
        provenance = f"{NAME}@{VERSION}"
        return [
            BiasEvent(
                user_id=user_id,
                decision_id=decision_id,
                turn_id=turn_id,
                category=event.category,
                intensity=event.intensity,
                evidence=event.evidence,
                challenger_response_addressed_it=event.challenger_response_addressed_it,
                written_by=provenance,
            )
            for event in parsed.events
        ]


def build_version(settings: Settings) -> AgentVersion:
    return AgentVersion(
        name=NAME,
        version=VERSION,
        prompt_sha=prompt_sha(SYSTEM_INSTRUCTION + PROMPT),
        model_id=settings.fast_model,
        owner="level-team",
        service_account=(
            None if settings.is_local else default_service_account_for(NAME, settings.gcp_project)
        ),
        allowed_tools=["append_bias_event", "get_bias_profile"],
        description=DESCRIPTION,
    )


__all__ = [
    "DESCRIPTION",
    "Judge",
    "JudgeInput",
    "NAME",
    "PROMPT",
    "SYSTEM_INSTRUCTION",
    "VERSION",
    "build_version",
]
