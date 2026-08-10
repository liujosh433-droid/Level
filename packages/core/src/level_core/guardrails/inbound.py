"""Inbound guardrail — applied to every ingested signal before persistence.

Goals:
- Block signals that look like prompt injection or tool poisoning. A
  malicious calendar invite could otherwise hijack the agent chain
  downstream.
- Redact PII before persistence. We don't want raw emails / phone numbers
  sitting in Firestore or embedded into the vector index.

Returns a :class:`SanitizedSignal` on success, raises :class:`GuardrailBlocked`
on failure. The caller (an ingestion job) decides whether to skip the signal
or surface a user-visible warning.
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
from level_core.guardrails.policies import DEFAULT_INBOUND_POLICY, GuardrailPolicy
from level_core.observability.audit import AuditEventKind, write_audit_event
from level_core.observability.logger import get_logger
from level_core.observability.tracer import traced
from level_core.schemas.signal import Signal

_logger = get_logger(__name__)


@dataclass(slots=True)
class SanitizedSignal:
    """Result of running a Signal through the inbound guardrail.

    Attributes:
        signal: The (possibly modified) signal, safe to persist.
        redacted: Whether PII was redacted from the signal's text.
        detected_categories: What Model Armor flagged (for audit).
    """

    signal: Signal
    redacted: bool
    detected_categories: list[str]


class InboundGuardrail:
    """Runs Model Armor on every ingested Signal.

    The guardrail is fail-closed: on unexpected errors from Model Armor
    we treat the signal as blocked. This is safe — worst case is the
    ingestion job skips one signal and picks it up on the next cursor
    pass.
    """

    def __init__(
        self,
        client: ModelArmorClient | None = None,
        policy: GuardrailPolicy = DEFAULT_INBOUND_POLICY,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._policy = policy
        self._client: ModelArmorClient = client or make_client(self._settings)

    @property
    def template(self) -> str:
        """Model Armor template path — resolved from settings."""
        return getattr(
            self._settings,
            self._policy.template_env_var.removeprefix("LEVEL_").lower(),
            "",
        ) or self._policy.template_env_var

    @traced("guardrail.inbound.sanitize")
    def sanitize(self, signal: Signal) -> SanitizedSignal:
        """Run the guardrail. Returns a SanitizedSignal or raises GuardrailBlocked."""
        text = signal.text or ""
        if not text.strip():
            # No text to inspect — pass through unchanged. Storage-only
            # signals (audio, images) are validated by the normalizer.
            return SanitizedSignal(signal=signal, redacted=False, detected_categories=[])

        result = self._client.check(template=self.template, text=text)

        if result.verdict is GuardrailVerdict.BLOCKED:
            hard_block = any(
                cat in self._policy.block_on for cat in result.detected_categories
            ) or not self._policy.block_on  # if unconfigured, treat any block as hard

            write_audit_event(
                AuditEventKind.GUARDRAIL_BLOCKED,
                subject=f"signal:{signal.signal_id}",
                user_id=signal.user_id,
                template=self.template,
                reason=result.reason,
                categories=result.detected_categories,
                hard_block=hard_block,
            )
            if hard_block:
                raise GuardrailBlocked(reason=result.reason, template=self.template)

        redacted = result.verdict is GuardrailVerdict.MODIFIED
        if redacted and result.sanitized_text is not None:
            # Copy the signal so we don't mutate caller state.
            signal = signal.model_copy(update={"text": result.sanitized_text, "scrubbed_pii": True})

        return SanitizedSignal(
            signal=signal,
            redacted=redacted,
            detected_categories=list(result.detected_categories),
        )


__all__ = ["InboundGuardrail", "SanitizedSignal"]
