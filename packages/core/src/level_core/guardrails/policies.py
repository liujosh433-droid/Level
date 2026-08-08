"""Guardrail policies — what we require of every inbound and outbound payload.

Kept as dataclasses (not YAML) so the type checker enforces they are used
correctly at every call site. YAML lives in ``infra/model_armor/policies.yaml``
for the actual Vertex Model Armor template configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class GuardrailPolicy:
    """Declarative policy describing what a guardrail enforces.

    Attributes:
        name: Human-readable name for logging.
        template_env_var: Name of the environment variable holding the
            Model Armor template resource path.
        block_on: Detected categories that MUST cause a block (not merely
            a redaction).
        redact_on: Detected categories that trigger sanitization but not
            a block.
        require_cited_facts: For outbound only — require every claim about
            the user's past to cite a fact id from a provided set.
    """

    name: str
    template_env_var: str
    block_on: frozenset[str] = field(default_factory=frozenset)
    redact_on: frozenset[str] = field(default_factory=frozenset)
    require_cited_facts: bool = False


DEFAULT_INBOUND_POLICY = GuardrailPolicy(
    name="inbound",
    template_env_var="LEVEL_MODEL_ARMOR_TEMPLATE_INBOUND",
    block_on=frozenset({"prompt_injection", "tool_poisoning", "malicious_url"}),
    redact_on=frozenset({"pii:email", "pii:phone_us", "pii:ssn", "pii:credit_card"}),
)


DEFAULT_OUTBOUND_POLICY = GuardrailPolicy(
    name="outbound",
    template_env_var="LEVEL_MODEL_ARMOR_TEMPLATE_OUTBOUND",
    block_on=frozenset(
        {
            "tool_poisoning",
            "leaked_credential",
            "hate_speech",
            "hallucinated_citation",
        }
    ),
    require_cited_facts=True,
)


__all__ = ["DEFAULT_INBOUND_POLICY", "DEFAULT_OUTBOUND_POLICY", "GuardrailPolicy"]
