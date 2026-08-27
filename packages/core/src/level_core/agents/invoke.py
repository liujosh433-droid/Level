"""Gemini invocation layer: SDK calls, retries, tier fallback ladder.

`base.py` is about the SHAPE of an agent call (schema, source_span
echo, guardrails, audit). This module is about actually reaching a
model: the retry loop, the AI-Studio-then-Vertex-then-Gemma fallback
ladder, safety/quota exception types, and the cached genai client.

The split matters because these two layers change for different
reasons: base.py changes when we invent a new guardrail; invoke.py
changes when a Google backend gets rate-limited differently, or a
new model tier lands.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from level_core.config import get_settings
from level_core.observability import get_logger

if TYPE_CHECKING:
    from level_core.agents.base import AgentSpec

logger = get_logger(__name__)


@dataclass
class RawResponse:
    """Raw model output before the guardrails in `base.py` process it."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    loop_broken: bool = False
    # Records which alternate model actually served this response
    # ("gemini-2.5-flash" for a Vertex fallback, "gemma-3-4b-it" for
    # a Gemma fallback). None when the primary model answered.
    fallback_used: str | None = None


class SafetyBlocked(Exception):
    """Google safety filter tripped. Not a quota issue."""


class QuotaExhausted(Exception):
    """The Gemini backend told us to slow down (429). We bubble this up so
    the chat handler can produce a specific, actionable reply rather than
    silently retrying and burning more quota.
    """

    def __init__(self, retry_after_s: int | None, message: str) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(message)


class LLMUnavailable(Exception):
    """No LLM backend is reachable in this process.

    Raised when neither ``GOOGLE_API_KEY`` (AI Studio) nor
    ``GOOGLE_CLOUD_PROJECT`` (Vertex ADC) is configured. ``call_agent``
    catches this and returns a soft-degraded result so downstream code
    can use its deterministic fallback (e.g. the daily-summary route
    synthesizes a summary from the raw event list) instead of 500ing.

    Deliberately separate from ``QuotaExhausted`` and ``SafetyBlocked``
    because those have retry semantics; this one is a hard configuration
    fact that won't change until the process restarts with fresh env.
    """



def _parse_retry_after(err: Exception) -> int | None:
    """Best-effort: pull the 'retry in Ns' hint out of a Gemini 429 error."""
    text = str(err)
    m = re.search(r"retry in ([\d.]+)s", text, re.IGNORECASE)
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return None
    return None


def _is_quota_error(err: Exception) -> bool:
    code = getattr(err, "code", None) or getattr(err, "status_code", None)
    try:
        if code is not None and int(code) == 429:
            return True
    except (TypeError, ValueError):
        pass
    text = str(err)
    return "RESOURCE_EXHAUSTED" in text or "429" in text[:80]


def _vertex_fallback_model(model_id: str) -> str:
    """Vertex Model Garden on this project has 2.5, not 3.5. Map accordingly."""
    if "pro" in model_id and "flash" not in model_id:
        return "gemini-2.5-pro"
    return "gemini-2.5-flash"


# Agents whose output schema is simple enough to be safely handled by a
# smaller open-weight model as a last-resort fallback. Excludes anything
# that must speak in a caregiver-appropriate tone (Email, Summary).
GEMMA_ELIGIBLE: frozenset[str] = frozenset(
    {
        "ChatRouterAgent",
        "ActivityAgent",
        "PriorityAgent",
        "ReminderAgent",
        "UsualAgent",
    }
)


async def invoke_with_retry(
    *, model_id: str, spec: "AgentSpec", contents: list[dict[str, Any]]
) -> RawResponse:
    """Call Gemini with retries + tiered fallback.

    Tier 1: primary Gemini (AI Studio or Vertex, per config).
    Tier 2: Vertex Gemini 2.5 when AI Studio 3.5 is rate-limited.
    Tier 3: Gemma via Vertex Model Garden for extraction-only agents
            (Hackathon bonus: additional Google model integrated).

    We do NOT swallow non-transient errors. Anything not in {429, 5xx}
    surfaces immediately so tests fail loudly on regressions.
    """
    from level_core.agents.fakes import fake_call, is_faked

    if is_faked(spec.name):
        return fake_call(spec.name, contents)

    settings = get_settings()
    delays = [0.5, 1.5, 4.0]
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return await _invoke_vertex(
                model_id=model_id, spec=spec, contents=contents
            )
        except SafetyBlocked:
            raise
        except LLMUnavailable:
            # No backend configured - retrying and Gemma fallback both
            # require reaching a Google endpoint, so bubble immediately.
            raise
        except Exception as e:
            last_exc = e
            if _is_quota_error(e):
                # Tier 2: AI Studio 429 -> Vertex 2.5.
                if settings.google_api_key and settings.google_cloud_project:
                    fallback = _vertex_fallback_model(model_id)
                    logger.warning(
                        "agent.aistudio_quota_fallback_vertex",
                        requested=model_id,
                        fallback=fallback,
                        agent=spec.name,
                    )
                    try:
                        resp = await _invoke_vertex(
                            model_id=fallback,
                            spec=spec,
                            contents=contents,
                            force_vertex=True,
                        )
                        resp.fallback_used = fallback
                        return resp
                    except Exception as fallback_err:
                        last_exc = fallback_err
                        # Tier 3: even Vertex 2.5 429 -> try Gemma for
                        # extraction agents. Never for generative agents
                        # (their tone budget is tighter than Gemma can do).
                        if _is_quota_error(fallback_err):
                            gemma = await _try_gemma(spec, contents)
                            if gemma is not None:
                                return gemma
                            raise QuotaExhausted(
                                _parse_retry_after(fallback_err),
                                str(fallback_err),
                            ) from fallback_err
                        raise
                # Single-backend deployment: Vertex quota. Try Gemma once.
                gemma = await _try_gemma(spec, contents)
                if gemma is not None:
                    return gemma
                raise QuotaExhausted(_parse_retry_after(e), str(e)) from e
            code = getattr(e, "code", None) or getattr(e, "status_code", None)
            if code and int(code) not in (500, 502, 503, 504):
                raise
            if attempt < 2:
                await asyncio.sleep(delays[attempt])
    assert last_exc is not None
    raise last_exc


