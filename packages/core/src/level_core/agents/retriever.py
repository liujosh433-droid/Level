"""Retriever — pulls the most relevant facts from the user's Memory Bank.

Hybrid retrieval:
  1. Pin Care Profile role facts first (caregiver specialization)
  2. Pin durable constraints / commitments / relationships / values
  3. Fill remaining slots with vector search
  4. Attach active contradiction summaries from the profile snapshot
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import Field

from level_core.agents.base import AgentOutputModel, PROMPT_VERSION, parse_output, prompt_sha
from level_core.gateway.router import AgentGateway
from level_core.identity.auth import default_service_account_for
from level_core.memory.base import FactRepository, ManifestoRepository, VectorHit, VectorStore
from level_core.models.base import EmbeddingClient, GeminiClient, GenerationRequest
from level_core.observability.tracer import traced
from level_core.schemas.agent import AgentVersion
from level_core.schemas.care import active_care_roles, care_profile_snippet
from level_core.schemas.decision import DecisionFrame
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Fact, FactType
from level_core.schemas.turn import RetrievedEvidence

if TYPE_CHECKING:
    from level_core.config import Settings

NAME = "retriever"
VERSION = f"{PROMPT_VERSION}.0.0"
DESCRIPTION = "Retrieves cited evidence for a decision from the user's Memory Bank."

_PIN_TYPES = frozenset(
    {
        FactType.CONSTRAINT,
        FactType.COMMITMENT,
        FactType.RELATIONSHIP,
        FactType.VALUE_STATEMENT,
    }
)

SYSTEM_INSTRUCTION = """\
You are the Retriever agent inside Level. You do not talk to the user. You
receive a decision frame, a list of candidate facts pulled from hybrid
retrieval, and the user's current manifesto snippet. Your job is to write a
short, honest coverage note describing how well the retrieved evidence
grounds the decision. If retrieval is weak, say so plainly.
"""


PROMPT = """\
Decision frame:
- Subject: {subject}
- Options: {options}
- Stakes: {stakes}

Candidate retrieved facts (pinned durable facts first, then similarity):
{fact_list}

Known tensions / contradictions:
{contradictions}

Manifesto snippet (may be empty):
---
{manifesto_snippet}
---

Return JSON matching this schema:

{{
  "coverage_note": string, 1-2 sentences, honest description of retrieval
                   quality. Include phrases like "little context available"
                   when applicable.
}}

