"""Runtime configuration.

Single Settings object read from env (via pydantic-settings). Imported once
in every module; never mutated at runtime. `get_settings()` is memoized.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["local", "cloud"]


class Settings(BaseSettings):
    """All Level runtime knobs. Every value is env-overridable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    level_env: Env = "local"
    level_service_name: str = "level"
    level_log_level: str = "INFO"

    google_cloud_project: str = ""
    google_cloud_region: str = "us-central1"
    level_firestore_database: str = "(default)"

    # AI backend selection.
    # Hackathon rule: Gemini 3.5 or newer, via either Gemini API (AI Studio)
    # or Vertex AI - both count.
    #
    # If GOOGLE_API_KEY is set, we call the Gemini API (aistudio.google.com)
    # directly. This is the fast path when your GCP project doesn't have 3.5
    # enabled in Vertex Model Garden - AI Studio ships new models first and
    # the same publisher IDs (gemini-3.5-flash / gemini-3.5-pro) work.
    #
    # Otherwise, we use Vertex AI with ADC (google_cloud_project + region).
    # In that case the model must be enabled in Model Garden for the project.
    google_api_key: str = ""
    level_model_pro: str = "gemini-3.5-flash"
    level_model_flash: str = "gemini-3.5-flash"

    # Gemma fallback (Bonus: additional Google model). When set and Gemini
    # returns 429 or the daily cost cap is hit, extraction-only agents
    # (chat_router, activity, priority, reminder) retry against this model
    # via Vertex Model Garden. Set to "" to disable.
    level_model_gemma: str = "gemma-3-4b-it"

    # ADK hot-path toggle. When True, chat.py routes email + book intents
    # through google.adk.LlmAgent so the ADK graph is exercised at request
    # time (not just the ADK adapter surface). Defaults to False for cost.
    level_adk_mode: bool = False

    # Multimodal bonus integrations (rules: +0.2 each Google model).
    # Veo 3 for weekly recap videos, Lyria for Hear-my-day audio ambience.
    # Both are gated on being non-empty AND the caller having Vertex
    # access; missing config degrades silently to text-only.
    level_model_veo: str = "veo-3.0-generate-preview"
    level_model_lyria: str = "lyria-002"
    level_media_enabled: bool = False

    level_cal_days_back: int = Field(default=14, ge=1, le=365)
    level_cal_days_forward: int = Field(default=28, ge=1, le=365)

    level_daily_cost_cap_usd: float = 2.00
    level_user_rate_per_hour: int = 60
    level_user_rate_per_day: int = 500
    # chat_router is exempted from the daily cost cap so users always get
    # a response, even when downstream agents get soft-degraded.
    level_router_cost_cap_multiplier: float = 3.0

    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8080/v1/auth/google/callback"
    level_web_app_url: str = "http://localhost:3000"
    level_public_api_url: str = ""

    level_session_secret: str = "change-me-to-a-long-random-string"

    level_otel_exporter: Literal["console", "cloud"] = "console"
    level_admin_traces_enabled: bool = True

    calendar_tz: str = "America/Los_Angeles"

    ai_mode: Literal["live", "record", "replay"] = "live"

    @property
    def is_local(self) -> bool:
        return self.level_env == "local"

    @property
    def is_cloud(self) -> bool:
        return self.level_env == "cloud"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached Settings singleton. Reset with `get_settings.cache_clear()` in tests."""
    return Settings()
