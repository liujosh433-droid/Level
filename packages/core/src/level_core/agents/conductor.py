"""Conductor — orchestrates a single turn end-to-end.

The Conductor is Level's own deterministic multi-agent orchestrator. It
sequences Framer → Retriever → Challenger → Judge and handles:

- Turn persistence (writes each partial state to the DecisionRepository so
  the UI can subscribe via Firestore ``onSnapshot`` and render as we go).
- Retry-once-with-stricter-schema on invalid agent output.
- Degradation: if a sub-agent fails after retry, the Turn is marked
  DEGRADED and Level admits it to the user rather than fabricating.
- Outbound guardrail enforcement on Challenger response.

We keep this as our own orchestration (rather than ADK's Runner) so we
have tight control over failure paths and structured-output validation.
ADK's ``SequentialAgent`` counterpart is still constructed via
:func:`build_adk_sequential_agent` and registered in the Agent Registry —
it's the "Google Agent Framework" declaration for compliance and can be
swapped in as the execution path later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from level_core.agents import challenger as challenger_module
from level_core.agents import framer as framer_module
from level_core.agents import judge as judge_module
from level_core.agents import retriever as retriever_module
from level_core.agents.challenger import Challenger, ChallengerInput
from level_core.agents.framer import Framer, FramerInput
from level_core.agents.judge import Judge, JudgeInput
from level_core.agents.registry import AgentRegistry
from level_core.agents.retriever import Retriever, RetrieverInput
from level_core.config import Settings, get_settings
from level_core.errors import GuardrailBlocked, InvalidAgentOutput
from level_core.guardrails.outbound import OutboundGuardrail
from level_core.memory.base import MemoryBank
from level_core.observability.audit import AuditEventKind, write_audit_event
from level_core.observability.logger import get_logger
from level_core.observability.tracer import current_trace_id, traced
from level_core.schemas.decision import Decision, DecisionStatus
from level_core.schemas.turn import (
    ChallengeQuestion,
    Turn,
    TurnRole,
    TurnStatus,
)

if TYPE_CHECKING:
    from google.adk.agents import LlmAgent, SequentialAgent

_logger = get_logger(__name__)


@dataclass(slots=True)
class SessionInput:
    """Input for one turn of a session."""

    user_id: str
    decision_id: str
    user_text: str
    manifesto_snippet: str = ""


@dataclass(slots=True)
class Conductor:
    """The session-orchestrator."""

    framer: Framer
    retriever: Retriever
    challenger: Challenger
    judge: Judge
    memory: MemoryBank
    guardrail: OutboundGuardrail
    settings: Settings

    @traced("conductor.run_turn")
    async def run_turn(self, input_: SessionInput) -> Turn:
        """Run one turn end-to-end. Persists the resulting Turn."""
        trace_id = current_trace_id()
        decision = await self._ensure_decision(input_)

        turn = Turn(
            user_id=input_.user_id,
            decision_id=input_.decision_id,
            role=TurnRole.LEVEL,
            status=TurnStatus.PENDING,
            user_text=input_.user_text,
            trace_id=trace_id,
        )
        await self.memory.decisions.append_turn(turn)

        # 1. Frame (only if we don't already have a frame).
        try:
            if decision.frame is None:
                frame = await self.framer.run(
                    FramerInput(
                        user_prompt=input_.user_text,
                        recent_signals_summary="",  # ingestion job already normalized signals
                        manifesto_snippet=input_.manifesto_snippet,
                    )
                )
                decision = decision.model_copy(update={"frame": frame})
                await self.memory.decisions.update(decision)
            else:
                frame = decision.frame
        except InvalidAgentOutput as exc:
            return await self._degrade(turn, reason=f"framer: {exc.validation_error}")

        # 2. Retrieve evidence.
        try:
            evidence = await self.retriever.run(
                RetrieverInput(user_id=input_.user_id, frame=frame, top_k=8)
            )
            turn = turn.model_copy(update={"retrieved_evidence": evidence})
        except InvalidAgentOutput as exc:
            return await self._degrade(turn, reason=f"retriever: {exc.validation_error}")

        # Load the facts we retrieved so we can pass rich context to Challenger.
        facts = await self.memory.facts.get_many(
            user_id=input_.user_id, fact_ids=evidence.fact_ids
        )
        bias_profile = await self.memory.manifestos.get_bias_profile(user_id=input_.user_id)
        prior_turns = await self.memory.decisions.list_turns(
            user_id=input_.user_id, decision_id=input_.decision_id
        )

        # 3. Challenge.
        try:
            questions = await self.challenger.run(
                ChallengerInput(
                    frame=frame,
                    retrieved_facts=facts,
                    manifesto_snippet=evidence.manifesto_snippet,
                    bias_profile=bias_profile,
                    prior_turns=prior_turns,
                    user_text=input_.user_text,
                    coverage_note=evidence.coverage_note or "",
                    contradiction_summaries=list(evidence.contradiction_summaries or []),
                )
            )
        except InvalidAgentOutput as exc:
            return await self._degrade(turn, reason=f"challenger: {exc.validation_error}")

        # 4. Outbound guardrail — block hallucinated citations before user sees them.
        try:
            self.guardrail.enforce(
                questions=questions,
                available_fact_ids={f.fact_id for f in facts},
                user_id=input_.user_id,
            )
        except GuardrailBlocked as exc:
            return await self._degrade(
                turn,
                reason=f"guardrail: {exc.reason}",
                status=TurnStatus.BLOCKED,
            )

        # 5. Judge — score biases from this turn.
        try:
            bias_events = await self.judge.run(
                JudgeInput(
                    frame=frame,
                    user_text=input_.user_text,
                    challenger_questions=questions,
                ),
                user_id=input_.user_id,
                decision_id=input_.decision_id,
                turn_id=turn.turn_id,
            )
        except InvalidAgentOutput as exc:
            # A judge failure is non-fatal — we still send the challenger's
            # questions to the user, just without bias events for this turn.
            _logger.warning("judge_failed", reason=exc.validation_error)
            bias_events = []

        for event in bias_events:
            await self.memory.turns.append_bias_event(event)

        if bias_events:
            from level_core.bias.profile import BiasAggregator

            update = BiasAggregator().update(profile=bias_profile, events=bias_events)
            await self.memory.manifestos.save_bias_profile(update.profile)

        turn = turn.model_copy(
            update={
                "status": TurnStatus.COMPLETE,
                "challenger_questions": questions,
                "bias_event_ids": [e.event_id for e in bias_events],
            }
        )
        turn.touch()
        await self.memory.decisions.append_turn(turn)

        write_audit_event(
            AuditEventKind.AGENT_INVOKED,
            subject=f"conductor.turn:{turn.turn_id}",
            user_id=input_.user_id,
            question_count=len(questions),
            bias_event_count=len(bias_events),
        )
        return turn

    async def _ensure_decision(self, input_: SessionInput) -> Decision:
        """Return the Decision doc, creating a placeholder if it doesn't exist."""
        try:
            return await self.memory.decisions.get(
                user_id=input_.user_id, decision_id=input_.decision_id
            )
        except Exception:  # noqa: BLE001
            decision = Decision(
                decision_id=input_.decision_id,
                user_id=input_.user_id,
                status=DecisionStatus.OPEN,
            )
            await self.memory.decisions.create(decision)
            return decision

    async def _degrade(
        self,
        turn: Turn,
        *,
        reason: str,
        status: TurnStatus = TurnStatus.DEGRADED,
    ) -> Turn:
        turn = turn.model_copy(
            update={"status": status, "degradation_reason": reason},
        )
        turn.touch()
        await self.memory.decisions.append_turn(turn)
        write_audit_event(
            AuditEventKind.AGENT_DEGRADED,
            subject=f"conductor.turn:{turn.turn_id}",
            user_id=turn.user_id,
            reason=reason,
            status=status.value,
        )
        return turn


