"""IngestNormalizer — turns a raw Signal into a list of structured Facts.

Called by the ingestion Cloud Run Jobs after :class:`InboundGuardrail` has
sanitized a Signal. Emits zero or more :class:`Fact` records typed by the
:class:`FactType` taxonomy.

The Normalizer is deliberately conservative — better to skip a signal than
manufacture spurious facts. Every fact carries a ``confidence`` score the
Retriever uses to weight results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import Field

from level_core.agents.base import AgentOutputModel, PROMPT_VERSION, parse_output, prompt_sha
from level_core.identity.auth import default_service_account_for
from level_core.models.base import GeminiClient, GenerationRequest
from level_core.observability.tracer import traced
from level_core.schemas.agent import AgentVersion
from level_core.schemas.signal import Fact, FactType, Signal

if TYPE_CHECKING:
    from level_core.config import Settings

NAME = "ingest_normalizer"
VERSION = f"{PROMPT_VERSION}.0.0"
DESCRIPTION = "Extracts typed Facts from a sanitized Signal."


SYSTEM_INSTRUCTION = """\
You are the Ingest Normalizer inside Level. You read one incoming signal
(email, calendar event, note, chat message, transcribed voice memo) and
extract zero or more structured facts about the user or their life.

You are conservative. If a signal contains no meaningful fact, return
{"facts": []}. Never fabricate detail beyond what's clearly stated.
"""


PROMPT = """\
Signal source: {source}
Signal recorded at: {occurred_at}

Signal content:
---
{text}
---

Fact types (the ONLY categories you may use):
- value_statement: explicit statement of something the user values or cares
  about deeply.
- commitment: a specific promise or plan the user has made.
- constraint: a hard limit on what the user can or will do.
- preference: a stated preference — softer than a constraint.
- concern: something the user is worried about.
- event: a specific dated occurrence relevant to their planning.
- decision_history: something the user tried before and how it went.
- relationship: information about someone else in the user's life relevant
  to their decisions.

Return JSON matching:

{{
  "facts": [
    {{
      "type": one of the fact types above,
      "statement": concise first-person restatement of the fact,
                   20-300 chars, in the user's voice,
      "salience": 0..1, how central this is to the user's decision context,
      "confidence": 0..1, how confident you are the fact is faithful to
                    the signal.
    }},
    ... zero or more
  ]
}}

Guidance:
- Restate in first person ("I care about ...", not "the user cares about ...").
- Prefer specificity over volume — 1 sharp fact beats 4 mushy ones.
- If the signal is a generic newsletter, spam, or trivial log, return
  {{"facts": []}}.
"""


@dataclass(slots=True)
class IngestNormalizerInput:
    """Structured input the Normalizer takes."""

    signal: Signal


class _NormalizedFact(AgentOutputModel):
    type: FactType
    statement: str = Field(min_length=20, max_length=300)
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.75, ge=0.0, le=1.0)


class _NormalizerOutput(AgentOutputModel):
    facts: list[_NormalizedFact] = Field(default_factory=list, max_length=8)


class IngestNormalizer:
    """The IngestNormalizer agent."""

    def __init__(self, gemini: GeminiClient, model_id: str) -> None:
        self._gemini = gemini
        self._model_id = model_id

    @traced("agent.ingest_normalizer.run")
    async def run(self, input_: IngestNormalizerInput) -> list[Fact]:
        signal = input_.signal
        text = (signal.text or "").strip()
        if not text:
            return []
        prompt = PROMPT.format(
            source=signal.source.value,
            occurred_at=(
                signal.occurred_at.isoformat() if signal.occurred_at is not None else "(unknown)"
            ),
            text=text[:4000],
        )
        response = await self._gemini.generate(
            GenerationRequest(
                prompt=prompt,
                model_id=self._model_id,
                system_instruction=SYSTEM_INSTRUCTION,
                response_schema=_NormalizerOutput.model_json_schema(),
                temperature=0.1,
                max_output_tokens=1024,
                metadata={"agent": NAME, "version": VERSION},
            )
        )
        parsed = parse_output(NAME, response.text, _NormalizerOutput)
        provenance = f"{NAME}@{VERSION}"
        return [
            Fact(
                user_id=signal.user_id,
                type=nf.type,
                statement=nf.statement,
                source_signal_ids=[signal.signal_id],
                salience=nf.salience,
                confidence=nf.confidence,
                written_by=provenance,
            )
            for nf in parsed.facts
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
        allowed_tools=["upsert_fact", "upsert_signal", "embed_query", "upsert_vector"],
        description=DESCRIPTION,
    )


__all__ = [
    "DESCRIPTION",
    "IngestNormalizer",
    "IngestNormalizerInput",
    "NAME",
    "PROMPT",
    "SYSTEM_INSTRUCTION",
    "VERSION",
    "build_version",
]