Do not include facts the user hasn't recorded. Do not include the manifesto
in the coverage note verbatim.
"""


@dataclass(slots=True)
class RetrieverInput:
    """Structured input the Retriever takes."""

    user_id: str
    frame: DecisionFrame
    top_k: int = 8


class _RetrieverOutput(AgentOutputModel):
    coverage_note: str = Field(max_length=400)


def _pin_durable_facts(facts: list[Fact], *, limit: int = 4) -> list[Fact]:
    pinned = [
        f
        for f in facts
        if f.type in _PIN_TYPES and f.confidence >= 0.55 and f.salience >= 0.35
    ]
    pinned.sort(key=lambda f: (f.salience, f.confidence), reverse=True)
    return pinned[:limit]


def _pin_care_role_facts(
    facts: list[Fact],
    role_fact_ids: list[str],
    *,
    limit: int = 4,
) -> list[Fact]:
    by_id = {f.fact_id: f for f in facts}
    out: list[Fact] = []
    for fid in role_fact_ids:
        fact = by_id.get(fid)
        if fact is None:
            continue
        out.append(fact)
        if len(out) >= limit:
            break
    return out


class Retriever:
    """Hybrid durable-pin + vector retrieval + LLM coverage annotation."""

    def __init__(
        self,
        *,
        gemini: GeminiClient,
        embedder: EmbeddingClient,
        vectors: VectorStore,
        facts: FactRepository,
        manifestos: ManifestoRepository,
        model_id: str,
        gateway: AgentGateway | None = None,
    ) -> None:
        self._gemini = gemini
        self._embedder = embedder
        self._vectors = vectors
        self._facts = facts
        self._manifestos = manifestos
        self._model_id = model_id
        self._gateway = gateway

    async def _load_care_profile(self, user_id: str):
        """Load Care Profile via Agent Gateway when wired (scoped tool access)."""
        if self._gateway is not None:
            return await self._gateway.invoke(
                agent_name=NAME,
                tool_name="get_care_profile",
                user_id=user_id,
            )
        return await self._manifestos.get_care_profile(user_id=user_id)

    @traced("agent.retriever.run")
    async def run(self, input_: RetrieverInput) -> RetrievedEvidence:
        all_facts = await self._facts.list_for_user(user_id=input_.user_id, limit=200)
        care = await self._load_care_profile(input_.user_id)
        care_roles = active_care_roles(care)
        care_fact_ids: list[str] = []
        for role in sorted(care_roles, key=lambda r: r.salience, reverse=True)[:4]:
            care_fact_ids.extend(role.source_fact_ids[:2])
        care_pinned = _pin_care_role_facts(all_facts, care_fact_ids, limit=4)
        durable = _pin_durable_facts(all_facts, limit=4)
        # Care roles first, then other durable pins.
        pinned: list[Fact] = []
        seen_pin: set[str] = set()
        for f in care_pinned + durable:
            if f.fact_id in seen_pin:
                continue
            seen_pin.add(f.fact_id)
            pinned.append(f)
            if len(pinned) >= 6:
                break
        pinned_ids = [f.fact_id for f in pinned]
        care_role_fact_ids = [f.fact_id for f in care_pinned]

        query_text = f"{input_.frame.subject}. Stakes: {input_.frame.stakes}. Options: " + ", ".join(
            input_.frame.options
        )
        embeddings = await self._embedder.embed(texts=[query_text])
        vector_ids: list[str] = []
        if embeddings:
            hits: list[VectorHit] = await self._vectors.query(
                user_id=input_.user_id,
                embedding=embeddings[0],
                top_k=input_.top_k,
            )
            vector_ids = [h.fact_id for h in hits]

        # Merge: pinned first, then vector fills, unique.
        ordered_fact_ids: list[str] = []
        seen: set[str] = set()
        for fid in pinned_ids + vector_ids:
            if fid in seen:
                continue
            seen.add(fid)
            ordered_fact_ids.append(fid)
            if len(ordered_fact_ids) >= input_.top_k + 2:
                break

        # Include contradiction-linked facts.
        snapshot = await self._manifestos.get_profile_snapshot(user_id=input_.user_id)
        contradiction_summaries: list[str] = []
        if snapshot:
            for c in snapshot.contradictions:
                if c.status is BulletStatus.REJECTED:
                    continue
                contradiction_summaries.append(c.summary)
                for fid in (c.fact_id_a, c.fact_id_b):
                    if fid in {"none", ""}:
                        continue
                    if fid not in seen:
                        seen.add(fid)
                        ordered_fact_ids.append(fid)
        if care and care.conflict_summaries:
            for s in care.conflict_summaries:
                if s not in contradiction_summaries:
                    contradiction_summaries.append(s)

        facts = await self._facts.get_many(user_id=input_.user_id, fact_ids=ordered_fact_ids)
        fact_id_set = {f.fact_id for f in facts}
        ordered_fact_ids = [fid for fid in ordered_fact_ids if fid in fact_id_set]
        # Preserve order for rendering.
        fact_by_id = {f.fact_id: f for f in facts}
        ordered_facts = [fact_by_id[fid] for fid in ordered_fact_ids if fid in fact_by_id]

        manifesto = await self._manifestos.get_current_manifesto(user_id=input_.user_id)
        manifesto_snippet = manifesto.statement[:800] if manifesto else None
        care_snip = care_profile_snippet(care) or None

        if not ordered_fact_ids and manifesto_snippet is None and care_snip is None:
            return RetrievedEvidence(
                fact_ids=[],
                manifesto_snippet=None,
                coverage_note="No prior signals or manifesto — Level has little context on this decision yet.",
                contradiction_summaries=[],
                care_profile_snippet=None,
                care_role_fact_ids=[],
                written_by=f"{NAME}@{VERSION}",
            )

        rendered_facts = (
            "\n".join(
                f"- [{f.fact_id}] {f.type.value}: {f.statement}" for f in ordered_facts
            )
            or "(no matching facts)"
        )
        rendered_contra = (
            "\n".join(f"- {s}" for s in contradiction_summaries[:5]) or "(none detected)"
        )
        prompt = PROMPT.format(
            subject=input_.frame.subject,
            options=", ".join(input_.frame.options),
            stakes=input_.frame.stakes,
            fact_list=rendered_facts,
            contradictions=rendered_contra,
            manifesto_snippet=manifesto_snippet or "(none)",
        )
        response = await self._gemini.generate(
            GenerationRequest(
                prompt=prompt,
                model_id=self._model_id,
                system_instruction=SYSTEM_INSTRUCTION,
                response_schema=_RetrieverOutput.model_json_schema(),
                temperature=0.1,
                max_output_tokens=256,
                metadata={"agent": NAME, "version": VERSION},
            )
        )
        parsed = parse_output(NAME, response.text, _RetrieverOutput)
        return RetrievedEvidence(
            fact_ids=ordered_fact_ids,
            manifesto_snippet=manifesto_snippet,
            coverage_note=parsed.coverage_note,
            contradiction_summaries=contradiction_summaries[:5],
            care_profile_snippet=care_snip,
            care_role_fact_ids=care_role_fact_ids,
            written_by=f"{NAME}@{VERSION}",
        )


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
        allowed_tools=[
            "embed_query",
            "vector_search",
            "get_facts",
            "get_manifesto",
            "get_care_profile",
        ],
        description=DESCRIPTION,
    )


__all__ = [
    "DESCRIPTION",
    "NAME",
    "PROMPT",
    "Retriever",
    "RetrieverInput",
    "SYSTEM_INSTRUCTION",
    "VERSION",
    "build_version",
]
