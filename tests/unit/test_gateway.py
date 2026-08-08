"""Tests for the AgentGateway — allowlist enforcement + rate limits."""

from __future__ import annotations

import pytest

from level_core.errors import AgentUnavailable, RateLimitExceeded, ToolNotPermitted
from level_core.gateway.policies import GatewayPolicy, ToolPolicy
from level_core.gateway.router import AgentGateway


async def _echo(**kwargs: object) -> dict[str, object]:
    return dict(kwargs)


class TestAgentGateway:
    async def test_allowed_tool_is_dispatched(self) -> None:
        gateway = AgentGateway()
        gateway.register("get_manifesto", _echo)
        result = await gateway.invoke(
            agent_name="challenger", tool_name="get_manifesto", user_id="u1"
        )
        assert result == {"user_id": "u1"}

    async def test_missing_tool_raises_unavailable(self) -> None:
        gateway = AgentGateway()
        with pytest.raises(AgentUnavailable):
            await gateway.invoke(agent_name="challenger", tool_name="get_manifesto")

    async def test_disallowed_tool_raises_tool_not_permitted(self) -> None:
        gateway = AgentGateway()
        gateway.register("append_bias_event", _echo)
        # framer is not permitted to call append_bias_event per the default policy.
        with pytest.raises(ToolNotPermitted):
            await gateway.invoke(
                agent_name="framer", tool_name="append_bias_event", event=None
            )

    async def test_rate_limit_enforced(self) -> None:
        policy = GatewayPolicy(
            tools_by_agent={
                "challenger": (ToolPolicy("get_manifesto", rate_limit_per_minute=2),)
            }
        )
        gateway = AgentGateway(policy=policy)
        gateway.register("get_manifesto", _echo)

        await gateway.invoke(agent_name="challenger", tool_name="get_manifesto")
        await gateway.invoke(agent_name="challenger", tool_name="get_manifesto")

        with pytest.raises(RateLimitExceeded):
            await gateway.invoke(agent_name="challenger", tool_name="get_manifesto")

    async def test_double_register_raises(self) -> None:
        gateway = AgentGateway()
        gateway.register("get_manifesto", _echo)
        with pytest.raises(ValueError, match="already registered"):
            gateway.register("get_manifesto", _echo)