async def _try_gemma(
    spec: "AgentSpec", contents: list[dict[str, Any]]
) -> RawResponse | None:
    """Attempt a Gemma call on Vertex Model Garden for eligible agents.

    Returns None (caller re-raises quota) when:
      - agent isn't extraction-only,
      - LEVEL_MODEL_GEMMA is empty,
      - GCP project isn't configured,
      - Gemma itself throws.

    Success: returns a RawResponse with `fallback_used` set to the
    Gemma model id so /admin/traces shows the bonus in action.
    """
    settings = get_settings()
    if spec.name not in GEMMA_ELIGIBLE:
        return None
    if not settings.level_model_gemma or not settings.google_cloud_project:
        return None
    try:
        resp = await _invoke_vertex(
            model_id=settings.level_model_gemma,
            spec=spec,
            contents=contents,
            force_vertex=True,
        )
        resp.fallback_used = settings.level_model_gemma
        logger.warning(
            "agent.gemma_fallback",
            agent=spec.name,
            model=settings.level_model_gemma,
        )
        return resp
    except Exception as err:  # noqa: BLE001 - fallback must never take down the primary path
        logger.warning(
            "agent.gemma_fallback_failed",
            agent=spec.name,
            err=str(err)[:200],
        )
        return None


_client_cache: dict[tuple[str, ...], Any] = {}


def _get_gemini_client(*, force_vertex: bool = False) -> Any:
    """Return a process-wide cached `genai.Client`.

    Rebuilding the client per request cost us ~200ms of TCP + auth handshake
    on every LLM call. The client is thread-safe and holds a connection pool
    to reuse across requests. Keyed by backend so both AI Studio and Vertex
    can coexist in the same process (e.g. tests).
    """
    from google import genai
    from google.genai import types

    settings = get_settings()
    # Never let the SDK retry 429s itself - Google's Retry-After on the
    # free tier is ~60s, which looks like a hang in the chat UI.
    http_options = types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=1)
    )
    use_studio = bool(settings.google_api_key) and not force_vertex
    # Detect the "no LLM backend at all" case up front so the SDK
    # doesn't throw a cryptic DefaultCredentialsError / AttributeError
    # midway through client init - see LLMUnavailable docstring.
    if not use_studio and not settings.google_cloud_project:
        raise LLMUnavailable(
            "No LLM backend configured: set GOOGLE_API_KEY (AI Studio) "
            "or GOOGLE_CLOUD_PROJECT (Vertex) to enable agent calls. "
            "Demo mode + deterministic fallbacks continue to work."
        )
    if use_studio:
        key = ("aistudio", settings.google_api_key)
    else:
        key = (
            "vertex",
            settings.google_cloud_project,
            settings.google_cloud_region,
        )
    client = _client_cache.get(key)
    if client is None:
        if use_studio:
            client = genai.Client(
                api_key=settings.google_api_key, http_options=http_options
            )
        else:
            client = genai.Client(
                vertexai=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_region,
                http_options=http_options,
            )
        _client_cache[key] = client
    return client


async def _invoke_vertex(
    *,
    model_id: str,
    spec: "AgentSpec",
    contents: list[dict[str, Any]],
    force_vertex: bool = False,
) -> RawResponse:
    """Real Gemini call via one of two backends.

    - If GOOGLE_API_KEY is set, use the Gemini API (AI Studio). This is the
      hackathon escape hatch when your GCP project doesn't yet have 3.5
      enabled in Vertex Model Garden; AI Studio ships new models first.
    - Otherwise, use Vertex AI with ADC.

    Isolated so tests can monkeypatch.
    """
    from google.genai import types

    client = _get_gemini_client(force_vertex=force_vertex)

    schema_json = spec.response_schema.model_json_schema()
    generation_config = types.GenerateContentConfig(
        temperature=spec.temperature,
        response_mime_type="application/json",
        response_schema=schema_json,
        safety_settings=[
            types.SafetySetting(
                category=cat,
                threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            )
            for cat in (
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            )
        ],
    )

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model_id,
        contents=contents,
        config=generation_config,
    )

    if getattr(response, "prompt_feedback", None) and getattr(
        response.prompt_feedback, "block_reason", None
    ):
        raise SafetyBlocked()

    usage = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage, "prompt_token_count", 0) if usage else 0
    output_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0

    return RawResponse(
        text=response.text or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
