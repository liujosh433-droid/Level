"""Gemini client wrappers used by every agent.

The :class:`GeminiClient` protocol hides the difference between:

- AI Studio (local dev, free tier, API key)
- Vertex AI (production, service-account credentials)

Every agent depends on the protocol, not the concrete class, so tests can
substitute a :class:`FakeGeminiClient` with deterministic responses.
"""

from level_core.models.base import EmbeddingClient, GeminiClient, GenerationRequest, GenerationResponse
from level_core.models.factory import build_embedding_client, build_gemini_client
from level_core.models.fakes import FakeEmbeddingClient, FakeGeminiClient, ScriptedResponse

__all__ = [
    "EmbeddingClient",
    "FakeEmbeddingClient",
    "FakeGeminiClient",
    "GeminiClient",
    "GenerationRequest",
    "GenerationResponse",
    "ScriptedResponse",
    "build_embedding_client",
    "build_gemini_client",
]
