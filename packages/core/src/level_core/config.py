"""Single source of truth for runtime configuration.

Values are read from environment variables (typically populated from `.env`
via the process manager, uv, or Cloud Run's built-in env var mechanism).
Configuration is loaded once and cached; downstream code should call
``get_settings()`` rather than reading ``os.environ`` directly so that tests
can override behavior via ``get_settings.cache_clear()``.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Runtime environment.

    ``local`` uses in-memory fakes for Firestore and Vector Search and the
    AI Studio Gemini API for LLM calls. ``cloud`` uses real GCP services.
    """

    LOCAL = "local"
    CLOUD = "cloud"


class OtelExporter(str, Enum):
    CONSOLE = "console"
    GCP = "gcp"
    NONE = "none"


class Settings(BaseSettings):
    """Runtime settings loaded from environment / .env.

    All fields have sensible defaults so that ``Settings()`` succeeds in a
    fresh checkout for local dev with an AI Studio key. Cloud deployments
    inject real values via Cloud Run env vars sourced from Secret Manager.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Environment = Field(default=Environment.LOCAL, alias="LEVEL_ENV")
    service_name: str = Field(default="level", alias="LEVEL_SERVICE_NAME")

    # GCP
    gcp_project: str = Field(default="project-c31bdcdc-f293-47c2-a4c", alias="GOOGLE_CLOUD_PROJECT")
    gcp_region: str = Field(default="us-central1", alias="GOOGLE_CLOUD_REGION")

    # Gemini
    google_api_key: str = Field(default="", alias="GOOGLE_API_KEY")
    reasoning_model: str = Field(default="gemini-3.5-flash", alias="LEVEL_REASONING_MODEL")
    fast_model: str = Field(default="gemini-3.5-flash", alias="LEVEL_FAST_MODEL")
    live_model: str = Field(default="gemini-3.5-flash-live-preview", alias="LEVEL_LIVE_MODEL")
    embedding_model: str = Field(
        default="gemini-embedding-001", alias="LEVEL_EMBEDDING_MODEL"
    )
    embedding_dimensions: int = Field(
        default=768,
        alias="LEVEL_EMBEDDING_DIMENSIONS",
        description="Must match Vertex Vector Search index dimensions.",
    )

    # Memory Bank
    firestore_database: str = Field(default="(default)", alias="LEVEL_FIRESTORE_DATABASE")
    vector_index_id: str = Field(default="", alias="LEVEL_VECTOR_INDEX_ID")
    vector_index_endpoint_id: str = Field(default="", alias="LEVEL_VECTOR_INDEX_ENDPOINT_ID")
    vector_deployed_index_id: str = Field(
        default="level_signals_deployed", alias="LEVEL_VECTOR_DEPLOYED_INDEX_ID"
    )

    # Model Armor
    model_armor_template_inbound: str = Field(
        default="", alias="LEVEL_MODEL_ARMOR_TEMPLATE_INBOUND"
    )
    model_armor_template_outbound: str = Field(
        default="", alias="LEVEL_MODEL_ARMOR_TEMPLATE_OUTBOUND"
    )

    # Observability
    otel_exporter: OtelExporter = Field(default=OtelExporter.CONSOLE, alias="LEVEL_OTEL_EXPORTER")

    # Google OAuth (end-user Calendar / Drive)
    google_oauth_client_id: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_ID")
    google_oauth_client_secret: str = Field(default="", alias="GOOGLE_OAUTH_CLIENT_SECRET")
    google_oauth_redirect_uri: str = Field(
        default="http://localhost:8080/v1/auth/google/callback",
        alias="GOOGLE_OAUTH_REDIRECT_URI",
    )
    web_app_url: str = Field(default="http://localhost:3000", alias="LEVEL_WEB_APP_URL")
    public_api_url: str = Field(
        default="",
        alias="LEVEL_PUBLIC_API_URL",
        description=(
            "Public HTTPS base URL for this API (e.g. https://api.example.com). "
            "Required for Google Calendar push notifications; leave blank locally."
        ),
    )
    session_secret: str = Field(
        default="",
        alias="LEVEL_SESSION_SECRET",
        description="HMAC secret for level_session cookies. Falls back to OAuth secret in local.",
    )

    # Prefer AI Studio API key even when LEVEL_ENV=cloud (handy during setup).
    use_ai_studio: bool = Field(default=True, alias="LEVEL_USE_AI_STUDIO")

    # Vector backend: "firestore" (immediate) or "vertex" (after Index deploy).
    vector_backend: str = Field(default="firestore", alias="LEVEL_VECTOR_BACKEND")

    # Retry / timeout knobs (rarely overridden but exposed for tests)
    llm_timeout_seconds: float = Field(default=30.0, alias="LEVEL_LLM_TIMEOUT")
    llm_max_retries: int = Field(default=2, alias="LEVEL_LLM_MAX_RETRIES")

    @field_validator("gcp_project")
    @classmethod
    def _project_not_empty_in_cloud(cls, value: str, info: object) -> str:  # noqa: ARG003
        """Allow blank project in local mode; require it in cloud mode.

        We can't easily reference other fields at validation time in a
        strict cross-field way, so validation of the local/cloud requirement
        is enforced by :meth:`assert_cloud_ready` below.
        """
        return value

    def assert_cloud_ready(self) -> None:
        """Fail fast if we're in cloud mode without required config.

        Called at process startup for services that intend to talk to GCP so
        misconfiguration is a startup crash, not a mysterious 500 later.
        """
        if self.env is not Environment.CLOUD:
            return
        missing: list[str] = []
        if not self.gcp_project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if self.vector_backend == "vertex" and not self.vector_index_endpoint_id:
            missing.append("LEVEL_VECTOR_INDEX_ENDPOINT_ID")
        if missing:
            raise RuntimeError(
                f"LEVEL_ENV=cloud but required settings are missing: {', '.join(missing)}"
            )

    @property
    def is_local(self) -> bool:
        return self.env is Environment.LOCAL

    @property
    def is_cloud(self) -> bool:
        return self.env is Environment.CLOUD


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so tests can call ``get_settings.cache_clear()`` after mutating
    environment variables to force a re-read.
    """
    return Settings()


# Explicit re-export for callers that prefer ``from level_core.config import Environment``.
__all__ = ["Environment", "OtelExporter", "Settings", "get_settings"]


# Convenience type used across the codebase for functions that only care whether
# they're in local or cloud mode.
Mode = Literal["local", "cloud"]
