"""Nightly Cloud Run Job:

  1. For each user: refresh_agenda + enrich_agenda + recompute usuals.
  2. Detect missing-usual gaps this week and stash proactive cards
     (Collaborative Partner: autonomous background action).
  3. Trim chat_turns > 20 per user.
  4. Trim ai_audit > 30 days.
  5. Renew any calendar watch channel due to expire in < 2 days.

Failure semantics:

  * A single user failure is logged and the loop continues.
  * The job exits with status 0 (success) when every user succeeded,
    and with status 1 (failure) when any user failed. This lets Cloud
    Run Jobs alerting see systemic breakage — the previous code
    swallowed every exception and always returned success, so a hard
    outage in, say, ``refresh_agenda`` was invisible.

Pagination:

  * ``_list_users`` streams via a bounded page size in the cloud path
    so a growing user base doesn't buffer thousands of doc ids in
    memory before the loop starts.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

from level_core.calendar.enrich import enrich_agenda
from level_core.calendar.proactive import (
    MAX_PROACTIVE_CARDS,
    PROACTIVE_CARDS_KEY,
    regenerate_proactive_cards,
)
from level_core.calendar.sync import ensure_watch, refresh_agenda
from level_core.calendar.usuals import compute_usuals_from_events
from level_core.config import get_settings
from level_core.observability import get_logger
from level_core.schemas import Usual
from level_core.storage.care_store import propose_usual, sync_usuals
from level_core.storage.factory import get_store
from level_core.tz import as_utc, tz_for_store

logger = get_logger("nightly")

# Re-exported from level_core.calendar.proactive so any legacy import
# of ``nightly.PROACTIVE_CARDS_KEY`` / ``nightly.MAX_PROACTIVE_CARDS``
# continues to work. New callers should import from
# ``level_core.calendar.proactive`` directly.
__all__ = ["PROACTIVE_CARDS_KEY", "MAX_PROACTIVE_CARDS", "main"]

# Cap enrichment work per user per nightly run. A user with 500+
# unclassified events shouldn't consume all their daily LLM budget in
# one background pass — the interactive path can pick up the tail on
# the next sync.
_ENRICH_EVENT_CAP = 200


async def _process_user(user_id: str) -> None:
    store = get_store(user_id)
    tokens = await store.tokens.read() or {}
    if not tokens.get("access_token"):
        return

    result = await refresh_agenda(store)
    if result.fingerprint_changed:
        # enrich_agenda respects the per-user gate, so a soft-degraded
        # response is already possible. The cap here is a belt-and-
        # braces limit so a first-time user with an enormous calendar
        # doesn't stampede the free-tier quota on the very first
        # nightly run.
        await enrich_agenda(store, max_events=_ENRICH_EVENT_CAP)
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

    await regenerate_proactive_cards(store, tz=tz, events=events, people=people)

    # Trim chat turns to the last 20 by created_at. Uses list() —
    # per-user chat volume is small (<100) so a full read is fine.
    turns = sorted(
        await store.chat_turns.list(), key=lambda t: as_utc(t.created_at), reverse=True
    )
    for t in turns[20:]:
        await store.chat_turns.delete(t.turn_id)

    # Trim ai_audit older than 30 days. Prefer the bounded
    # ``list_since`` query on the backend so we only touch rows past
    # the cutoff — falls back to a full scan when unavailable.
    cutoff = datetime.now(UTC) - timedelta(days=30)
    older_fn = getattr(store.ai_audit, "list_before", None)
    if callable(older_fn):
        try:
            old_rows = await older_fn(cutoff)
        except TypeError:
            old_rows = await store.ai_audit.list()
    else:
        old_rows = await store.ai_audit.list()
    for a in old_rows:
        created = a.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            await store.ai_audit.delete(a.audit_id)

    await ensure_watch(store)


async def _iter_users() -> AsyncIterator[str]:
    """Yield user ids one at a time.

    Cloud path uses Firestore's server-side pagination so a growing
    user base doesn't buffer the whole id list in memory. Local path
    streams directory entries from ``.level/local_store``.
    """
    settings = get_settings()
    if settings.is_local:
        root = Path(".level/local_store")
        if not root.exists():
            return
        for p in sorted(root.iterdir()):
            if p.is_dir():
                yield p.name
        return
    from google.cloud import firestore

    client = firestore.Client(project=settings.google_cloud_project or None)
    page_size = 200
    query = client.collection("users").limit(page_size)
    while True:
        # Firestore's Python client returns a generator; materialize
        # per page so the outer async loop can hand control back to
        # the event loop between pages.
        docs = await asyncio.to_thread(lambda: list(query.stream()))
        if not docs:
            return
        for doc in docs:
            yield doc.id
        if len(docs) < page_size:
            return
        query = (
            client.collection("users").start_after(docs[-1]).limit(page_size)
        )


async def main() -> int:
    """Run one nightly pass. Returns the exit code (0 = success)."""
    processed = 0
    failed = 0
    async for uid in _iter_users():
        processed += 1
        try:
            await _process_user(uid)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("nightly.user_failed", user_id=uid, exc=str(exc))
    logger.info("nightly.done", processed=processed, failed=failed)
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
