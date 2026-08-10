"""Event-linked reminders learned from day check-ins (e.g. soccer → shoes)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import Field

from level_core.config import Settings, get_settings
from level_core.schemas.base import LevelModel, _new_id


class EventCue(LevelModel):
    cue_id: str = Field(default_factory=_new_id)
    user_id: str
    keywords: list[str] = Field(default_factory=list)
    reminder: str = Field(min_length=4, max_length=220)
    source_text: str = ""


class EventCueStore(Protocol):
    async def list_for_user(self, user_id: str) -> list[EventCue]: ...
    async def add(self, cue: EventCue) -> None: ...


class InMemoryEventCueStore:
    def __init__(self) -> None:
        self._items: dict[str, EventCue] = {}

    async def list_for_user(self, user_id: str) -> list[EventCue]:
        return [c for c in self._items.values() if c.user_id == user_id]

    async def add(self, cue: EventCue) -> None:
        # Dedupe near-identical reminders for same user+keyword set
        key = (cue.user_id, cue.reminder.strip().lower(), tuple(sorted(cue.keywords)))
        for existing in self._items.values():
            ek = (
                existing.user_id,
                existing.reminder.strip().lower(),
                tuple(sorted(existing.keywords)),
            )
            if ek == key:
                return
        self._items[cue.cue_id] = cue


class LocalFileEventCueStore(InMemoryEventCueStore):
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
        for row in raw.get("cues") or []:
            try:
                cue = EventCue(**row)
            except Exception:  # noqa: BLE001
                continue
            self._items[cue.cue_id] = cue

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cues": [c.model_dump(mode="json") for c in self._items.values()]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    async def add(self, cue: EventCue) -> None:
        await super().add(cue)
        self._save()


_STORE: EventCueStore | None = None


def build_event_cue_store(settings: Settings | None = None) -> EventCueStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    settings = settings or get_settings()
    if settings.is_local:
        _STORE = LocalFileEventCueStore(Path.cwd() / ".level" / "event_cues.json")
    else:
        _STORE = InMemoryEventCueStore()
    return _STORE


def match_cues_for_summary(summary: str, cues: list[EventCue]) -> list[str]:
    hay = (summary or "").lower()
    out: list[str] = []
    seen: set[str] = set()
    for cue in cues:
        if any(k.lower() in hay for k in cue.keywords if k.strip()):
            rem = cue.reminder.strip()
            if rem and rem not in seen:
                seen.add(rem)
                out.append(rem)
    return out[:3]


__all__ = [
    "EventCue",
    "EventCueStore",
    "build_event_cue_store",
    "match_cues_for_summary",
]
