"""Multimodal media: Veo (one-shot Info-page film) + Lyria (audio chime).

Hackathon bonus: rules give +0.2 for each additional Google AI model
integrated (Gemma, Veo, Lyria) up to +0.6. Level integrates Veo for a
single brand film on /about (Info) and Lyria for a start/end chime on
"Hear my day".

Veo is generated once, for everyone. The prompt is a fixed Level
trailer - no calendar, no names, no per-user or per-week variation.
The mp4 lives at a stable GCS object; every later request is a URL
lookup. That is the whole cost model: one ~$1.20 clip, then reuse.

Both endpoints degrade gracefully when the caller isn't configured for
Vertex AI or when ``LEVEL_MEDIA_ENABLED=false``. They return
``{ready: false, reason: "..."}`` and the frontend keeps the static
photo so the demo never breaks.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.storage.base import UserStore

from level_api.deps import get_user_store

router = APIRouter()
logger = get_logger(__name__)

MEDIA_CACHE_KEY = "media_cache"
# v2: new prompt (caregiver + kids at a calendar). A new blob name
# is what actually forces a fresh Veo call; the old
# about/level-intro.mp4 object would otherwise be reused forever.
INTRO_BLOB_NAME = "about/level-intro-v2.mp4"

# Veo 3.x on Vertex only accepts 4/6/8-second clips. 8s is the SDK
# default; we don't ask for 15s because the model would ignore it.
INTRO_PROMPT = (
    "An 8-second cinematic documentary shot of a caregiver at home with "
    "two kids, looking together at a large paper calendar on the fridge. "
    "Warm morning kitchen light. The parent studies the week's dates; "
    "the children stand beside them. Unhurried, hopeful, family-friendly. "
    "No logos. No readable text on phones or screens."
)

IN_FLIGHT_MAX_AGE_SECONDS = 900
VEO_POLL_CEILING_SECONDS = 600
# If the one-shot generation fails, wait before trying again so a
# misconfigured project doesn't burn a fresh $1.20 every 6s poll.
ERROR_COOLDOWN_SECONDS = 300

# Process-local handles for the in-flight generation. Durable cache is
# GCS (or LEVEL_VEO_INTRO_URL). These only exist so two polls on the
# same Cloud Run instance don't spawn two Veo calls.
_intro_cached_url: str | None = None
_intro_cached_poster: str | None = None
_intro_started_at: str | None = None
_intro_error_at: datetime | None = None
_intro_error_reason: str | None = None


class IntroResponse(BaseModel):
    ready: bool
    reason: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    model: str | None = None
    cached: bool = False
    generating: bool = False
    started_at: str | None = None


class ChimeResponse(BaseModel):
    ready: bool
    reason: str | None = None
    audio_url: str | None = None
    mood: str | None = None
    model: str | None = None
    cached: bool = False


def reset_intro_runtime_state() -> None:
    """Test helper: wipe process-local intro state between cases."""
    global _intro_cached_url, _intro_cached_poster
    global _intro_started_at, _intro_error_at, _intro_error_reason
    _intro_cached_url = None
    _intro_cached_poster = None
    _intro_started_at = None
    _intro_error_at = None
    _intro_error_reason = None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


async def _read_cache(store: UserStore) -> dict[str, Any]:
    raw = await store.profile.read() or {}
    cache = raw.get(MEDIA_CACHE_KEY)
    if isinstance(cache, dict):
        return cache
    return {}


async def _write_cache(store: UserStore, cache: dict[str, Any]) -> None:
    profile = dict(await store.profile.read() or {})
    profile[MEDIA_CACHE_KEY] = cache
    await store.profile.write(profile)


def _lookup_intro_url() -> str | None:
    """Return the durable intro URL if it already exists. No Veo call.

    Order: explicit env pin, then process cache, then the stable GCS
    object. Any of those means we never touch Veo again.
    """
    global _intro_cached_url
    settings = get_settings()
    pinned = (settings.level_veo_intro_url or "").strip()
    if pinned:
        _intro_cached_url = pinned
        return pinned
    if _intro_cached_url:
        return _intro_cached_url
    bucket_name = _media_bucket_name()
    if not bucket_name:
        return None
    try:
        from google.cloud import storage
    except ImportError:  # pragma: no cover
        return None
    try:
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(INTRO_BLOB_NAME)
        if blob.exists():
            url = _gcs_https_url(f"gs://{bucket_name}/{INTRO_BLOB_NAME}")
            _intro_cached_url = url
            return url
    except Exception as err:  # noqa: BLE001
        logger.warning("media.veo.intro_lookup_failed", err=str(err)[:200])
    return None


def _promote_intro(video_url: str) -> str | None:
    """Copy a just-generated mp4 onto the stable GCS object.

    Veo often lands bytes at a random UUID path (or a data: URL). The
    intro must live at INTRO_BLOB_NAME so a Cloud Run restart doesn't
    spend another $1.20 discovering it.
    """
    if not video_url:
        return None
    bucket_name = _media_bucket_name()
    public = _gcs_https_url(f"gs://{bucket_name}/{INTRO_BLOB_NAME}") if bucket_name else ""
    if public and video_url.rstrip("/") == public.rstrip("/"):
        return video_url
    if not bucket_name:
        return video_url
    try:
        from google.cloud import storage
    except ImportError:  # pragma: no cover
        return video_url
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        prefix = f"https://storage.googleapis.com/{bucket_name}/"
        if video_url.startswith(prefix):
            src_name = video_url[len(prefix) :]
            if src_name != INTRO_BLOB_NAME:
                bucket.copy_blob(bucket.blob(src_name), bucket, INTRO_BLOB_NAME)
            return public
        if video_url.startswith("data:video/mp4;base64,"):
            raw = base64.b64decode(video_url.split(",", 1)[1])
            bucket.blob(INTRO_BLOB_NAME).upload_from_string(raw, content_type="video/mp4")
            return public
        return video_url
    except Exception as err:  # noqa: BLE001
        logger.warning("media.veo.intro_promote_failed", err=str(err)[:200])
        return video_url


@router.get("/intro", response_model=IntroResponse)
async def about_intro() -> IntroResponse:
    """Return the one-shot Veo Info-page film.

    No auth: /about is public. No per-user state. No regenerate
    button. First visitor kicks off generation in the background;
    everyone after that gets the cached GCS URL.
    """
    settings = get_settings()
    if not settings.level_media_enabled:
        return IntroResponse(ready=False, reason="media_disabled")

    existing = await asyncio.to_thread(_lookup_intro_url)
    if existing:
        return IntroResponse(
            ready=True,
            video_url=existing,
            poster_url=_intro_cached_poster,
            model=settings.level_model_veo,
            cached=True,
        )

    global _intro_error_at, _intro_error_reason, _intro_started_at
    if _intro_error_at is not None:
        age = (datetime.now(timezone.utc) - _intro_error_at).total_seconds()
        if age < ERROR_COOLDOWN_SECONDS:
            return IntroResponse(
                ready=False,
                reason=_intro_error_reason or "veo_unavailable",
            )
        _intro_error_at = None
        _intro_error_reason = None

    started = _parse_iso_datetime(_intro_started_at)
    if started is not None:
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age < IN_FLIGHT_MAX_AGE_SECONDS:
            return IntroResponse(
                ready=False,
                reason="generating",
                generating=True,
                started_at=_intro_started_at,
            )

    started_at_iso = datetime.now(timezone.utc).isoformat()
    _intro_started_at = started_at_iso
    asyncio.create_task(
        _run_intro_in_background(model=settings.level_model_veo)
    )
    logger.info("media.veo.intro_scheduled", model=settings.level_model_veo)
    return IntroResponse(
        ready=False,
        reason="generating",
        generating=True,
        started_at=started_at_iso,
        model=settings.level_model_veo,
    )


async def _run_intro_in_background(*, model: str) -> None:
    """One Veo call. On success, pin the URL in GCS + process cache."""
    global _intro_cached_url, _intro_cached_poster
    global _intro_started_at, _intro_error_at, _intro_error_reason
    try:
        result = await _generate_veo(prompt=INTRO_PROMPT, model=model)
        exc_reason: str | None = None
    except Exception as err:  # noqa: BLE001
        logger.warning("media.veo.intro_exception", err=str(err)[:200])
        result = None
        exc_reason = "veo_unavailable"

    video_url = await _ensure_playable_url((result or {}).get("video_url") or "")
    if video_url and not video_url.startswith("data:"):
        stable = await asyncio.to_thread(_promote_intro, video_url)
        if stable and not stable.startswith("data:"):
            _intro_cached_url = stable
            _intro_cached_poster = (result or {}).get("poster_url") or None
            _intro_error_at = None
            _intro_error_reason = None
            _intro_started_at = None
            logger.info("media.veo.intro_done", model=model, url=stable[:120])
            return

    _intro_started_at = None
    _intro_error_at = datetime.now(timezone.utc)
    _intro_error_reason = (result or {}).get("reason") or exc_reason or "veo_unavailable"
    logger.info("media.veo.intro_failed", model=model, reason=_intro_error_reason)


@router.get("/chime", response_model=ChimeResponse)
async def daily_chime(
    mood: str = Query(default="calm", pattern=r"^(calm|hopeful|energetic)$"),
    store: UserStore = Depends(get_user_store),
) -> ChimeResponse:
    """Return a Lyria-generated 3-second ambience for 'Hear my day'.

    Prompt is fixed per mood — no PII, no per-user variation — so we
    cache a single mp3 per mood at the app level.
    """
    settings = get_settings()
    if not settings.level_media_enabled:
        return ChimeResponse(ready=False, reason="media_disabled", mood=mood)

    cache = await _read_cache(store)
    cached = (cache.get("chime") or {}).get(mood)
    if cached and cached.get("audio_url"):
        return ChimeResponse(
            ready=True,
            audio_url=cached.get("audio_url"),
            mood=mood,
            model=cached.get("model"),
            cached=True,
        )

    result = await _generate_lyria(mood=mood, model=settings.level_model_lyria)
    if not result:
        return ChimeResponse(ready=False, reason="lyria_unavailable", mood=mood)

    chime_cache = cache.get("chime") or {}
    chime_cache[mood] = {
        "audio_url": result.get("audio_url"),
        "model": settings.level_model_lyria,
        "generated_at": datetime.utcnow().isoformat(),
    }
    cache["chime"] = chime_cache
    await _write_cache(store, cache)
    logger.info(
        "media.lyria.generated",
        user=store.user_id,
        mood=mood,
        model=settings.level_model_lyria,
    )
    return ChimeResponse(
        ready=True,
        audio_url=result.get("audio_url"),
        mood=mood,
        model=settings.level_model_lyria,
        cached=False,
    )


# ---------------------------------------------------------------------------
# Vertex bridges. Isolated behind sync helpers so tests can monkeypatch.
# We keep both bridges "best-effort" — never let a media outage break chat.
# ---------------------------------------------------------------------------


def _media_bucket_name() -> str:
    settings = get_settings()
    explicit = (settings.level_media_gcs_bucket or "").strip()
    if explicit:
        return explicit
    if settings.google_cloud_project:
        return f"level-media-{settings.google_cloud_project}"
    return ""


def _gcs_https_url(uri: str) -> str:
    if uri.startswith("gs://"):
        return "https://storage.googleapis.com/" + uri[len("gs://") :]
    return uri


def _upload_mp4_bytes(raw: bytes, blob_name: str | None = None) -> str | None:
    """Put Veo bytes in GCS and return a public HTTPS URL."""
    bucket_name = _media_bucket_name()
    if not bucket_name or not raw:
        return None
    try:
        from google.cloud import storage
    except ImportError:  # pragma: no cover
        logger.warning("media.veo.no_gcs_sdk")
        return None
    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        name = blob_name or f"recaps/{uuid.uuid4().hex}.mp4"
        blob = bucket.blob(name)
        blob.upload_from_string(raw, content_type="video/mp4")
        return _gcs_https_url(f"gs://{bucket_name}/{blob.name}")
    except Exception as err:  # noqa: BLE001
        logger.warning("media.veo.gcs_upload_failed", err=str(err)[:200])
        return None


async def _ensure_playable_url(video_url: str) -> str:
    """Turn a Veo gs:// URI or data: URL into a browser-playable HTTPS URL."""
    if not video_url:
        return ""
    if video_url.startswith("gs://"):
        return _gcs_https_url(video_url)
    if video_url.startswith("https://") or video_url.startswith("http://"):
        return video_url
    if video_url.startswith("data:video/mp4;base64,"):
        b64 = video_url.split(",", 1)[1]
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            return ""
        uploaded = await asyncio.to_thread(
            _upload_mp4_bytes, raw, INTRO_BLOB_NAME
        )
        return uploaded or ""
    return video_url


