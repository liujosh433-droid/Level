"""AgentGateway — the in-process router agents call to invoke tools.

Every tool the system exposes to an agent is registered with the gateway
via :meth:`AgentGateway.register`. Agents then call
``gateway.invoke(agent_name, tool_name, **args)`` which:

1. Verifies the agent is permitted to call the tool (allowlist).
2. Applies a per-agent, per-tool sliding-window rate limit.
3. Opens an OpenTelemetry span for the invocation.
4. Dispatches to the registered handler.

Missing tools raise :class:`AgentUnavailable` (not :class:`ToolNotPermitted`)
so bugs are distinguishable from policy violations.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from level_core.errors import AgentUnavailable, RateLimitExceeded, ToolNotPermitted
from level_core.gateway.policies import GatewayPolicy, load_default_policy
from level_core.observability.audit import AuditEventKind, write_audit_event
from level_core.observability.logger import get_logger
from level_core.observability.tracer import traced

_logger = get_logger(__name__)


ToolHandler = Callable[..., Awaitable[Any]]


class AgentGateway:
    """Central router with policy enforcement.

    Thread-safety: the gateway is designed for single-event-loop concurrency
    (which is how FastAPI + ADK's Runner operate). Rate-limit windows use
    plain deques; if you need multi-process rate limiting, plug in Redis
    behind the same interface.
    """

    def __init__(self, policy: GatewayPolicy | None = None) -> None:
        self._policy = policy or load_default_policy()
        self._handlers: dict[str, ToolHandler] = {}
        # Sliding window of call timestamps per (agent_name, tool_name).
        self._windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def register(self, tool_name: str, handler: ToolHandler) -> None:
        """Register a tool handler.

        Handlers should accept keyword arguments only, be async, and return
        an object that is either JSON-serializable or a Pydantic model.
        """
        if tool_name in self._handlers:
            raise ValueError(f"tool already registered: {tool_name!r}")
        self._handlers[tool_name] = handler

    def registered_tools(self) -> list[str]:
        return sorted(self._handlers.keys())

    @traced("gateway.invoke")
    async def invoke(self, *, agent_name: str, tool_name: str, **kwargs: Any) -> Any:
        """Invoke a tool as ``agent_name``, subject to policy and rate limits."""
        if not self._policy.is_permitted(agent_name=agent_name, tool_name=tool_name):
            write_audit_event(
                AuditEventKind.TOOL_INVOCATION_DENIED,
                subject=f"{agent_name}->{tool_name}",
                reason="not in allowlist",
            )
            raise ToolNotPermitted(agent_name=agent_name, tool_name=tool_name)

        handler = self._handlers.get(tool_name)
        if handler is None:
            raise AgentUnavailable(f"tool {tool_name!r} is not registered with the gateway")

        policy = self._policy.tool_policy(agent_name=agent_name, tool_name=tool_name)
        assert policy is not None, "is_permitted was true but tool_policy returned None"

        window = self._windows[(agent_name, tool_name)]
        now = time.monotonic()
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= policy.rate_limit_per_minute:
            write_audit_event(
                AuditEventKind.RATE_LIMIT_EXCEEDED,
                subject=f"{agent_name}->{tool_name}",
                limit=policy.rate_limit_per_minute,
            )
            raise RateLimitExceeded(
                agent_name=agent_name,
                tool_name=tool_name,
                limit=policy.rate_limit_per_minute,
            )
        window.append(now)

        _logger.debug(
            "gateway.dispatch",
            agent=agent_name,
            tool=tool_name,
            arg_keys=sorted(kwargs.keys()),
        )
        return await handler(**kwargs)


__all__ = ["AgentGateway", "ToolHandler"]
