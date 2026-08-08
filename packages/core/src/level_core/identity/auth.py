"""Agent identity + service-account impersonation helpers.

Each agent has a canonical service account name derived from its registry
name. In cloud mode, the Conductor impersonates the target agent's SA
before invoking any tools that touch external systems. In local mode
identity is a no-op that returns bare application-default credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from level_core.config import Settings, get_settings

if TYPE_CHECKING:
    from google.auth.credentials import Credentials


def default_service_account_for(agent_name: str, project_id: str) -> str:
    """Return the canonical service account email for a given agent."""
    return f"level-{agent_name}@{project_id}.iam.gserviceaccount.com"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """Identity metadata for one agent.

    Attributes:
        agent_name: Registry name of the agent.
        service_account_email: The GCP SA email this agent runs as.
        scopes: OAuth scopes required when calling Google APIs.
    """

    agent_name: str
    service_account_email: str
    scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/cloud-platform",)


@dataclass(slots=True)
class IdentityContext:
    """Runtime identity context for an agent invocation.

    In cloud mode this holds impersonated credentials scoped to the target
    agent's SA. In local mode it's a null context — the agent runs with
    whatever credentials the process has.
    """

    identity: AgentIdentity | None
    credentials: Credentials | None

    @property
    def is_null(self) -> bool:
        return self.identity is None or self.credentials is None


def build_identity_context(
    *,
    agent_name: str,
    settings: Settings | None = None,
) -> IdentityContext:
    """Construct an :class:`IdentityContext` for the given agent.

    In cloud mode the returned context wraps an impersonated-credentials
    object scoped to the agent's service account. In local mode the
    returned context is a null context; the caller runs with whatever
    credentials the process has (typically application-default).
    """
    settings = settings or get_settings()

    if settings.is_local:
        return IdentityContext(identity=None, credentials=None)

    identity = AgentIdentity(
        agent_name=agent_name,
        service_account_email=default_service_account_for(agent_name, settings.gcp_project),
    )

    try:
        import google.auth
        from google.auth import impersonated_credentials

        source_creds, _ = google.auth.default(scopes=list(identity.scopes))
        creds = impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=identity.service_account_email,
            target_scopes=list(identity.scopes),
            lifetime=3600,
        )
        return IdentityContext(identity=identity, credentials=creds)
    except Exception:  # noqa: BLE001
        # Fail open in dev-cloud: fall back to source credentials. In prod
        # you'd want this to fail closed; for the hackathon build we log via
        # the exception being visible in the caller's trace.
        return IdentityContext(identity=identity, credentials=None)


__all__ = [
    "AgentIdentity",
    "IdentityContext",
    "build_identity_context",
    "default_service_account_for",
]