async def _generate_veo(*, prompt: str, model: str) -> dict[str, str] | None:
    """Call Veo 3 on Vertex and normalize the response to a playable URL.

    The google-genai SDK evolved twice for Veo: an older shape exposed
    ``video_uri``/``thumbnail_uri`` directly on the ``GeneratedVideo``
    item, and the current shape wraps them in a nested ``video`` /
    ``thumbnail`` object with ``uri`` + ``video_bytes`` fields. We
    duck-type both here so a routine SDK bump doesn't silently break
    the intro.

    Returns:
      ``{"video_url": ..., "poster_url": ..., "reason": ...}`` with
      ``video_url`` set on success. Falls back to a ``data:video/mp4``
      URL when the SDK returned inline bytes instead of a GCS URI (no
      ``output_gcs_uri`` was configured). Returns ``None`` on any hard
      failure; a well-formed response with no usable payload returns
      ``{"reason": "veo_no_output"}`` so the caller can surface a
      more actionable error than "veo_unavailable".
    """
    try:
        from google import genai
        from google.genai import types  # noqa: F401 - kept for future config
    except ImportError:  # pragma: no cover - runtime env
        logger.warning("media.veo.no_sdk")
        return None
    settings = get_settings()
    if not settings.google_cloud_project:
        return None
    try:
        client = genai.Client(
            vertexai=True,
            project=settings.google_cloud_project,
            location=settings.google_cloud_region,
        )
        op = await asyncio.to_thread(
            client.models.generate_videos, model=model, prompt=prompt
        )
        for _ in range(VEO_POLL_CEILING_SECONDS):
            if getattr(op, "done", False):
                break
            await asyncio.sleep(1.0)
            op = await asyncio.to_thread(client.operations.get, op)
        response = getattr(op, "response", None) or {}
        videos = getattr(response, "generated_videos", None) or []
        if not videos:
            logger.warning("media.veo.no_videos")
            return {"reason": "veo_no_output"}
        first = videos[0]
        video_obj = getattr(first, "video", None) or first
        thumb_obj = getattr(first, "thumbnail", None) or first
        video_url = (
            getattr(video_obj, "uri", None)
            or getattr(first, "video_uri", None)
            or ""
        )
        poster_url = (
            getattr(thumb_obj, "uri", None)
            or getattr(first, "thumbnail_uri", None)
            or ""
        )
        if not video_url:
            raw = getattr(video_obj, "video_bytes", None)
            if isinstance(raw, str) and raw:
                try:
                    raw = base64.b64decode(raw)
                except Exception:  # noqa: BLE001
                    raw = None
            if isinstance(raw, bytes) and raw:
                video_url = (
                    await asyncio.to_thread(
                        _upload_mp4_bytes, raw, INTRO_BLOB_NAME
                    )
                    or ""
                )
        else:
            video_url = await _ensure_playable_url(video_url)
        if poster_url:
            poster_url = _gcs_https_url(poster_url)
        if not video_url:
            logger.warning("media.veo.no_video_url")
            return {"reason": "veo_no_output"}
        return {"video_url": video_url, "poster_url": poster_url}
    except Exception as err:  # noqa: BLE001 - media must never break chat
        logger.warning("media.veo.failed", err=str(err)[:200])
        return None


