"""Agent Gateway — policy-enforcing router between agents and tools.

Agents never call tools directly. They call ``gateway.invoke(tool_name, ...)``
which enforces:

- Per-agent tool allowlist (what tools this agent is permitted to call).
- Per-agent, per-tool rate limits (defensive: prevents an agent that's
  looping on a hallucination from burning through cost or hitting API
  quotas).
- Observability: every invocation is an OTel span with agent + tool + args
  size.

The gateway is in-process — this is not an HTTP hop. In cloud mode we can
optionally route tool calls through Apigee for enterprise features, but for
the hackathon the in-process router is enough to satisfy the "unified
routing and policy enforcement" rubric requirement.
"""

from level_core.gateway.policies import GatewayPolicy, ToolPolicy, load_default_policy
from level_core.gateway.router import AgentGateway, ToolHandler

__all__ = [
    "AgentGateway",
    "GatewayPolicy",
    "ToolHandler",
    "ToolPolicy",
    "load_default_policy",
]
