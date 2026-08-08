"""Client wrapper around Vertex AI Model Armor.

Model Armor exposes template-based content guardrails: you configure a
template in the Cloud Console (or via Terraform) with checks for PII,
prompt injection, tool poisoning, hate speech, etc., and then call the
template with a payload to get a verdict.

Our wrapper hides the (verbose) protobuf request/response and normalizes
everything into :class:`GuardrailResult`.

In local mode the client returns a benign :class:`GuardrailVerdict.PASS`
for well-formed input and applies a small set of heuristic pattern matches
so the outbound guardrail can still catch obvious hallucinated citations
in tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from level_core.config import Settings, get_settings
from level_core.observability.logger import get_logger

_logger = get_logger(__name__)


class GuardrailVerdict(str, Enum):
    PASS = "pass"
    BLOCKED = "blocked"
    MODIFIED = "modified"


@dataclass(slots=True)
class GuardrailResult:
    """Normalized guardrail verdict."""

    verdict: GuardrailVerdict
    reason: str = ""
    sanitized_text: str | None = None
    detected_categories: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict is GuardrailVerdict.BLOCKED

    @property
    def modified(self) -> bool:
        return self.verdict is GuardrailVerdict.MODIFIED


class ModelArmorClient(Protocol):
    """The interface every Model Armor client (real or fake) implements."""

    def check(self, *, template: str, text: str) -> GuardrailResult:  # noqa: D401
        """Send ``text`` to the named Model Armor template and return the verdict."""
        ...


# --- Local heuristic client (for dev + tests) -------------------------------

_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Match "ignore/disregard all|previous|prior … instructions" with optional
    # adjectives between (e.g. "ignore all previous instructions").
    re.compile(
        r"\b(?:ignore|disregard)\b(?:\s+\w+){0,4}\s+instructions?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bsystem prompt\b.*\boverride\b", re.IGNORECASE),
    re.compile(r"\byou are now\b.*\b(unrestricted|dan|jailbroken|no rules)\b", re.IGNORECASE),
    re.compile(r"</?system>", re.IGNORECASE),
)

_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
    ("phone_us", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,16}\b")),
)


class LocalHeuristicModelArmor:
    """A fake Model Armor client used in local + test modes.

    Not a security boundary — never rely on this in production. It exists to
    exercise the guardrail control flow in the absence of Vertex Model Armor.
    """

    def check(self, *, template: str, text: str) -> GuardrailResult:
        for pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(text):
                return GuardrailResult(
                    verdict=GuardrailVerdict.BLOCKED,
                    reason=f"local heuristic matched prompt-injection pattern: {pattern.pattern!r}",
                    detected_categories=["prompt_injection"],
                )

        sanitized = text
        detected: list[str] = []
        for label, pattern in _PII_PATTERNS:
            if pattern.search(sanitized):
                detected.append(f"pii:{label}")
                sanitized = pattern.sub(f"[REDACTED:{label}]", sanitized)

        if detected:
            return GuardrailResult(
                verdict=GuardrailVerdict.MODIFIED,
                reason="local heuristic redacted PII",
                sanitized_text=sanitized,
                detected_categories=detected,
            )

        return GuardrailResult(
            verdict=GuardrailVerdict.PASS,
            reason=f"local heuristic clean (template={template})",
        )


# --- Vertex client -----------------------------------------------------------


class VertexModelArmor:
    """Real Model Armor client backed by Vertex AI.

    Kept import-lazy so local-mode processes don't need the aiplatform SDK
    initialized to import ``level_core.guardrails``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def check(self, *, template: str, text: str) -> GuardrailResult:
        # Delegating to the raw Model Armor client is done lazily so we don't
        # take a hard dep on the underlying package at import time.
        try:
            from google.cloud import modelarmor_v1  # type: ignore[import-not-found]
        except ImportError:
            _logger.warning(
                "model_armor_sdk_missing",
                message="falling back to local heuristic",
            )
            return LocalHeuristicModelArmor().check(template=template, text=text)

        client = modelarmor_v1.ModelArmorClient()
        request = modelarmor_v1.SanitizeUserPromptRequest(
            name=template,
            user_prompt_data=modelarmor_v1.DataItem(text=text),
        )
        response = client.sanitize_user_prompt(request=request)

        # Response contains a match state per filter category. We consider a
        # verdict BLOCKED if any filter matched with high severity.
        if response.sanitization_result.filter_match_state == "MATCH_FOUND":
            return GuardrailResult(
                verdict=GuardrailVerdict.BLOCKED,
                reason="Model Armor filter matched",
                detected_categories=[
                    str(f) for f in response.sanitization_result.filter_results
                ],
            )

        return GuardrailResult(
            verdict=GuardrailVerdict.PASS,
            reason=f"Model Armor pass ({template})",
        )


def make_client(settings: Settings | None = None) -> ModelArmorClient:
    """Return the appropriate client for the current runtime mode."""
    settings = settings or get_settings()
    if settings.is_local:
        return LocalHeuristicModelArmor()
    return VertexModelArmor(settings=settings)


__all__ = [
    "GuardrailResult",
    "GuardrailVerdict",
    "LocalHeuristicModelArmor",
    "ModelArmorClient",
    "VertexModelArmor",
    "make_client",
]
