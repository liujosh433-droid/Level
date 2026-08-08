"""Agent Identity — per-agent service accounts and impersonation helpers.

Each ADK agent runs under its own Google Cloud service account with
narrowly scoped IAM permissions. The Conductor (running as a "root" SA)
impersonates each worker SA when invoking that agent's tools, giving us
zero-trust between agents.

In local mode there's no real IAM — this module returns a null
:class:`IdentityContext` and every call is a no-op.
"""

from level_core.identity.auth import (
    AgentIdentity,
    IdentityContext,
    build_identity_context,
    default_service_account_for,
)

__all__ = [
    "AgentIdentity",
    "IdentityContext",
    "build_identity_context",
    "default_service_account_for",
]
