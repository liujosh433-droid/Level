"""Shared pytest fixtures.

Everything a test needs to exercise Level without touching GCP:

- ``settings``: process settings (local mode, ephemeral values).
- ``memory``: an in-memory :class:`MemoryBank`.
- ``fake_gemini`` / ``fake_embedder``: deterministic model clients.
- ``registry``: an in-memory :class:`AgentRegistry`.
- ``gateway``: an :class:`AgentGateway` with the default policy.
- ``conductor``: a fully-assembled :class:`Conductor` wired to the fakes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("LEVEL_ENV", "local")
os.environ.setdefault("GOOGLE_API_KEY", "test-key-for-local-fakes")
os.environ.setdefault("LEVEL_OTEL_EXPORTER", "none")


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Ensure every test starts with a fresh Settings object."""
    from level_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def settings():  # type: ignore[no-untyped-def]
    from level_core.config import get_settings

    return get_settings()


@pytest.fixture()
def memory():  # type: ignore[no-untyped-def]
    from level_core.memory.fakes import build_in_memory_bank

    return build_in_memory_bank()


@pytest.fixture()
def fake_gemini():  # type: ignore[no-untyped-def]
    from level_core.models.fakes import FakeGeminiClient

    return FakeGeminiClient()


@pytest.fixture()
def fake_embedder():  # type: ignore[no-untyped-def]
    from level_core.models.fakes import FakeEmbeddingClient

    return FakeEmbeddingClient()


@pytest.fixture()
def registry():  # type: ignore[no-untyped-def]
    from level_core.agents.registry import InMemoryAgentRegistry

    return InMemoryAgentRegistry()


@pytest.fixture()
def gateway():  # type: ignore[no-untyped-def]
    from level_core.gateway.router import AgentGateway

    return AgentGateway()


@pytest.fixture()
def outbound_guardrail():  # type: ignore[no-untyped-def]
    from level_core.guardrails.model_armor import LocalHeuristicModelArmor
    from level_core.guardrails.outbound import OutboundGuardrail

    return OutboundGuardrail(client=LocalHeuristicModelArmor())


@pytest.fixture()
def conductor(memory, fake_gemini, fake_embedder, settings, outbound_guardrail):  # type: ignore[no-untyped-def]
    from level_core.agents.conductor import build_conductor

    return build_conductor(
        memory=memory,
        gemini=fake_gemini,
        embedder=fake_embedder,
        guardrail=outbound_guardrail,
        settings=settings,
    )
