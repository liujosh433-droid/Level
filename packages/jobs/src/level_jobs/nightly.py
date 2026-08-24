"""Nightly Cloud Run Job:

  1. For each user: refresh_agenda + enrich_agenda + recompute usuals.
  2. Trim chat_turns > 20 per user.
  3. Trim ai_audit > 30 days.
  4. Renew any calendar watch channel due to expire in < 2 days.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.sync import ensure_watch, refresh_agenda
from level_core.calendar.usuals import compute_usuals_from_events
from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.schemas import Usual
from level_core.storage.care_store import propose_usual, sync_usuals
from level_core.storage.factory import get_store
from level_core.tz import tz_for_store

logger = get_logger("nightly")


async def _process_user(user_id: str) -> None:
    store = get_store(user_id)
    tokens = await store.tokens.read() or {}
    if not tokens.get("access_token"):
        return

    result = await refresh_agenda(store)
    if result.fingerprint_changed:
        await enrich_agenda(store)
    events = await store.agenda.list()
    people = await store.people.list()
    tz = await tz_for_store(store)
    fresh_ids: set[str] = set()
    for c in compute_usuals_from_events(events, people, tz=tz):
        await propose_usual(
            store,
            person_id=c.person_id,
            weekday=c.weekday,
            hour_band=c.hour_band,
            activity_type=c.activity_type,
            display_summary=c.display_summary,
            source_event_uids=list(c.source_event_uids),
            confidence=c.confidence,
        )
        fresh_ids.add(Usual.compose_id(c.person_id, c.weekday, c.hour_band))
    await sync_usuals(store, fresh_ids)

    turns = sorted(
        await store.chat_turns.list(), key=lambda t: t.created_at, reverse=True
    )
    for t in turns[20:]:
        await store.chat_turns.delete(t.turn_id)

    cutoff = datetime.now(UTC) - timedelta(days=30)
    for a in await store.ai_audit.list():
        created = a.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            await store.ai_audit.delete(a.audit_id)

    await ensure_watch(store)


async def _list_users() -> list[str]:
    """Cloud impl scans Firestore for user docs. Local impl scans .level dir."""
    settings = get_settings()
    if settings.is_local:
        root = Path(".level/local_store")
        if not root.exists():
            return []
        return [p.name for p in root.iterdir() if p.is_dir()]
    from google.cloud import firestore

    client = firestore.Client(project=settings.google_cloud_project or None)
    return [d.id for d in client.collection("users").stream()]


async def main() -> None:
    users = await _list_users()
    logger.info("nightly.start", user_count=len(users))
    for uid in users:
        try:
            await _process_user(uid)
        except Exception as exc:
            logger.exception("nightly.user_failed", user_id=uid, exc=str(exc))
    logger.info("nightly.done")


if __name__ == "__main__":
    asyncio.run(main())
