"""Protocol definitions for Gemini + embedding clients.

Keeps the agent code free of any vendor-specific type dependencies. Every
agent function that needs an LLM accepts a :class:`GeminiClient` (protocol)
so tests can pass a :class:`FakeGeminiClient` without any monkey-patching.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """A single generation request to Gemini.

    Attributes:
        prompt: The user-facing prompt (typically already assembled from a
            template + retrieved context).
        model_id: Gemini model to invoke, e.g. "gemini-3.5-pro".
        response_schema: Optional JSON schema dict. When present, the model
            is expected to return JSON matching this schema.
        temperature: 0..1. Defaults to 0.2 (deterministic-ish for reasoning).
        max_output_tokens: Cap on response length.
        system_instruction: Optional persistent instruction (persona, tone).
        metadata: Arbitrary tags surfaced in observability spans.
    """

    prompt: str
    model_id: str
    response_schema: Mapping[str, Any] | None = None
    temperature: float = 0.2
    max_output_tokens: int = 2048
    system_instruction: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationResponse:
    """A single Gemini response."""

    text: str
    input_tokens: int
    output_tokens: int
    model_id: str
    finish_reason: str = "stop"


class GeminiClient(Protocol):
    """Every Gemini backend (AI Studio, Vertex, fake) implements this shape."""

    async def generate(self, request: GenerationRequest) -> GenerationResponse: ...


class EmbeddingClient(Protocol):
    """Every embedding backend implements this shape."""

    async def embed(self, *, texts: list[str]) -> list[list[float]]: ...


__all__ = [
    "EmbeddingClient",
    "GeminiClient",
    "GenerationRequest",
    "GenerationResponse",
]
