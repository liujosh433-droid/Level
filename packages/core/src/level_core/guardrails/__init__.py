"""Model Armor guardrails — inbound (ingest) and outbound (challenger).

Every ingested signal passes through :class:`InboundGuardrail` before being
persisted. Every Challenger response passes through :class:`OutboundGuardrail`
before being streamed to the user.

In cloud mode both guardrails call Vertex AI Model Armor. In local mode
they use lightweight local heuristics — enough to keep tests green without
requiring a network call.
"""

from level_core.guardrails.inbound import InboundGuardrail
from level_core.guardrails.model_armor import (
    GuardrailResult,
    GuardrailVerdict,
    ModelArmorClient,
)
from level_core.guardrails.outbound import OutboundGuardrail
from level_core.guardrails.policies import DEFAULT_INBOUND_POLICY, DEFAULT_OUTBOUND_POLICY

__all__ = [
    "DEFAULT_INBOUND_POLICY",
    "DEFAULT_OUTBOUND_POLICY",
    "GuardrailResult",
    "GuardrailVerdict",
    "InboundGuardrail",
    "ModelArmorClient",
    "OutboundGuardrail",
]
