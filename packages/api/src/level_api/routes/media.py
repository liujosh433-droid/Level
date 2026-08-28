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
from datetime import date, datetime, timedelta, timezone
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
# Per-ISO-week counter for Regenerate-button uses of Veo. Lives
# under media_cache so it shares the same ISO-week rollover as the
# cached video. See ``_read_regen_quota`` for the reset rule.
REGEN_QUOTA_KEY = "recap_regens"
# Where a failed Veo generation memoizes its reason so the next
# poll can surface it instead of silently spawning a fresh
# background task. Without this, a Veo failure (veo_no_output,
# quota, region misconfig) would cascade into an infinite
# generate-fail-repoll-generate loop: each background task clears
# in_flight without writing a recap, so the next 6s poll sees a
# clean slate and starts over with a NEW ``started_at``. The
# visible symptom was the elapsed-time counter resetting from
# ~60s back to 0 forever with no video ever appearing. See
# ``ERROR_COOLDOWN_SECONDS`` for the retry gate.
RECAP_ERROR_KEY = "recap_error"
# Stale threshold for the in-flight flag. Set to 1.5x
# VEO_POLL_CEILING_SECONDS so the frontend re-poll window can
# NEVER clear a flag while a legit background task is still
# running (that would spawn a duplicate Veo call). Only crashed
# tasks or torn-down Cloud Run instances should ever trigger the
# stale path.
IN_FLIGHT_MAX_AGE_SECONDS = 900
# How long to keep polling Veo's long-running operation before
# giving up. Published latency numbers for Veo 3.1 Fast on Vertex:
#   * P50: 60-90s
#   * P90: 2-3 minutes (under load)
#   * P99: 4-6 minutes (peak load / cold cache in the region)
# The task is cheap while it waits (an asyncio.sleep loop); the
# real cost lives in the Veo call at the far end. 10 min gives
# us enough headroom to catch essentially every legit generation
# while still bailing out on genuinely hung operations.
VEO_POLL_CEILING_SECONDS = 600
# How long to memoize a Veo failure before allowing another
# automatic (cold-path) retry. Bounded so a genuinely transient
# outage (regional quota blip) heals on its own without judge
# intervention, but long enough that a hard misconfig
# (LEVEL_MEDIA_ENABLED=true but Veo not enabled in the project)
# doesn't burn a fresh $1.20 attempt every 6s of polling. The
# Regenerate button clears the cooldown so a user who thinks the
# outage has passed can retry immediately - but the automatic
# poll cycle stays gated to prevent runaway retries.
ERROR_COOLDOWN_SECONDS = 300


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
    # Regenerate-button quota state, echoed on every response so
    # the UI can render "N of M regenerations used this week"
    # preemptively (not just after a rejection).
    regenerations_used: int | None = None
    regenerations_max: int | None = None


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

    Duration: Veo 3.x accepts only 4/6/8-second clips (SDK default 8),
    so the prompt asks for 8s rather than the previous "15-second"
    which the model silently ignored.
    """
    if not highlights:
        highlights = ["a calm week", "family time", "small victories"]
    scene = "; ".join(highlights[:5])
    return (
        "A warm 8-second cinematic recap for a caregiver's week: "
        f"{scene}. "
        "Soft morning light, gentle motion, unhurried pacing, family-friendly. "
        "No text overlays. No people's faces in focus."
    )


def _read_regen_quota(cache: dict[str, Any], week_start_iso: str) -> dict[str, Any]:
    """Read the current regen quota, resetting on a new ISO week.

    Returns a dict with ``week_start`` and ``count`` fields, always
    tagged to the current week - so a caller can trust the count
    field without a separate "is this stale?" check. Doesn't
    persist; the caller either bumps and writes, or discards.
    """
    q = cache.get(REGEN_QUOTA_KEY) or {}
    if q.get("week_start") != week_start_iso:
        return {"week_start": week_start_iso, "count": 0}
    return {"week_start": week_start_iso, "count": int(q.get("count") or 0)}


def _regen_max() -> int:
    """Central place to fetch the configured max. Kept as a helper
    so tests can monkeypatch a single call rather than every call
    site."""
    return int(get_settings().level_veo_max_regens_per_week)


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
    Veo. Used by the /week "Regenerate" button, which shows its
    own loading state - a UI that explicitly asks for a fresh call
    is expected to wait for it. Rate-limited to
    ``level_veo_max_regens_per_week`` per user per ISO week to
    bound cost at demo-friendly spend levels.
    """
    settings = get_settings()
    from level_core.tz import tz_for_store

    tz = await tz_for_store(store)
    today = datetime.now(tz).date()
    week_start = _iso_week_start(today)
    week_start_iso = week_start.isoformat()
    regen_max = _regen_max()

    if not settings.level_media_enabled:
        return RecapResponse(
            ready=False,
            reason="media_disabled",
            week_start=week_start_iso,
            regenerations_max=regen_max,
            regenerations_used=0,
        )

    cache = await _read_cache(store)
    quota = _read_regen_quota(cache, week_start_iso)
    regens_used = int(quota["count"])

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
            regenerations_used=regens_used,
            regenerations_max=regen_max,
        )

    highlights = await _collect_highlights(store)
    prompt = _prompt_recap(highlights)

    if force:
        # Rate-limit the explicit Regenerate button. Count is
        # checked and incremented BEFORE the Veo call so a Veo
        # failure still consumes the credit - otherwise a spurious
        # veo_unavailable would let a user retry unbounded, which
        # is exactly the demo-cost story we're trying to protect.
        if regens_used >= regen_max:
            logger.info(
                "media.veo.regen_limit_reached",
                user=store.user_id,
                week=week_start_iso,
                used=regens_used,
                max=regen_max,
            )
            return RecapResponse(
                ready=False,
                reason="regeneration_limit_reached",
                week_start=week_start_iso,
                regenerations_used=regens_used,
                regenerations_max=regen_max,
            )
        # A user who explicitly clicked Regenerate is telling us
        # they want to retry NOW, so wipe any active cooldown
        # before invoking Veo. If Veo still fails, the sync path
        # will re-write the cooldown so the automatic poll cycle
        # remains gated - only the explicit user action bypasses.
        cache.pop(RECAP_ERROR_KEY, None)
        cache[REGEN_QUOTA_KEY] = {
            "week_start": week_start_iso,
            "count": regens_used + 1,
        }
        await _write_cache(store, cache)
        result = await _run_recap_synchronously(
            store=store,
            cache=cache,
            prompt=prompt,
            week_start_iso=week_start_iso,
            model=settings.level_model_veo,
        )
        # ``_run_recap_synchronously`` may re-read cache before it
        # writes, but the quota bump above landed first - the
        # response just needs the updated count reflected.
        result.regenerations_used = regens_used + 1
        result.regenerations_max = regen_max
        return result

    # Failure memoization gate. If a prior generation for this week
    # failed inside the cooldown window, return the memoized reason
    # instead of spawning yet another attempt. Without this the
    # frontend polls every 6s, each poll finds "no in-flight, no
    # recap" and spawns a new $1.20 background task with a fresh
    # started_at - producing the "elapsed counter loops from 0 to
    # ~60s forever" symptom and burning credits on every cycle.
    recap_error = cache.get(RECAP_ERROR_KEY) or {}
    if recap_error.get("week_start") == week_start_iso:
        failed_at = _parse_iso_datetime(recap_error.get("failed_at"))
        if failed_at is not None:
            error_age = (datetime.now(timezone.utc) - failed_at).total_seconds()
            if error_age < ERROR_COOLDOWN_SECONDS:
                return RecapResponse(
                    ready=False,
                    reason=recap_error.get("reason") or "veo_unavailable",
                    week_start=week_start_iso,
                    regenerations_used=regens_used,
                    regenerations_max=regen_max,
                )
        # Cooldown expired - clear it and let the cold path retry.
        # A stale/unparseable timestamp falls through here too so
        # a corrupt blob can't strand the user forever.
        cache.pop(RECAP_ERROR_KEY, None)

    # Not cached, not forcing. Check whether a background task is
    # already running for this week. The flag is only trusted while
    # fresh - a stale one (crashed task, torn-down Cloud Run
    # instance) is cleared so we don't wait forever on a ghost.
    in_flight = cache.get(IN_FLIGHT_KEY) or {}
    if in_flight.get("week_start") == week_start_iso:
        started_at = _parse_iso_datetime(in_flight.get("started_at"))
        if started_at is not None:
            # ``started_at`` is tz-aware because we serialize with
            # ``datetime.now(timezone.utc).isoformat()`` below;
            # comparing against a tz-aware "now" avoids the naive/
            # aware TypeError that would 500 the endpoint on any
            # subsequent GET.
            age = (datetime.now(timezone.utc) - started_at).total_seconds()
            if age < IN_FLIGHT_MAX_AGE_SECONDS:
                return RecapResponse(
                    ready=False,
                    reason="generating",
                    generating=True,
                    started_at=in_flight.get("started_at"),
                    week_start=week_start_iso,
                    regenerations_used=regens_used,
                    regenerations_max=regen_max,
                )
        # Stale or unparseable - fall through to kick a fresh task.

    # Tz-aware ISO string so the frontend can Date.parse() it
    # unambiguously as UTC and compute elapsed seconds for the
    # "still generating..." hint. A naive datetime.utcnow() would
    # be interpreted as local time by JS Date parsers, throwing
    # the elapsed count off by the client's timezone offset.
    started_at_iso = datetime.now(timezone.utc).isoformat()
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
        regenerations_used=regens_used,
        regenerations_max=regen_max,
    )


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Best-effort tz-aware ISO parser for the in-flight timestamp.

    Kept lenient because a malformed value in the profile blob
    should degrade to "treat as stale" rather than 500 the caller.

    Naive ISO strings (from the pre-timezone version of this
    module) are treated as UTC rather than local time - anything
    written by the old code path was ``datetime.utcnow()``, which
    is UTC in fact if not in tzinfo.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


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
        exc_reason: str | None = None
    except Exception as err:  # noqa: BLE001 - defensive; media must never break chat
        logger.warning("media.veo.background_exception", err=str(err)[:200])
        result = None
        exc_reason = "veo_unavailable"

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
        # A success wipes any prior error - the outage that caused
        # it has clearly cleared. Leaving a stale error blob here
        # would let the next poll (unluckily hitting the stale
        # cooldown before its own cache read caught up) still
        # surface an "unavailable" for a video that just landed.
        cache.pop(RECAP_ERROR_KEY, None)
    else:
        # Persist the reason so the next poll cycle stops looping.
        # Without this, an empty background-task exit clears the
        # in-flight flag but writes no recap - the next 6s poll
        # then sees "no in-flight, no recap" and treats it as
        # cold, spawning ANOTHER background task with a fresh
        # ``started_at``. Symptom: elapsed counter climbs to ~60s,
        # resets to 0, climbs again, forever. Fix: memoize the
        # failure so subsequent polls return the actual reason
        # (frontend treats it as terminal and stops polling).
        cache[RECAP_ERROR_KEY] = {
            "week_start": week_start_iso,
            "reason": (result or {}).get("reason") or exc_reason or "veo_unavailable",
            "failed_at": datetime.now(timezone.utc).isoformat(),
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
        # Same error-memoization contract as the background path:
        # a failed synchronous force=true persists the reason so a
        # subsequent cold-path GET returns the terminal error
        # (within the cooldown window) rather than kicking off a
        # fresh background task. Otherwise the user hits Regenerate,
        # sees "unavailable," and every subsequent /week visit
        # spawns a $1.20 attempt in the background.
        cache[RECAP_ERROR_KEY] = {
            "week_start": week_start_iso,
            "reason": (result or {}).get("reason") or "veo_unavailable",
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        await _write_cache(store, cache)
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
        # Success wipes any prior cooldown - same rationale as the
        # background path.
        cache.pop(RECAP_ERROR_KEY, None)
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
        # Poll for completion up to VEO_POLL_CEILING_SECONDS. Veo
        # 3.1 Fast has a wide latency distribution on Vertex - see
        # the constant's docstring. Earlier ceilings (90s, then
        # 300s) were silently aborting mid-generation on legit
        # slow-but-not-hung runs and causing the frontend to
        # re-poll into a fresh background task, doubling the cost
        # for no benefit. The stale-flag guard on the in-flight
        # key uses a 1.5x version of this ceiling so a re-poll
        # can't spawn a duplicate while a legit task is still
        # running.
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
