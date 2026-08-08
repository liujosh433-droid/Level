"""Tests for the in-memory AgentRegistry."""

from __future__ import annotations

import pytest

from level_core.agents.registry import InMemoryAgentRegistry
from level_core.errors import AgentUnavailable
from level_core.schemas.agent import AgentVersion


def _version(prompt_sha: str = "0" * 64, version: str = "v1.0.0") -> AgentVersion:
    return AgentVersion(
        name="challenger",
        version=version,
        prompt_sha=prompt_sha,
        model_id="gemini-3.5-pro",
        owner="level-team",
        description="test",
    )


class TestInMemoryAgentRegistry:
    async def test_register_and_get_current(self) -> None:
        registry = InMemoryAgentRegistry()
        await registry.register(_version())
        got = await registry.get_current("challenger")
        assert got.name == "challenger"
        assert got.model_id == "gemini-3.5-pro"

    async def test_registering_same_version_with_different_prompt_raises(self) -> None:
        registry = InMemoryAgentRegistry()
        await registry.register(_version(prompt_sha="a" * 64))
        with pytest.raises(AgentUnavailable, match="different prompt_sha"):
            await registry.register(_version(prompt_sha="b" * 64))

    async def test_register_new_version_updates_current(self) -> None:
        registry = InMemoryAgentRegistry()
        await registry.register(_version(version="v1.0.0", prompt_sha="a" * 64))
        await registry.register(_version(version="v2.0.0", prompt_sha="b" * 64))
        got = await registry.get_current("challenger")
        assert got.version == "v2.0.0"

    async def test_get_current_unregistered_raises(self) -> None:
        registry = InMemoryAgentRegistry()
        with pytest.raises(AgentUnavailable):
            await registry.get_current("challenger")

    async def test_list_agents(self) -> None:
        registry = InMemoryAgentRegistry()
        await registry.register(_version(version="v1.0.0", prompt_sha="a" * 64))
        await registry.register(_version(version="v2.0.0", prompt_sha="b" * 64))
        agents = await registry.list_agents()
        assert len(agents) == 1
        assert agents[0].current_version == "v2.0.0"
