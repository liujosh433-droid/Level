"""Schemas for the Agent Registry.

Every ADK agent Level runs is registered in Firestore with its version,
prompt hash, model id, and IAM binding. This delivers the "cataloged for
cross-department use" requirement of the Fortified Enterprise Fleet
architecture, applied at the agent level.
"""

from __future__ import annotations

from pydantic import Field

from level_core.schemas.base import TimestampedModel


class AgentVersion(TimestampedModel):
    """A specific version of a registered agent.

    Immutable once written — new prompt or new model = new version.
    """

    name: str = Field(description="Registered agent name, e.g. 'challenger'.")
    version: str = Field(
        description="Semantic version string, e.g. 'v3.1.0'.",
        pattern=r"^v\d+\.\d+\.\d+$",
    )

    prompt_sha: str = Field(
        description="SHA-256 hash of the prompt template used at this version.",
        min_length=64,
        max_length=64,
    )
    model_id: str = Field(description="Underlying Gemini model, e.g. 'gemini-3.5-pro'.")

    owner: str = Field(
        description="Team or engineer responsible for this agent version.",
    )
    service_account: str | None = Field(
        default=None,
        description="Google Cloud service account this agent runs as (cloud mode).",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Names of gateway tools this agent version may invoke.",
    )
    description: str = Field(
        description="One-line description of what this agent does.",
        max_length=300,
    )


class RegisteredAgent(TimestampedModel):
    """A logical agent record — points at the current default version and lists history."""

    name: str
    current_version: str = Field(pattern=r"^v\d+\.\d+\.\d+$")
    versions: list[str] = Field(default_factory=list, description="All versions ever registered.")


__all__ = ["AgentVersion", "RegisteredAgent"]