# --- Assembly + ADK compliance ---------------------------------------------


def build_conductor(
    *,
    memory: MemoryBank,
    gemini: object,  # GeminiClient
    embedder: object,  # EmbeddingClient
    guardrail: OutboundGuardrail | None = None,
    settings: Settings | None = None,
) -> Conductor:
    """Assemble a Conductor with the given dependencies."""
    settings = settings or get_settings()
    guardrail = guardrail or OutboundGuardrail(settings=settings)
    from level_core.models.base import EmbeddingClient, GeminiClient  # runtime type refs

    gemini_client: GeminiClient = gemini  # type: ignore[assignment]
    embedding_client: EmbeddingClient = embedder  # type: ignore[assignment]

    framer = Framer(gemini=gemini_client, model_id=settings.reasoning_model)
    retriever = Retriever(
        gemini=gemini_client,
        embedder=embedding_client,
        vectors=memory.vectors,
        facts=memory.facts,
        manifestos=memory.manifestos,
        model_id=settings.fast_model,
    )
    challenger = Challenger(gemini=gemini_client, model_id=settings.reasoning_model)
    judge = Judge(gemini=gemini_client, model_id=settings.fast_model)

    return Conductor(
        framer=framer,
        retriever=retriever,
        challenger=challenger,
        judge=judge,
        memory=memory,
        guardrail=guardrail,
        settings=settings,
    )


