"""Resolve a user's IANA timezone.

Cloud Run is UTC. The greeting and "today" window must use the person's
local zone, not the container clock or a date-only string parsed as UTC.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from level_core.config import get_settings


def resolve_tz_name(*candidates: str | None) -> str:
    fallback = get_settings().calendar_tz or "America/Los_Angeles"
    for raw in candidates:
        name = (raw or "").strip()
        if not name:
            continue
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return name
    try:
        ZoneInfo(fallback)
        return fallback
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def resolve_tz(*candidates: str | None) -> ZoneInfo:
    return ZoneInfo(resolve_tz_name(*candidates))


def tz_from_profile(profile: dict | None) -> ZoneInfo:
    raw = (profile or {}).get("tz")
    return resolve_tz(raw if isinstance(raw, str) else None)


async def tz_for_store(store: object) -> ZoneInfo:
    """IANA zone saved from the browser, else the app default."""
    profile = await store.profile.read() or {}  # type: ignore[attr-defined]
    return tz_from_profile(profile if isinstance(profile, dict) else None)
