"""Outbound guardrail — applied to every Challenger response before streaming.

Two responsibilities:

1. Model Armor content check (leaked credentials, tool poisoning, hate).
2. Citation grounding: every ``fact_id`` cited in the response must exist
   in the pool the Retriever produced. This is a hallucination guard —
   it catches the Challenger inventing evidence.

The guardrail is designed to be run *after* the Challenger has produced
its structured output but *before* the response is committed to the
user-visible Turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from level_core.config import Settings, get_settings
from level_core.errors import GuardrailBlocked
from level_core.guardrails.model_armor import (
    GuardrailVerdict,
    ModelArmorClient,
    make_client,
)
from level_core.guardrails.policies import DEFAULT_OUTBOUND_POLICY, GuardrailPolicy
from level_core.observability.audit import AuditEventKind, write_audit_event
from level_core.observability.logger import get_logger
from level_core.observability.tracer import traced
from level_core.schemas.turn import ChallengeQuestion

_logger = get_logger(__name__)


@dataclass(slots=True)
class OutboundVerdict:
    """Outcome of running a Challenger response through the outbound guardrail."""

    approved: bool
    reason: str
    hallucinated_fact_ids: list[str]
    blocked_categories: list[str]


class OutboundGuardrail:
    """Guardrail that runs on every Challenger response."""

    def __init__(
        self,
        client: ModelArmorClient | None = None,
        policy: GuardrailPolicy = DEFAULT_OUTBOUND_POLICY,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._policy = policy
        self._client: ModelArmorClient = client or make_client(self._settings)

    @property
    def template(self) -> str:
        return getattr(
            self._settings,
            self._policy.template_env_var.removeprefix("LEVEL_").lower(),
            "",
        ) or self._policy.template_env_var

    @traced("guardrail.outbound.review")
    def review(
        self,
        *,
        questions: list[ChallengeQuestion],
        available_fact_ids: set[str],
        user_id: str | None = None,
    ) -> OutboundVerdict:
        """Review the Challenger's questions before they are sent to the user.

        Args:
            questions: The Challenger's structured output.
            available_fact_ids: The set of fact ids the Retriever produced.
                Any citation to a fact outside this set is treated as a
                hallucination.
            user_id: Attached to audit events; optional.

        Returns:
            An :class:`OutboundVerdict`. Callers should check ``approved``
            and use the reason for user-visible fallback messaging.
        """
        hallucinated: list[str] = []
        for question in questions:
            for citation in question.citations:
                if citation.fact_id not in available_fact_ids:
                    hallucinated.append(citation.fact_id)

        if hallucinated and self._policy.require_cited_facts:
            write_audit_event(
                AuditEventKind.HALLUCINATED_CITATION,
                subject="challenger_response",
                user_id=user_id,
                hallucinated_fact_ids=hallucinated,
            )
            return OutboundVerdict(
                approved=False,
                reason=f"cited {len(hallucinated)} fact id(s) not in retrieval pool",
                hallucinated_fact_ids=hallucinated,
                blocked_categories=["hallucinated_citation"],
            )

        combined_text = "\n".join(q.question for q in questions)
        if combined_text.strip():
            armor_result = self._client.check(template=self.template, text=combined_text)
            if armor_result.verdict is GuardrailVerdict.BLOCKED:
                blocked = [c for c in armor_result.detected_categories if c in self._policy.block_on]
                if blocked or not self._policy.block_on:
                    write_audit_event(
                        AuditEventKind.GUARDRAIL_BLOCKED,
                        subject="challenger_response",
                        user_id=user_id,
                        template=self.template,
                        reason=armor_result.reason,
                        categories=armor_result.detected_categories,
                    )
                    return OutboundVerdict(
                        approved=False,
                        reason=armor_result.reason,
                        hallucinated_fact_ids=[],
                        blocked_categories=blocked or list(armor_result.detected_categories),
                    )

        return OutboundVerdict(
            approved=True,
            reason="approved",
            hallucinated_fact_ids=[],
            blocked_categories=[],
        )

    def enforce(
        self,
        *,
        questions: list[ChallengeQuestion],
        available_fact_ids: set[str],
        user_id: str | None = None,
    ) -> None:
        """Convenience: review and raise :class:`GuardrailBlocked` if not approved."""
        verdict = self.review(
            questions=questions,
            available_fact_ids=available_fact_ids,
            user_id=user_id,
        )
        if not verdict.approved:
            raise GuardrailBlocked(reason=verdict.reason, template=self.template)


__all__ = ["OutboundGuardrail", "OutboundVerdict"]
