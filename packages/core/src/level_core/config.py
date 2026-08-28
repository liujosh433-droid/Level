"""Runtime configuration.

Single Settings object read from env (via pydantic-settings). Imported once
in every module; never mutated at runtime. `get_settings()` is memoized.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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
    # Veo 3.1 for weekly recap videos, Lyria for Hear-my-day audio
    # ambience. Both are gated on being non-empty AND the caller
    # having Vertex access; missing config degrades silently to
    # text-only.
    #
    # Veo model ID pitfalls that have burned this project before:
    #   * Vertex AI uses the ``-001`` suffix
    #     (``veo-3.1-generate-001``, ``veo-3.1-fast-generate-001``).
    #     The Gemini API uses ``-preview``. Because we call the model
    #     via ``genai.Client(vertexai=True, ...)`` in media.py, only
    #     the ``-001`` IDs actually resolve; ``-preview`` returns 404.
    #   * Veo 3.0 preview was retired in April 2026. Defaulting to
    #     ``veo-3.0-generate-preview`` (what shipped originally)
    #     returned "veo_unavailable" on every call.
    #   * Fast is 2x cheaper and equally good for a 15-second
    #     stylistic recap loop, so it's the default. Bump to
    #     ``veo-3.1-generate-001`` via env if you need higher
    #     fidelity or first-and-last-frame control.
    #
    # Lyria 3 model ID has to be one of the Interactions API models
    # (``lyria-3-clip-preview`` = fixed 30-second clip,
    # ``lyria-3-pro-preview`` = longer compositions). The older
    # ``lyria-002`` returned "Model has no attribute 'generate_music'"
    # because the code was calling a method that never existed on the
    # SDK - see routes/media.py::_generate_lyria for the corrected
    # ``interactions.create`` shape.
    level_model_veo: str = "veo-3.1-fast-generate-001"
    level_model_lyria: str = "lyria-3-clip-preview"
    level_media_enabled: bool = False

    level_cal_days_back: int = Field(default=14, ge=1, le=365)
    level_cal_days_forward: int = Field(default=28, ge=1, le=365)
    # Onboarding UX: on first Google connect we pull the calendar
    # synchronously so the homepage renders with populated events
    # instead of "Pulling your calendar...". If the pull exceeds this
    # timeout, we fall back to background and the frontend polls every
    # 1200ms. Enrichment (LLM classification) is always background.
    #
    # v2: dropped from 6.0s -> 3.0s. Typical calendars finish in
    # <1s with syncToken, and calendars slow enough to exceed 3s were
    # already going to feel slow in the foreground - background +
    # OnboardingProgress card handles them gracefully.
    level_oauth_refresh_timeout_s: float = Field(default=3.0, ge=1.0, le=30.0)

    level_daily_cost_cap_usd: float = 2.00
    level_user_rate_per_hour: int = 60
    level_user_rate_per_day: int = 500
    # chat_router is exempted from the daily cost cap so users always get
    # a response, even when downstream agents get soft-degraded.
    level_router_cost_cap_multiplier: float = 3.0

    # HTTP-layer rate limit on /v1/chat (see level_api.rate_limit).
    # Sits ABOVE the LLM gate - protects the fast-path CPU + Firestore
    # reads even when the model isn't invoked. Token bucket: `burst`
    # capacity, refill at `per_min` messages/min. Defaults suit a
    # normal caregiver typing pace.
    level_chat_rate_burst: int = Field(default=20, ge=1, le=1000)
    level_chat_rate_per_min: int = Field(default=30, ge=1, le=6000)

    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Empty means "auto-derive from level_api_port at runtime" - see the
    # model validator below. An explicit value (e.g. the deployed Cloud
    # Run HTTPS URL) always wins.
    google_oauth_redirect_uri: str = ""
    # Local dev port for the FastAPI server. Kept here so the OAuth
    # redirect URI can follow the port when a judge runs the app on a
    # non-default port (e.g. LEVEL_API_PORT=8081 to sidestep Cursor's
    # collision on 8080).
    level_api_port: int = Field(default=8080, ge=1, le=65535)
    level_web_app_url: str = "http://localhost:3000"
    level_public_api_url: str = ""

    level_session_secret: str = "change-me-to-a-long-random-string"

    level_otel_exporter: Literal["console", "cloud"] = "console"
    level_admin_traces_enabled: bool = True

    calendar_tz: str = "America/Los_Angeles"

    ai_mode: Literal["live", "record", "replay"] = "live"

    # Prod demo mode. When True, POST /v1/auth/demo works in cloud too,
    # letting judges try the deployed API with zero setup (no clone,
    # no local env, no OAuth). Defaults to False because a naive
    # implementation would let bots spawn unbounded synthetic users
    # against real Firestore + real Vertex billing.
    #
    # Safety when enabled:
    #   * fixed pool of ``level_demo_slots_per_scenario`` user_ids per
    #     scenario (total = slots * len(SCENARIOS)) - storage stays
    #     bounded regardless of demo traffic.
    #   * client IP is hashed to a slot so the same judge lands on the
    #     same user across clicks (stable session UX) while bots
    #     rotating IPs still can't grow the pool.
    #   * per-IP token bucket on the demo endpoint itself
    #     (``level_demo_per_ip_per_hour``) rejects burst abuse.
    #   * per-user daily cost cap (``level_daily_cost_cap_usd``) already
    #     caps LLM spend at ``$cap * pool_size`` in the pathological case.
    level_demo_in_cloud: bool = False
    level_demo_slots_per_scenario: int = Field(default=3, ge=1, le=50)
    level_demo_per_ip_per_hour: int = Field(default=10, ge=1, le=1000)

    # "Real send" mode for demo email. When all three are set, the
    # ``/v1/email/send`` demo short-circuit switches from a preview
    # response to an actual Gmail send using the operator's own OAuth
    # refresh token, with the recipient rewritten to a safe intercept
    # address (usually the operator's own inbox) so the demo produces
    # visible email proof without ever mailing the fake demo contacts.
    #
    # Only fires when ``is_demo_user(profile)`` is True AND all three
    # values are populated - a defensive fence so a stray env var
    # can't accidentally cause a real user's mail to be rerouted.
    # See ``routes/email.py::send`` for the branch.
    #
    # Setup (one-time, on the operator's own laptop):
    #   1. Run the normal OAuth flow against your own Gmail account.
    #   2. Copy the resulting refresh_token from `.level/tokens.json`.
    #   3. Set ``LEVEL_DEMO_GMAIL_REFRESH_TOKEN`` to it.
    #   4. Set ``LEVEL_DEMO_EMAIL_INTERCEPT_TO`` to your own email.
    #   5. Set ``LEVEL_DEMO_SEND_REAL_EMAILS=true``.
    level_demo_send_real_emails: bool = False
    level_demo_email_intercept_to: str = ""
    level_demo_gmail_refresh_token: str = ""

    @property
    def is_local(self) -> bool:
        return self.level_env == "local"

    @property
    def is_cloud(self) -> bool:
        return self.level_env == "cloud"

    @model_validator(mode="after")
    def _default_oauth_redirect_uri(self) -> Settings:
        """Auto-derive the OAuth redirect URI from LEVEL_API_PORT when empty.

        Deployed environments set this explicitly to the HTTPS Cloud Run
        URL. Local dev leaves it empty, and we compute
        ``http://localhost:{level_api_port}/v1/auth/google/callback`` so
        the redirect follows the API port automatically when a judge
        overrides LEVEL_API_PORT to sidestep a port collision (Cursor
        grabs 8080 by default). The Google Cloud Console still needs
        that URI pre-registered - we tell judges to add a small set of
        common local ports at once (see SETUP.md) so this is a one-time
        setup, not a per-port chore.
        """
        if not self.google_oauth_redirect_uri:
            self.google_oauth_redirect_uri = (
                f"http://localhost:{self.level_api_port}/v1/auth/google/callback"
            )
        return self


_INSECURE_SESSION_SECRET = "change-me-to-a-long-random-string"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached Settings singleton. Reset with `get_settings.cache_clear()` in tests.

    In cloud mode we refuse to boot with the default `LEVEL_SESSION_SECRET`.
    That secret signs both session cookies (via `URLSafeSerializer`) and
    the HMAC agent-identity token audit rows use for tamper detection;
    leaving the default in a deployed environment would let anyone forge
    both. Local dev keeps the default for zero-config startup.
    """
    settings = Settings()
    if settings.is_cloud and settings.level_session_secret == _INSECURE_SESSION_SECRET:
        raise RuntimeError(
            "LEVEL_SESSION_SECRET is still the insecure default. "
            "Set it to a long random string before deploying to cloud."
        )
    return settings
