"""Framer agent — restates the user's decision precisely and structurally.

The Framer's job is to convert whatever the user typed ("idk if we should
switch schools") into a canonical :class:`DecisionFrame` — a structured
picture of the choice they're actually making, the options they have, what's
at stake, and the shape of the decision (time pressure, horizon,
reversibility).

Every downstream agent (Retriever, Challenger, Judge) works off this frame,
so we intentionally push the LLM toward being precise here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from level_core.agents.base import AgentOutputModel, PROMPT_VERSION, parse_output, prompt_sha
from level_core.identity.auth import default_service_account_for
from level_core.models.base import GeminiClient, GenerationRequest
from level_core.observability.tracer import traced
from level_core.schemas.agent import AgentVersion
from level_core.schemas.decision import DecisionFrame

if TYPE_CHECKING:
    from level_core.config import Settings

NAME = "framer"
VERSION = f"{PROMPT_VERSION}.0.0"  # bump version when PROMPT changes
DESCRIPTION = "Restates the user's decision as a structured DecisionFrame."


SYSTEM_INSTRUCTION = """\
You are the Framer agent inside Level, a decision-partner AI for busy caregivers.

Your job is to take whatever the user typed and return a *precise, canonical*
restatement of the decision as JSON. You do not offer opinions. You do not
add options the user didn't mention (unless the user's framing is missing an
obvious "do nothing" option, in which case include it). You do not soften
or reframe the stakes.

Output STRICTLY as JSON matching the schema. No prose outside the JSON.
"""


PROMPT = """\
The user wrote:
---
{user_prompt}
---

Recent context from their signals (may be empty):
---
{recent_signals_summary}
---

Their current manifesto (what they've said they value; may be empty):
---
{manifesto_snippet}
---

Return a JSON object matching this schema:

{{
  "subject": string, 8-200 chars, a single sentence naming the decision topic,
  "options": list of 2-8 strings, the choices the user is actually weighing,
  "stakes": string, 8-500 chars, what's at stake in the user's own framing,
  "time_pressure": "low" | "medium" | "high",
  "horizon": "days" | "weeks" | "months" | "years",
  "reversibility": "reversible" | "hard_to_reverse" | "irreversible"
}}

Guidelines:
- Options MUST cover the actual space of choices. If the user only mentions
  one option, infer the implicit alternative ("do nothing" or "stay the
  same") and include it explicitly.
- Time pressure is "high" only if there is a concrete external deadline
  within days. Internal urgency without an external deadline is "medium".
- Reversibility describes the choice, not the outcome. Signing a school
  contract is "hard_to_reverse". Whether the child adapts is a separate
  question.
- Do not editorialize. Just structure.
"""


@dataclass(slots=True)
class FramerInput:
    """Structured input the Framer takes."""

    user_prompt: str
    recent_signals_summary: str = ""
    manifesto_snippet: str = ""


class _FramerOutput(AgentOutputModel):
    """The exact JSON shape we require from Gemini."""

    subject: str = Field(min_length=8, max_length=200)
    options: list[str] = Field(min_length=2, max_length=8)
    stakes: str = Field(min_length=8, max_length=500)
    time_pressure: Literal["low", "medium", "high"]
    horizon: Literal["days", "weeks", "months", "years"]
    reversibility: Literal["reversible", "hard_to_reverse", "irreversible"]


class Framer:
    """The Framer agent."""

    def __init__(self, gemini: GeminiClient, model_id: str) -> None:
        self._gemini = gemini
        self._model_id = model_id

    @traced("agent.framer.run")
    async def run(self, input_: FramerInput) -> DecisionFrame:
        rendered = PROMPT.format(
            user_prompt=input_.user_prompt.strip(),
            recent_signals_summary=input_.recent_signals_summary.strip() or "(none)",
            manifesto_snippet=input_.manifesto_snippet.strip() or "(none)",
        )
        response = await self._gemini.generate(
            GenerationRequest(
                prompt=rendered,
                model_id=self._model_id,
                system_instruction=SYSTEM_INSTRUCTION,
                response_schema=_FramerOutput.model_json_schema(),
                temperature=0.15,
                max_output_tokens=1024,
                metadata={"agent": NAME, "version": VERSION},
            )
        )
        parsed = parse_output(NAME, response.text, _FramerOutput)
        return DecisionFrame(
            subject=parsed.subject,
            options=parsed.options,
            stakes=parsed.stakes,
            time_pressure=parsed.time_pressure,
            horizon=parsed.horizon,
            reversibility=parsed.reversibility,
            written_by=f"{NAME}@{VERSION}",
        )


def build_version(settings: Settings) -> AgentVersion:
    """Return the AgentVersion record for the Registry."""
    return AgentVersion(
        name=NAME,
        version=VERSION,
        prompt_sha=prompt_sha(SYSTEM_INSTRUCTION + PROMPT),
        model_id=settings.reasoning_model,
        owner="level-team",
        service_account=(
            None if settings.is_local else default_service_account_for(NAME, settings.gcp_project)
        ),
        allowed_tools=[],
        description=DESCRIPTION,
    )


__all__ = [
    "DESCRIPTION",
    "Framer",
    "FramerInput",
    "NAME",
    "PROMPT",
    "SYSTEM_INSTRUCTION",
    "VERSION",
    "build_version",
]