async def _generate_lyria(*, mood: str, model: str) -> dict[str, str] | None:
    """Call Lyria 3 on Vertex via the Interactions API. Returns None
    on any failure so callers can silently degrade.

    Two Vertex-specific gotchas encoded here:

    1. **Wrong method**: an older iteration used
       ``client.models.generate_music()`` which was never a real SDK
       surface. Lyria 3 lives on ``client.interactions.create()`` and
       ``client.models.generate_content()`` returns 400 for Lyria
       models on Vertex (google/genai issue #2533).

    2. **Region must be "global"**: Lyria 3 on Vertex only serves from
       the ``global`` location. Any regional location (us-central1,
       etc.) returns 500 InternalServerError. We hardcode ``global``
       here instead of ``settings.google_cloud_region`` for that
       reason - the setting still governs Gemini and Veo where
       regional placement matters.

    Prompt is fixed per mood, so the caller caches one blob per mood
    and the whole app shares three chimes.
    """
    prompts = {
        "calm": "A soft, unobtrusive ambient chime, warm piano, gentle intro tone.",
        "hopeful": "A hopeful mallet chime, morning light, uplifting intro tone.",
        "energetic": "A bright uplifting chord, subtle percussion, energetic intro tone.",
    }
    prompt = prompts.get(mood, prompts["calm"])
    try:
        from google import genai
    except ImportError:  # pragma: no cover
        logger.warning("media.lyria.no_sdk")
        return None
    settings = get_settings()
    if not settings.google_cloud_project:
        return None
    try:
        client = genai.Client(
            vertexai=True,
            project=settings.google_cloud_project,
            # Not settings.google_cloud_region - see docstring.
            location="global",
        )
        interaction = await asyncio.to_thread(
            client.interactions.create,
            model=model,
            input=prompt,
        )
        audio = getattr(interaction, "output_audio", None)
        if audio is None:
            logger.warning("media.lyria.no_audio", mood=mood)
            return None
        b64 = getattr(audio, "data", "") or ""
        if not b64:
            logger.warning("media.lyria.empty_audio", mood=mood)
            return None
        return {"audio_url": f"data:audio/mp3;base64,{b64}"}
    except Exception as err:  # noqa: BLE001
        logger.warning("media.lyria.failed", err=str(err)[:200])
        return None


__all__ = ["router", "IntroResponse", "ChimeResponse", "reset_intro_runtime_state"]
