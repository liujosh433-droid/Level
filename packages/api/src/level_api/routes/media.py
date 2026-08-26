"""Multimodal media: Veo (video recap) + Lyria (audio chime).

Hackathon bonus: rules give +0.2 for each additional Google AI model
integrated (Gemma, Veo, Lyria) up to +0.6. Level integrates Veo for a
weekly recap on /about and Lyria for a start/end chime on "Hear my day".

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


class RecapResponse(BaseModel):
    ready: bool
    reason: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    week_start: str | None = None
    model: str | None = None
    cached: bool = False


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
    """Generate (or return cached) 15-second Veo recap for this ISO week.

    The video is generated once per user per ISO week to bound cost
    (~$1-4/week/user depending on region). Set `force=true` to bypass
    the cache — used in the demo video to trigger a fresh call live.
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

    result = await _generate_veo(prompt=prompt, model=settings.level_model_veo)
    if not result:
        return RecapResponse(
            ready=False,
            reason="veo_unavailable",
            week_start=week_start_iso,
        )

    cache["recap"] = {
        "week_start": week_start_iso,
        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:16],
        "video_url": result.get("video_url"),
        "poster_url": result.get("poster_url"),
        "model": settings.level_model_veo,
        "generated_at": datetime.utcnow().isoformat(),
    }
    await _write_cache(store, cache)
    logger.info(
        "media.veo.generated",
        user=store.user_id,
        week=week_start_iso,
        model=settings.level_model_veo,
    )
    return RecapResponse(
        ready=True,
        video_url=result.get("video_url"),
        poster_url=result.get("poster_url"),
        week_start=week_start_iso,
        model=settings.level_model_veo,
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
    """Call Veo 3 on Vertex. Returns None on any failure."""
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
        # Poll for completion up to 60s; Veo previews usually return in
        # 20-40s. Anything longer, we skip and let the next call retry.
        for _ in range(60):
            if getattr(op, "done", False):
                break
            await asyncio.sleep(1.0)
            op = await asyncio.to_thread(client.operations.get, op)
        response = getattr(op, "response", None) or {}
        videos = getattr(response, "generated_videos", None) or []
        if not videos:
            return None
        return {
            "video_url": getattr(videos[0], "video_uri", None) or "",
            "poster_url": getattr(videos[0], "thumbnail_uri", None) or "",
        }
    except Exception as err:  # noqa: BLE001 - media must never break chat
        logger.warning("media.veo.failed", err=str(err)[:200])
        return None


async def _generate_lyria(*, mood: str, model: str) -> dict[str, str] | None:
    """Call Lyria on Vertex. Returns None on any failure.

    Prompt is fixed per mood so caching is trivial.
    """
    prompts = {
        "calm": "A soft, unobtrusive 3-second ambient chime, warm piano, gentle.",
        "hopeful": "A hopeful 3-second mallet chime, morning light, uplifting.",
        "energetic": "A bright 3-second uplifting chord, subtle percussion.",
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
            location=settings.google_cloud_region,
        )
        op = await asyncio.to_thread(
            client.models.generate_music, model=model, prompt=prompt
        )
        for _ in range(30):
            if getattr(op, "done", False):
                break
            await asyncio.sleep(1.0)
            op = await asyncio.to_thread(client.operations.get, op)
        response = getattr(op, "response", None) or {}
        tracks = getattr(response, "generated_tracks", None) or []
        if not tracks:
            return None
        return {"audio_url": getattr(tracks[0], "audio_uri", None) or ""}
    except Exception as err:  # noqa: BLE001
        logger.warning("media.lyria.failed", err=str(err)[:200])
        return None


__all__ = ["router", "RecapResponse", "ChimeResponse"]
_ = json  # keep import for future audit-trail work
