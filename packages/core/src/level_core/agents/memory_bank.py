"""Memory Bank: long-lived facts about the caregiver that outlive chat_turns.

The chat history in `chat_turns` is capped at 20 rows (nightly job trims
the tail). That's enough for immediate context but useless as long-term
memory. The Memory Bank fills the gap.

A memory is a short, verifiable statement the caregiver told Level
(explicitly, or via a keep/adjust chip). Memories are:

  - de-duplicated by lowercased text
  - capped at MAX_MEMORIES per user (LRU by last_used_at)
  - injected as few-shot context on GENERATOR agents (email, summary)
  - never leaked to third parties (Gmail, Calendar payloads)

Storage: piggybacks on the profile KV under `memory_bank`. That keeps
this a zero-migration feature — no new Firestore collection.

Data shape:
  {
    "memories": [
      {
        "id": "mem_...",
        "text": "Nova starts kindergarten Aug 25",
        "tags": ["nova", "school"],
        "created_at": ISO8601,
        "last_used_at": ISO8601,
        "source": "chat" | "feedback",
      }, ...
    ]
  }
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from level_core.observability import get_logger
from level_core.storage.base import UserStore

logger = get_logger(__name__)

MEMORY_KEY = "memory_bank"
MAX_MEMORIES = 40
MAX_MEMORY_TEXT = 240


def _new_id() -> str:
    return f"mem_{uuid.uuid4().hex[:12]}"


async def remember(
    store: UserStore,
    *,
    text: str,
    tags: list[str] | None = None,
    source: str = "chat",
) -> dict[str, Any] | None:
    """Persist one memory. Dedupes on lowercased text; noop when duplicate.

    Uses `profile.mutate()` so concurrent `record_charge()` gate counter
    updates (also on `profile`) don't clobber each other.
    """
    text = (text or "").strip()
    if not text or len(text) > MAX_MEMORY_TEXT:
        return None
    lower = text.lower()
    now = datetime.now(UTC).isoformat()

    memory_id = _new_id()
    saved: dict[str, Any] | None = None

    def _apply(profile: dict[str, Any]) -> dict[str, Any]:
        nonlocal saved
        profile = dict(profile)
        bank = profile.get(MEMORY_KEY) if isinstance(profile.get(MEMORY_KEY), dict) else {}
        memories = list(bank.get("memories") or []) if isinstance(bank, dict) else []

        for existing in memories:
            if isinstance(existing, dict) and str(existing.get("text", "")).lower() == lower:
                existing["last_used_at"] = now
                profile[MEMORY_KEY] = {"memories": memories}
                saved = existing
                return profile

        memory = {
            "id": memory_id,
            "text": text,
            "tags": [t.strip().lower() for t in (tags or []) if t.strip()],
            "created_at": now,
            "last_used_at": now,
            "source": source,
        }
        memories.append(memory)
        if len(memories) > MAX_MEMORIES:
            memories.sort(key=lambda m: m.get("last_used_at", ""))
            memories = memories[-MAX_MEMORIES:]
        profile[MEMORY_KEY] = {"memories": memories}
        saved = memory
        return profile

    await store.profile.mutate(_apply)
    if saved:
        logger.info(
            "memory.remembered",
            user=store.user_id,
            memory_id=saved.get("id"),
            source=source,
        )
    return saved


async def recall(
    store: UserStore,
    *,
    tags: list[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Return the top `limit` memories, optionally filtered by tag overlap.

    Ordered most-recently-used first so generator agents see the most
    relevant facts near the top of the few-shot block.
    """
    profile = await store.profile.read() or {}
    bank = profile.get(MEMORY_KEY)
    if not isinstance(bank, dict):
        return []
    memories = [m for m in (bank.get("memories") or []) if isinstance(m, dict)]

    if tags:
        want = {t.strip().lower() for t in tags if t.strip()}
        memories = [
            m for m in memories if want & set(m.get("tags") or []) or not want
        ]

    memories.sort(key=lambda m: m.get("last_used_at", ""), reverse=True)
    return memories[:limit]


async def touch(store: UserStore, *, memory_ids: list[str]) -> None:
    """Bump `last_used_at` on the given memories. Best-effort, no error."""
    if not memory_ids:
        return
    now = datetime.now(UTC).isoformat()
    ids = set(memory_ids)

    def _apply(profile: dict[str, Any]) -> dict[str, Any]:
        profile = dict(profile)
        bank = profile.get(MEMORY_KEY)
        if not isinstance(bank, dict):
            return profile
        memories = [m for m in (bank.get("memories") or []) if isinstance(m, dict)]
        for m in memories:
            if m.get("id") in ids:
                m["last_used_at"] = now
        profile[MEMORY_KEY] = {"memories": memories}
        return profile

    try:
        await store.profile.mutate(_apply)
    except Exception:  # noqa: BLE001 - touch is best-effort
        return


async def forget(store: UserStore, *, memory_id: str) -> bool:
    """Remove one memory. Used when the caregiver explicitly retracts."""
    removed = False

    def _apply(profile: dict[str, Any]) -> dict[str, Any]:
        nonlocal removed
        profile = dict(profile)
        bank = profile.get(MEMORY_KEY)
        if not isinstance(bank, dict):
            return profile
        before = bank.get("memories") or []
        memories = [
            m for m in before if isinstance(m, dict) and m.get("id") != memory_id
        ]
        if len(memories) != len(before):
            removed = True
            profile[MEMORY_KEY] = {"memories": memories}
        return profile

    await store.profile.mutate(_apply)
    return removed
