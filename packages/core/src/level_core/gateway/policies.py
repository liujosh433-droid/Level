"""Declarative gateway policy — which agents may call which tools.

Kept as Python dataclasses (not YAML) so misconfiguration is caught at
import time by the type checker. Every agent's registered tool set is
declared here; the ``AgentGateway`` refuses to invoke anything not on the
matching allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Per-tool policy — the rate limit is per agent, per rolling window."""

    tool_name: str
    rate_limit_per_minute: int = 30

    def __post_init__(self) -> None:
        if self.rate_limit_per_minute < 1:
            raise ValueError(
                f"tool {self.tool_name!r}: rate limit must be positive, got {self.rate_limit_per_minute}"
            )


@dataclass(frozen=True, slots=True)
class GatewayPolicy:
    """Full gateway policy — which tools are exposed to which agents."""

    tools_by_agent: dict[str, tuple[ToolPolicy, ...]] = field(default_factory=dict)

    def is_permitted(self, *, agent_name: str, tool_name: str) -> bool:
        return any(t.tool_name == tool_name for t in self.tools_by_agent.get(agent_name, ()))

    def tool_policy(self, *, agent_name: str, tool_name: str) -> ToolPolicy | None:
        for policy in self.tools_by_agent.get(agent_name, ()):
            if policy.tool_name == tool_name:
                return policy
        return None


# --- Default policy ---------------------------------------------------------
#
# Change these lists when adding a new tool or extending an agent's
# permissions. This is intentionally verbose — being explicit about which
# agent can call which tool is the whole point.

_DEFAULT_TOOLS_BY_AGENT: Final[dict[str, tuple[ToolPolicy, ...]]] = {
    "framer": (
        ToolPolicy("get_recent_signals", rate_limit_per_minute=10),
    ),
    "retriever": (
        ToolPolicy("embed_query", rate_limit_per_minute=60),
        ToolPolicy("vector_search", rate_limit_per_minute=60),
        ToolPolicy("get_facts", rate_limit_per_minute=60),
        ToolPolicy("get_manifesto", rate_limit_per_minute=10),
        ToolPolicy("get_care_profile", rate_limit_per_minute=30),
    ),
    "challenger": (
        ToolPolicy("get_facts", rate_limit_per_minute=60),
        ToolPolicy("get_manifesto", rate_limit_per_minute=10),
        ToolPolicy("get_bias_profile", rate_limit_per_minute=10),
    ),
    "judge": (
        ToolPolicy("append_bias_event", rate_limit_per_minute=30),
        ToolPolicy("get_bias_profile", rate_limit_per_minute=10),
    ),
    "ingest_normalizer": (
        ToolPolicy("upsert_fact", rate_limit_per_minute=120),
        ToolPolicy("upsert_signal", rate_limit_per_minute=120),
        ToolPolicy("embed_query", rate_limit_per_minute=120),
        ToolPolicy("upsert_vector", rate_limit_per_minute=120),
    ),
    "conductor": (
        ToolPolicy("create_decision", rate_limit_per_minute=30),
        ToolPolicy("append_turn", rate_limit_per_minute=60),
    ),
}


def load_default_policy() -> GatewayPolicy:
    return GatewayPolicy(tools_by_agent=dict(_DEFAULT_TOOLS_BY_AGENT))


__all__ = ["GatewayPolicy", "ToolPolicy", "load_default_policy"]
