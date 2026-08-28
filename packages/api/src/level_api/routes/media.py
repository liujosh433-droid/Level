"""Multimodal media: Veo (video recap) + Lyria (audio chime).

Hackathon bonus: rules give +0.2 for each additional Google AI model
integrated (Gemma, Veo, Lyria) up to +0.6. Level integrates Veo for a
weekly recap on /week and Lyria for a start/end chime on "Hear my day".

Both endpoints degrade gracefully when the caller isn't configured for
Vertex AI or when `LEVEL_MEDIA_ENABLED=false`. They return
`{ready: false, reason: "..."}` and the frontend renders a static
placeholder so the demo never breaks.

Cost control:
  - Veo output is cached per ISO week and per-user (single request per
    week per user). Repeat calls in the same week return the cached URL.
  - Lyria output is deterministic-per-mood; we cache a small library and
    reuse across all users (no PII in the prompt).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import date, datetime, timedelta
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
IN_FLIGHT_KEY = "recap_in_flight"
# Stale threshold for the in-flight flag. Veo previews land in
# 30-60s in the happy path, so 5 min covers the polling ceiling
# (90s) plus a wide slack. If a background task crashes or the
# Cloud Run instance is torn down mid-generation, the next GET
# after this window clears the flag and re-attempts rather than
# waiting forever for a ghost task.
IN_FLIGHT_MAX_AGE_SECONDS = 300


class RecapResponse(BaseModel):
    ready: bool
    reason: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    week_start: str | None = None
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


def _iso_week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


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


def _prompt_recap(highlights: list[str]) -> str:
    """Build the Veo prompt from this week's priorities + top events.

    Deterministic and PII-free: names are stripped upstream (recap only
    receives category labels), so the prompt is safe to hash+cache.
    """
    if not highlights:
        highlights = ["a calm week", "family time", "small victories"]
    scene = "; ".join(highlights[:5])
    return (
        "A warm 15-second cinematic recap for a caregiver's week: "
        f"{scene}. "
        "Soft morning light, gentle motion, unhurried pacing, family-friendly. "
        "No text overlays. No people's faces in focus."
    )


async def _collect_highlights(store: UserStore) -> list[str]:
    """Category-label rollup for this week — no PII into the prompt."""
    events = await store.agenda.list()
    priorities = await store.priorities.list()
    tz = None
    from level_core.tz import tz_for_store

    tz = await tz_for_store(store)
    today = datetime.now(tz).date()
    week_start = _iso_week_start(today)
    week_end = week_start + timedelta(days=7)
    by_category: dict[str, int] = {}
    for e in events:
        if not e.activity_type:
            continue
        local_start = e.time.start.astimezone(tz)
        if not (week_start <= local_start.date() < week_end):
            continue
        label = e.activity_type.category.label
        by_category[label] = by_category.get(label, 0) + 1
    top = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)[:3]
    labels = [f"{label} time" for label, _ in top]
    for p in priorities[:2]:
        # Content words only — we've already stripped PII in call_agent,
        # but Veo doesn't need identifying text either.
        labels.append(f"space for {p.text.strip().lower()[:60]}")
    return labels


@router.get("/recap", response_model=RecapResponse)
async def weekly_recap(
    store: UserStore = Depends(get_user_store),
    force: bool = Query(default=False),
) -> RecapResponse:
    """Return this week's Veo recap - cached, generating, or kick off generation.

    Three paths, each guaranteed to return in <100ms so /week
    never sits on an open HTTP request:

    1. **Cached hit**: return the stored URL with ``cached=true``.
    2. **In-flight**: a background task is already generating this
       week's recap; return ``{ready:false, generating:true,
       started_at:...}``. The frontend polls this endpoint until
       ``ready:true`` or the reason changes to a terminal error.
    3. **Cold**: set the in-flight flag, spawn a background task,
       return ``{ready:false, generating:true, started_at:now}``.

    ``force=true`` bypasses the cache and blocks synchronously on
    Veo (30-60s). Used by the /week "Regenerate" button, which
    shows its own loading state - a UI that explicitly asks for a
    fresh call is expected to wait for it.
    """
    settings = get_settings()
    from level_core.tz import tz_for_store

    tz = await tz_for_store(store)
    today = datetime.now(tz).date()
    week_start = _iso_week_start(today)
    week_start_iso = week_start.isoformat()

    if not settings.level_media_enabled:
        return RecapResponse(
            ready=False,
            reason="media_disabled",
            week_start=week_start_iso,
        )

    cache = await _read_cache(store)
    cached = cache.get("recap") or {}
    if (
        not force
        and cached.get("week_start") == week_start_iso
        and cached.get("video_url")
    ):
        return RecapResponse(
            ready=True,
            video_url=cached.get("video_url"),
            poster_url=cached.get("poster_url"),
            week_start=week_start_iso,
            model=cached.get("model"),
            cached=True,
        )

    highlights = await _collect_highlights(store)
    prompt = _prompt_recap(highlights)

    if force:
        return await _run_recap_synchronously(
            store=store,
            cache=cache,
            prompt=prompt,
            week_start_iso=week_start_iso,
            model=settings.level_model_veo,
        )

    # Not cached, not forcing. Check whether a background task is
    # already running for this week. The flag is only trusted while
    # fresh - a stale one (crashed task, torn-down Cloud Run
    # instance) is cleared so we don't wait forever on a ghost.
    in_flight = cache.get(IN_FLIGHT_KEY) or {}
    if in_flight.get("week_start") == week_start_iso:
        started_at = _parse_iso_datetime(in_flight.get("started_at"))
        if started_at is not None:
            age = (datetime.utcnow() - started_at).total_seconds()
            if age < IN_FLIGHT_MAX_AGE_SECONDS:
                return RecapResponse(
                    ready=False,
                    reason="generating",
                    generating=True,
                    started_at=in_flight.get("started_at"),
                    week_start=week_start_iso,
                )
        # Stale or unparseable - fall through to kick a fresh task.

    started_at_iso = datetime.utcnow().isoformat()
    cache[IN_FLIGHT_KEY] = {
        "week_start": week_start_iso,
        "started_at": started_at_iso,
    }
    await _write_cache(store, cache)

    asyncio.create_task(
        _run_recap_in_background(
            store=store,
            prompt=prompt,
            week_start_iso=week_start_iso,
            model=settings.level_model_veo,
        )
    )
    logger.info(
        "media.veo.scheduled",
        user=store.user_id,
        week=week_start_iso,
        model=settings.level_model_veo,
    )
    return RecapResponse(
        ready=False,
        reason="generating",
        generating=True,
        started_at=started_at_iso,
        week_start=week_start_iso,
    )


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Best-effort ISO parser for the in-flight timestamp.

    Kept lenient because a malformed value in the profile blob
    should degrade to "treat as stale" rather than 500 the caller.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _run_recap_in_background(
    *,
    store: UserStore,
    prompt: str,
    week_start_iso: str,
    model: str,
) -> None:
    """Fire-and-forget generation. Writes result + clears flag.

    Runs on the FastAPI event loop after the request that spawned
    it has already returned. Any exception here would be silently
    lost by asyncio, so we log + always clear the in-flight flag
    so the next GET can retry rather than showing "generating"
    forever.
    """
    try:
        result = await _generate_veo(prompt=prompt, model=model)
    except Exception as err:  # noqa: BLE001 - defensive; media must never break chat
        logger.warning("media.veo.background_exception", err=str(err)[:200])
        result = None

    # Refresh the cache blob to avoid clobbering unrelated media
    # sub-keys (chime cache, etc.) that other requests may have
    # written in the interim.
    cache = await _read_cache(store)
    cache.pop(IN_FLIGHT_KEY, None)

    video_url = (result or {}).get("video_url") or ""
    if video_url and not video_url.startswith("data:"):
        cache["recap"] = {
            "week_start": week_start_iso,
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:16],
            "video_url": video_url,
            "poster_url": (result or {}).get("poster_url") or None,
            "model": model,
            "generated_at": datetime.utcnow().isoformat(),
        }
    await _write_cache(store, cache)
    logger.info(
        "media.veo.background_done",
        user=store.user_id,
        week=week_start_iso,
        model=model,
        ok=bool(video_url),
        cached_in_profile=bool(video_url and not video_url.startswith("data:")),
    )


async def _run_recap_synchronously(
    *,
    store: UserStore,
    cache: dict[str, Any],
    prompt: str,
    week_start_iso: str,
    model: str,
) -> RecapResponse:
    """Legacy synchronous path, retained for the Regenerate button.

    Shares the cache-write invariants with the background task -
    data URLs are returned but never persisted, real URIs land in
    the profile.
    """
    result = await _generate_veo(prompt=prompt, model=model)
    if not result or not result.get("video_url"):
        return RecapResponse(
            ready=False,
            reason=(result or {}).get("reason") or "veo_unavailable",
            week_start=week_start_iso,
        )
    video_url = result["video_url"]
    poster_url = result.get("poster_url") or None
    if not video_url.startswith("data:"):
        cache["recap"] = {
            "week_start": week_start_iso,
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:16],
            "video_url": video_url,
            "poster_url": poster_url,
            "model": model,
            "generated_at": datetime.utcnow().isoformat(),
        }
        await _write_cache(store, cache)
    logger.info(
        "media.veo.generated",
        user=store.user_id,
        week=week_start_iso,
        model=model,
        cached_in_profile=not video_url.startswith("data:"),
    )
    return RecapResponse(
        ready=True,
        video_url=video_url,
        poster_url=poster_url,
        week_start=week_start_iso,
        model=model,
        cached=False,
    )


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


async def _generate_veo(*, prompt: str, model: str) -> dict[str, str] | None:
    """Call Veo 3 on Vertex and normalize the response to a playable URL.

    The google-genai SDK evolved twice for Veo: an older shape exposed
    ``video_uri``/``thumbnail_uri`` directly on the ``GeneratedVideo``
    item, and the current shape wraps them in a nested ``video`` /
    ``thumbnail`` object with ``uri`` + ``video_bytes`` fields. We
    duck-type both here so a routine SDK bump doesn't silently break
    the recap.

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
        # Veo generate_videos is a long-running op; we surface only the
        # first video URI. If Vertex hasn't enabled Veo in this project,
        # this call raises and we degrade gracefully.
        op = await asyncio.to_thread(
            client.models.generate_videos, model=model, prompt=prompt
        )
        # Poll for completion up to 90s; Veo 3 previews usually return
        # in 30-60s. Anything longer, we skip and let the next call
        # retry - the cache means the wait only lands once per user
        # per week.
        for _ in range(90):
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
        # New SDK: first.video is a Video object with .uri + .video_bytes.
        # Old SDK: first.video_uri / first.thumbnail_uri live directly.
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
            # No GCS URI - the SDK returned inline bytes. Wrap them as
            # a data URL so the browser can still play the clip; the
            # caller decides whether to cache it (data URLs are too
            # big for the profile doc).
            raw = getattr(video_obj, "video_bytes", None)
            if isinstance(raw, bytes):
                b64 = base64.b64encode(raw).decode("ascii")
                video_url = f"data:video/mp4;base64,{b64}"
            elif isinstance(raw, str) and raw:
                video_url = f"data:video/mp4;base64,{raw}"
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
        # output_audio.data is base64-encoded MP3 per the Interactions
        # API. Rather than upload to GCS (extra IAM, bucket, TTL) we
        # ship it back as a data: URL - the frontend Audio() element
        # accepts these transparently and Firestore's 1 MB doc cap
        # comfortably holds one ~500 KB base64 chime per mood.
        b64 = getattr(audio, "data", "") or ""
        if not b64:
            logger.warning("media.lyria.empty_audio", mood=mood)
            return None
        return {"audio_url": f"data:audio/mp3;base64,{b64}"}
    except Exception as err:  # noqa: BLE001
        logger.warning("media.lyria.failed", err=str(err)[:200])
        return None


__all__ = ["router", "RecapResponse", "ChimeResponse"]
_ = json  # keep import for future audit-trail work
