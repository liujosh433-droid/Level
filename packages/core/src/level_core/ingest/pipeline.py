"""End-to-end ingest pipeline for one Signal.

Flow:
  1. Inbound Model Armor (block prompt injection / redact PII)
  2. Persist sanitized Signal
  3. IngestNormalizer → Facts
  4. Persist Facts + upsert embeddings into Vector Store

Used by Cloud Run Jobs and by the manual ``POST /v1/ingest/signal`` path
(the API route currently only does steps 1–2; jobs do the full chain).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from level_core.agents.ingest_normalizer import IngestNormalizer, IngestNormalizerInput
from level_core.errors import GuardrailBlocked, ModelUnavailable
from level_core.guardrails.inbound import InboundGuardrail
from level_core.memory.base import MemoryBank
from level_core.models.base import EmbeddingClient
from level_core.observability.audit import AuditEventKind, write_audit_event
from level_core.observability.logger import get_logger
from level_core.observability.tracer import traced
from level_core.schemas.signal import Fact, FactType, Signal, SignalSource

_logger = get_logger(__name__)


@dataclass(slots=True)
class IngestResult:
    """Outcome of running the pipeline on one signal."""

    signal: Signal | None
    facts: list[Fact] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""
    skipped_duplicate: bool = False


@dataclass(slots=True)
class IngestPipeline:
    """Compose guardrail + normalizer + memory writes."""

    memory: MemoryBank
    normalizer: IngestNormalizer
    embedder: EmbeddingClient
    guardrail: InboundGuardrail

    @traced("ingest.pipeline.run")
    async def run(self, signal: Signal) -> IngestResult:
        """Ingest one signal end-to-end. Never raises for guardrail blocks."""
        # Idempotency: if we already have this external_id for this source, skip.
        existing = await self.memory.signals.list_by_source(
            user_id=signal.user_id, source=signal.source.value
        )
        if any(s.external_id == signal.external_id for s in existing):
            _logger.info(
                "ingest_skip_duplicate",
                user_id=signal.user_id,
                source=signal.source.value,
                external_id=signal.external_id,
            )
            return IngestResult(signal=None, skipped_duplicate=True)

        try:
            sanitized = self.guardrail.sanitize(signal)
        except GuardrailBlocked as exc:
            write_audit_event(
                AuditEventKind.GUARDRAIL_BLOCKED,
                subject=f"ingest:{signal.source.value}:{signal.external_id}",
                user_id=signal.user_id,
                reason=exc.reason,
            )
            return IngestResult(signal=None, blocked=True, block_reason=exc.reason)

        clean = sanitized.signal
        await self.memory.signals.upsert(clean)

        # Calendar events are already structured — skip the Normalizer LLM
        # so bulk Google sync doesn't burn Gemini quota.
        if clean.source is SignalSource.GCAL and clean.text:
            # google_live already formats "On my calendar …: title"
            statement = clean.text.replace("Calendar: ", "", 1).split("\n", 1)[0].strip()[:500]
            facts = [
                Fact(
                    user_id=clean.user_id,
                    type=FactType.EVENT,
                    statement=statement,
                    source_signal_ids=[clean.signal_id],
                    confidence=0.9,
                    salience=0.55,
                )
            ]
        else:
            try:
                facts = await self.normalizer.run(IngestNormalizerInput(signal=clean))
            except ModelUnavailable:
                _logger.warning(
                    "ingest_normalizer_quota",
                    user_id=clean.user_id,
                    source=clean.source.value,
                )
                raise

        persisted: list[Fact] = []
        for fact in facts:
            await self.memory.facts.upsert(fact)
            try:
                embeddings = await self.embedder.embed(texts=[fact.statement])
            except ModelUnavailable:
                embeddings = []
            if embeddings:
                await self.memory.vectors.upsert(
                    user_id=fact.user_id,
                    fact_id=fact.fact_id,
                    text=fact.statement,
                    embedding=embeddings[0],
                )
            persisted.append(fact)

        write_audit_event(
            AuditEventKind.INGEST_ACCEPTED,
            subject=f"signal:{clean.signal_id}",
            user_id=clean.user_id,
            source=clean.source.value,
            fact_count=len(persisted),
            redacted=sanitized.redacted,
        )
        _logger.info(
            "ingest_complete",
            user_id=clean.user_id,
            source=clean.source.value,
            fact_count=len(persisted),
        )
        return IngestResult(signal=clean, facts=persisted)


__all__ = ["IngestPipeline", "IngestResult"]