async def register_all_agents(registry: AgentRegistry, settings: Settings) -> None:
    """Register every agent version with the Agent Registry.

    Called at process startup. Registration is idempotent — re-registering
    the same (name, version, prompt_sha) is a no-op.
    """
    await registry.register(framer_module.build_version(settings))
    await registry.register(retriever_module.build_version(settings))
    await registry.register(challenger_module.build_version(settings))
    await registry.register(judge_module.build_version(settings))
    from level_core.agents import ingest_normalizer as ingest_module

    await registry.register(ingest_module.build_version(settings))


def build_adk_sequential_agent(settings: Settings | None = None) -> SequentialAgent:
    """Assemble the pipeline as a Google ADK :class:`SequentialAgent`.

    This is our declaration of "using a Google Agent Framework." The
    returned SequentialAgent is registered in the Agent Registry alongside
    the individual LlmAgent versions. Our Conductor class remains the
    primary execution path (giving us structured-output validation and
    per-agent degradation); the ADK SequentialAgent is available for
    invocation via ``google.adk.runners.Runner`` when full ADK-native
    execution is desired.
    """
    settings = settings or get_settings()
    from google.adk.agents import LlmAgent, SequentialAgent

    def _mk(name: str, description: str, model_id: str, instruction: str) -> LlmAgent:
        return LlmAgent(
            name=name,
            model=model_id,
            description=description,
            instruction=instruction,
            output_key=f"{name}_output",
        )

    framer_agent = _mk(
        framer_module.NAME,
        framer_module.DESCRIPTION,
        settings.reasoning_model,
        framer_module.SYSTEM_INSTRUCTION + "\n\n" + framer_module.PROMPT,
    )
    retriever_agent = _mk(
        retriever_module.NAME,
        retriever_module.DESCRIPTION,
        settings.fast_model,
        retriever_module.SYSTEM_INSTRUCTION + "\n\n" + retriever_module.PROMPT,
    )
    challenger_agent = _mk(
        challenger_module.NAME,
        challenger_module.DESCRIPTION,
        settings.reasoning_model,
        challenger_module.SYSTEM_INSTRUCTION + "\n\n" + challenger_module.PROMPT,
    )
    judge_agent = _mk(
        judge_module.NAME,
        judge_module.DESCRIPTION,
        settings.fast_model,
        judge_module.SYSTEM_INSTRUCTION + "\n\n" + judge_module.PROMPT,
    )
    return SequentialAgent(
        name="conductor",
        description="Framer → Retriever → Challenger → Judge",
        sub_agents=[framer_agent, retriever_agent, challenger_agent, judge_agent],
    )


__all__ = [
    "Conductor",
    "SessionInput",
    "build_adk_sequential_agent",
    "build_conductor",
    "register_all_agents",
]
