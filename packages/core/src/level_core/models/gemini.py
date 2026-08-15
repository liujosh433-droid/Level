"""Real Gemini client implementations.

We use the ``google-genai`` SDK, which supports both AI Studio (API key)
and Vertex AI (service account) with the same call surface — the client
constructor is what differs.

Timeouts and retries are handled here so callers don't have to think about
them. Retries use tenacity with exponential backoff + jitter, capped by
``Settings.llm_max_retries``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from level_core.config import Settings, get_settings
from level_core.errors import ModelBlocked, ModelUnavailable
from level_core.models.base import GenerationRequest, GenerationResponse
from level_core.observability.logger import get_logger
from level_core.observability.tracer import traced

if TYPE_CHECKING:
    from google.genai import Client as _GenAIClient  # type: ignore[import-not-found]

_logger = get_logger(__name__)

# Keys Gemini's response_schema rejects (Pydantic's model_json_schema emits them).
_SCHEMA_KEYS_TO_STRIP = frozenset(
    {
        "additionalProperties",
        "additional_properties",
        "$schema",
        "title",
        "default",
    }
)


def sanitize_schema_for_gemini(schema: Any) -> Any:
    """Make a Pydantic JSON schema acceptable to Gemini structured output.

    Pydantic v2 emits ``additionalProperties`` and ``$defs``/``$ref``;
    Gemini returns 400 if those are present. We inline ``$ref`` targets
    then strip the unsupported keys.
    """
    if not isinstance(schema, dict):
        if isinstance(schema, list):
            return [sanitize_schema_for_gemini(item) for item in schema]
        return schema

    defs = schema.get("$defs") or schema.get("definitions") or {}
    inlined = _inline_refs(schema, defs)
    return _strip_unsupported(inlined)


def _inline_refs(node: Any, defs: dict[str, Any], *, _seen: frozenset[str] | None = None) -> Any:
    seen = _seen or frozenset()
    if isinstance(node, list):
        return [_inline_refs(item, defs, _seen=seen) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return {"type": "object"}
        target = defs.get(name)
        if target is None:
            return {"type": "object"}
        return _inline_refs(target, defs, _seen=seen | {name})
    return {k: _inline_refs(v, defs, _seen=seen) for k, v in node.items() if k not in {"$defs", "definitions"}}


def _strip_unsupported(node: Any) -> Any:
    if isinstance(node, list):
        return [_strip_unsupported(item) for item in node]
    if not isinstance(node, dict):
        return node
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key in _SCHEMA_KEYS_TO_STRIP or key in {"$defs", "definitions", "$ref"}:
            continue
        cleaned[key] = _strip_unsupported(value)
    return cleaned


def _make_genai_client(settings: Settings) -> _GenAIClient:
    """Construct a google-genai client for the current runtime.

    Local mode uses the AI Studio API key (``GOOGLE_API_KEY``). Cloud mode
    uses Vertex AI with the process's application-default credentials.
    """
    from google.genai import Client

    # AI Studio free tier is tiny (e.g. 20 req/day on some models). Prefer
    # Vertex when LEVEL_USE_AI_STUDIO=false so we can use GCP billing credits.
    if settings.use_ai_studio and settings.google_api_key:
        return Client(api_key=settings.google_api_key)

    if settings.gcp_project:
        return Client(
            vertexai=True,
            project=settings.gcp_project,
            location=settings.gcp_region,
        )

    raise ModelUnavailable(
        "No Gemini credentials. Set GOOGLE_API_KEY (AI Studio) or "
        "GOOGLE_CLOUD_PROJECT + ADC for Vertex (LEVEL_USE_AI_STUDIO=false)."
    )


class GeminiGenAIClient:
    """Concrete GeminiClient backed by the google-genai SDK."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: _GenAIClient | None = None

    def _get(self) -> _GenAIClient:
        if self._client is None:
            self._client = _make_genai_client(self._settings)
        return self._client

    @traced("gemini.generate")
    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        client = self._get()

        # Gemini 3.5 thinking models spend output budget on thoughts. For
        # structured JSON we keep thinking minimal and ensure enough room
        # for the actual payload.
        max_tokens = request.max_output_tokens
        if request.response_schema is not None:
            max_tokens = max(max_tokens, 4096)

        config: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": max_tokens,
        }
        if request.system_instruction:
            config["system_instruction"] = request.system_instruction
        if request.response_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = sanitize_schema_for_gemini(request.response_schema)
            # thinking_level is only valid on Gemini 3.x thinking models —
            # gemini-2.5-* on Vertex returns 400 if we send it.
            if "gemini-3" in (request.model_id or ""):
                try:
                    from google.genai import types as genai_types

                    config["thinking_config"] = genai_types.ThinkingConfig(
                        thinking_level=genai_types.ThinkingLevel.MINIMAL,
                    )
                except Exception:  # noqa: BLE001
                    pass

        async def _call() -> Any:
            # google-genai exposes an async client on `.aio`.
            contents: Any = request.prompt
            if request.media:
                from google.genai import types as genai_types

                parts: list[Any] = [genai_types.Part.from_text(text=request.prompt)]
                for item in request.media:
                    parts.append(
                        genai_types.Part.from_bytes(
                            data=item.data, mime_type=item.mime_type
                        )
                    )
                contents = parts
            return await client.aio.models.generate_content(
                model=request.model_id,
                contents=contents,
                config=config,
            )

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._settings.llm_max_retries + 1),
            wait=wait_exponential_jitter(initial=0.5, max=8.0),
            retry=retry_if_exception_type(ModelUnavailable),
            reraise=True,
        ):
            with attempt:
                try:
                    response = await _call()
                except Exception as exc:  # noqa: BLE001
                    raise ModelUnavailable(str(exc)) from exc

                text = getattr(response, "text", None) or ""
                usage = getattr(response, "usage_metadata", None)
                finish = "stop"
                if hasattr(response, "candidates") and response.candidates:
                    finish_reason = getattr(response.candidates[0], "finish_reason", "STOP")
                    finish = str(finish_reason).lower()

                if finish in {"safety", "prohibited_content", "blocklist"}:
                    raise ModelBlocked(f"Gemini refused to answer (finish_reason={finish})")

                if request.response_schema is not None and text:
                    # Fail fast on unparseable JSON so the caller can retry
                    # with a stricter prompt / schema.
                    try:
                        json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise ModelUnavailable(
                            f"Gemini returned unparseable JSON: {exc.msg}"
                        ) from exc

                return GenerationResponse(
                    text=text,
                    input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                    model_id=request.model_id,
                    finish_reason=finish,
                )

        # Unreachable — AsyncRetrying either returns via `with attempt` or reraises.
        raise ModelUnavailable("exhausted retries without response")


class GeminiEmbeddingClient:
    """Embedding client backed by the google-genai SDK."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: _GenAIClient | None = None

    def _get(self) -> _GenAIClient:
        if self._client is None:
            self._client = _make_genai_client(self._settings)
        return self._client

    @traced("gemini.embed")
    async def embed(self, *, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._get()
        try:
            from google.genai import types as genai_types

            response = await client.aio.models.embed_content(
                model=self._settings.embedding_model,
                contents=texts,
                config=genai_types.EmbedContentConfig(
                    output_dimensionality=self._settings.embedding_dimensions,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise ModelUnavailable(str(exc)) from exc

        embeddings = getattr(response, "embeddings", []) or []
        return [list(getattr(e, "values", [])) for e in embeddings]


__all__ = ["GeminiEmbeddingClient", "GeminiGenAIClient", "sanitize_schema_for_gemini"]
