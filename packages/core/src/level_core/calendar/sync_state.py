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
    # Agenda hash last sent through Care / usuals infer. Empty = never stamped.
    care_infer_fingerprint: str | None = None
    updated_at: datetime = Field(default_factory=_now_utc)


def watch_is_live(
    state: CalendarSyncState | None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when Google can still push to our webhook for this user."""
    if state is None or not state.channel_id or not state.resource_id:
        return False
    if not state.channel_expiration_ms:
        return True
    current = now or datetime.now(tz=timezone.utc)
    now_ms = int(current.timestamp() * 1000)
    return state.channel_expiration_ms > now_ms


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
        self._mtime: float | None = None
        self._load()

    def _file_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        """Pick up writes from other processes (scripts / reloads) before serving."""
        mtime = self._file_mtime()
        if mtime is None:
            return
        if self._mtime is not None and mtime <= self._mtime:
            return
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._mtime = None
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._by_user.clear()
        self._by_channel.clear()
        for row in raw.get("states") or []:
            try:
                # LevelModel is strict=True; JSON ISO datetimes need strict=False.
                state = CalendarSyncState.model_validate(row, strict=False)
            except Exception:  # noqa: BLE001
                continue
            self._by_user[state.user_id] = state
            if state.channel_id:
                self._by_channel[state.channel_id] = state.user_id
        self._mtime = self._file_mtime()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "states": [s.model_dump(mode="json") for s in self._by_user.values()]
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        self._mtime = self._file_mtime()

    async def get(self, user_id: str) -> CalendarSyncState | None:
        self._maybe_reload()
        return await super().get(user_id)

    async def get_by_channel_id(self, channel_id: str) -> CalendarSyncState | None:
        self._maybe_reload()
        return await super().get_by_channel_id(channel_id)

    async def upsert(self, state: CalendarSyncState) -> None:
        self._maybe_reload()
        await super().upsert(state)
        self._save()


_STORE: CalendarSyncStore | None = None


def _local_sync_path() -> Path:
    # packages/core/src/level_core/calendar/sync_state.py → repo root = parents[5]
    return Path(__file__).resolve().parents[5] / ".level" / "calendar_sync.json"


class FirestoreCalendarSyncStore:
    """Persist agenda cache + watch channel under users/{uid}/state/calendar_sync."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any = None

    def _db(self) -> Any:
        if self._client is None:
            from google.cloud.firestore_v1 import AsyncClient

            self._client = AsyncClient(
                project=self._settings.gcp_project,
                database=self._settings.firestore_database,
            )
        return self._client

    async def get(self, user_id: str) -> CalendarSyncState | None:
        snap = await (
            self._db()
            .collection("users")
            .document(user_id)
            .collection("state")
            .document("calendar_sync")
            .get()
        )
        if not snap.exists:
            return None
        return CalendarSyncState.model_validate(snap.to_dict() or {}, strict=False)

    async def upsert(self, state: CalendarSyncState) -> None:
        state = state.model_copy(update={"updated_at": _now_utc()})
        prev = await self.get(state.user_id)
        db = self._db()
        await (
            db.collection("users")
            .document(state.user_id)
            .collection("state")
            .document("calendar_sync")
            .set(state.model_dump(mode="json"), merge=True)
        )
        # Maintain channel → user reverse index for Google push webhooks.
        if prev and prev.channel_id and prev.channel_id != state.channel_id:
            try:
                await db.collection("calendar_channels").document(prev.channel_id).delete()
            except Exception:  # noqa: BLE001
                pass
        if state.channel_id:
            await db.collection("calendar_channels").document(state.channel_id).set(
                {"user_id": state.user_id},
                merge=True,
            )

    async def get_by_channel_id(self, channel_id: str) -> CalendarSyncState | None:
        snap = await self._db().collection("calendar_channels").document(channel_id).get()
        if not snap.exists:
            return None
        uid = (snap.to_dict() or {}).get("user_id")
        if not uid:
            return None
        return await self.get(str(uid))


def build_calendar_sync_store(settings: Settings | None = None) -> CalendarSyncStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    settings = settings or get_settings()
    if settings.is_local:
        _STORE = LocalFileCalendarSyncStore(_local_sync_path())
    else:
        _STORE = FirestoreCalendarSyncStore(settings=settings)
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
    "FirestoreCalendarSyncStore",
    "build_calendar_sync_store",
    "events_for_local_day",
    "watch_is_live",
]
