"""Factories for constructing the appropriate model clients per runtime mode."""

from __future__ import annotations

from level_core.config import Settings, get_settings
from level_core.models.base import EmbeddingClient, GeminiClient


def build_gemini_client(settings: Settings | None = None) -> GeminiClient:
    """Return the appropriate Gemini client.

    Always the real ``google-genai``-backed client — even in local mode we
    exercise the real API surface (AI Studio) so agent behavior matches
    what will happen in production. Tests substitute FakeGeminiClient
    explicitly.
    """
    settings = settings or get_settings()
    from level_core.models.gemini import GeminiGenAIClient

    return GeminiGenAIClient(settings=settings)


def build_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    settings = settings or get_settings()
    from level_core.models.gemini import GeminiEmbeddingClient

    return GeminiEmbeddingClient(settings=settings)


__all__ = ["build_embedding_client", "build_gemini_client"]
