"""Domain-specific exceptions.

Every error raised by Level's code should be one of these (or a subclass).
Raw ``Exception``s that escape to the top level are treated as bugs and
logged with full traceback.
"""

from __future__ import annotations


class LevelError(Exception):
    """Base class for every Level-specific error."""


# --- Configuration ---------------------------------------------------------

class ConfigError(LevelError):
    """The runtime environment is misconfigured."""


# --- Guardrails ------------------------------------------------------------

class GuardrailBlocked(LevelError):
    """Model Armor rejected an inbound or outbound payload.

    Attributes:
        reason: Human-readable reason (safe to log; not user-facing).
        template: Which Model Armor template blocked the payload.
    """

    def __init__(self, reason: str, template: str) -> None:
        super().__init__(f"Model Armor blocked payload ({template}): {reason}")
        self.reason = reason
        self.template = template


# --- Agents ----------------------------------------------------------------

class AgentError(LevelError):
    """Base class for agent-execution errors."""


class InvalidAgentOutput(AgentError):
    """An agent produced output that failed schema validation.

    Raised after all retries have been exhausted. The Conductor catches
    this and degrades the turn rather than propagating to the user.
    """

    def __init__(self, agent_name: str, validation_error: str) -> None:
        super().__init__(f"agent {agent_name!r} produced invalid output: {validation_error}")
        self.agent_name = agent_name
        self.validation_error = validation_error


class AgentTimeout(AgentError):
    """An agent invocation timed out."""


class AgentUnavailable(AgentError):
    """The agent isn't registered or its dependencies aren't available."""


# --- Gateway ---------------------------------------------------------------

class GatewayError(LevelError):
    """Base class for Agent Gateway policy errors."""


class ToolNotPermitted(GatewayError):
    """An agent tried to call a tool that isn't in its allowlist."""

    def __init__(self, agent_name: str, tool_name: str) -> None:
        super().__init__(f"agent {agent_name!r} may not call tool {tool_name!r}")
        self.agent_name = agent_name
        self.tool_name = tool_name


class RateLimitExceeded(GatewayError):
    """An agent exceeded its per-window call quota for a given tool."""

    def __init__(self, agent_name: str, tool_name: str, limit: int) -> None:
        super().__init__(
            f"agent {agent_name!r} exceeded rate limit for {tool_name!r} "
            f"(limit={limit} calls / window)"
        )
        self.agent_name = agent_name
        self.tool_name = tool_name
        self.limit = limit


# --- Memory Bank -----------------------------------------------------------

class MemoryError(LevelError):
    """Base class for Memory Bank errors."""


class NotFound(MemoryError):
    """A document was not found in the store."""

    def __init__(self, collection: str, doc_id: str) -> None:
        super().__init__(f"{collection}/{doc_id} not found")
        self.collection = collection
        self.doc_id = doc_id


class ConflictError(MemoryError):
    """A write failed due to a version / etag conflict."""


# --- Model layer -----------------------------------------------------------

class ModelError(LevelError):
    """Base class for Gemini call errors."""


class ModelBlocked(ModelError):
    """Gemini refused to answer (safety filter or content policy)."""


class ModelUnavailable(ModelError):
    """Gemini returned 5xx after all retries."""


__all__ = [
    "AgentError",
    "AgentTimeout",
    "AgentUnavailable",
    "ConfigError",
    "ConflictError",
    "GatewayError",
    "GuardrailBlocked",
    "InvalidAgentOutput",
    "LevelError",
    "MemoryError",
    "ModelBlocked",
    "ModelError",
    "ModelUnavailable",
    "NotFound",
    "RateLimitExceeded",
    "ToolNotPermitted",
]
