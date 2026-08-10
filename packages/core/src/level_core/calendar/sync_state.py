"""Per-user Google Calendar sync state (agenda cache + watch channel).

Agenda refreshes are cheap and never touch the LLM / profile pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from level_core.config import Settings, get_settings
from level_core.schemas.base import LevelModel, _now_utc


class CachedCalendarEvent(LevelModel):
    id: str
    summary: str = "(no title)"
    start: str | None = None
    end: str | None = None
    all_day: bool = False
    status: str = "confirmed"
    recurring_event_id: str | None = None


class CalendarSyncState(LevelModel):
    user_id: str
    sync_token: str | None = None
    events: dict[str, CachedCalendarEvent] = Field(default_factory=dict)
    agenda_updated_at: datetime | None = None
    profile_ingested_at: datetime | None = None
    initial_sync_done: bool = False
    initial_sync_error: str | None = None
    channel_id: str | None = None
    resource_id: str | None = None
    channel_token: str | None = None
    channel_expiration_ms: int | None = None
    updated_at: datetime = Field(default_factory=_now_utc)


class CalendarSyncStore(Protocol):
    async def get(self, user_id: str) -> CalendarSyncState | None: ...
    async def upsert(self, state: CalendarSyncState) -> None: ...
    async def get_by_channel_id(self, channel_id: str) -> CalendarSyncState | None: ...


class InMemoryCalendarSyncStore:
    def __init__(self) -> None:
        self._by_user: dict[str, CalendarSyncState] = {}
        self._by_channel: dict[str, str] = {}

    async def get(self, user_id: str) -> CalendarSyncState | None:
        return self._by_user.get(user_id)

    async def upsert(self, state: CalendarSyncState) -> None:
        state = state.model_copy(update={"updated_at": _now_utc()})
        prev = self._by_user.get(state.user_id)
        if prev and prev.channel_id and prev.channel_id != state.channel_id:
            self._by_channel.pop(prev.channel_id, None)
        self._by_user[state.user_id] = state
        if state.channel_id:
            self._by_channel[state.channel_id] = state.user_id

    async def get_by_channel_id(self, channel_id: str) -> CalendarSyncState | None:
        uid = self._by_channel.get(channel_id)
        return self._by_user.get(uid) if uid else None


class LocalFileCalendarSyncStore(InMemoryCalendarSyncStore):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for row in raw.get("states") or []:
            try:
                # LevelModel is strict=True; JSON ISO datetimes need strict=False.
                state = CalendarSyncState.model_validate(row, strict=False)
            except Exception:  # noqa: BLE001
                continue
            self._by_user[state.user_id] = state
            if state.channel_id:
                self._by_channel[state.channel_id] = state.user_id

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "states": [s.model_dump(mode="json") for s in self._by_user.values()]
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    async def upsert(self, state: CalendarSyncState) -> None:
        await super().upsert(state)
        self._save()


_STORE: CalendarSyncStore | None = None


def _local_sync_path() -> Path:
    # packages/core/src/level_core/calendar/sync_state.py → repo root = parents[5]
    return Path(__file__).resolve().parents[5] / ".level" / "calendar_sync.json"


def build_calendar_sync_store(settings: Settings | None = None) -> CalendarSyncStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    settings = settings or get_settings()
    if settings.is_local:
        _STORE = LocalFileCalendarSyncStore(_local_sync_path())
    else:
        _STORE = InMemoryCalendarSyncStore()
    return _STORE


def events_for_local_day(
    state: CalendarSyncState,
    *,
    day_offset: int = 0,
    timezone_name: str = "America/Los_Angeles",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter cached events into a local calendar day (Today-shaped dicts)."""
    from zoneinfo import ZoneInfo

    from level_core.ingest.google_live import _parse_when

    now = now or datetime.now(tz=timezone.utc)
    local = now.astimezone(ZoneInfo(timezone_name)) + timedelta(days=day_offset)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = local.replace(hour=23, minute=59, second=59, microsecond=999999)

    out: list[dict[str, Any]] = []
    for ev in state.events.values():
        if (ev.status or "").lower() == "cancelled":
            continue
        when = _parse_when(ev.start)
        if when is None:
            continue
        local_when = when.astimezone(ZoneInfo(timezone_name))
        if day_start <= local_when <= day_end:
            out.append(
                {
                    "id": ev.id,
                    "summary": ev.summary,
                    "start": ev.start,
                    "end": ev.end,
                    "all_day": ev.all_day,
                }
            )

    def _key(row: dict[str, Any]) -> str:
        return str(row.get("start") or "")

    return sorted(out, key=_key)


__all__ = [
    "CachedCalendarEvent",
    "CalendarSyncState",
    "CalendarSyncStore",
    "build_calendar_sync_store",
    "events_for_local_day",
]
