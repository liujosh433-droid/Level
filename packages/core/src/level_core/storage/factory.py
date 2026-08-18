"""Backend selection. Import this, not the concrete backends."""

from __future__ import annotations

from level_core.config import get_settings
from level_core.storage.base import UserStore


def get_store(user_id: str) -> UserStore:
    settings = get_settings()
    if settings.is_local:
        from level_core.storage.local_json import make_local_store

        return make_local_store(user_id)
    from level_core.storage.firestore import make_firestore_store

    return make_firestore_store(user_id)
