"""Backend selection. Import this, not the concrete backends.

Every caller into the storage layer must go through ``get_store``,
which is the single choke point that sanitizes ``user_id`` before it
becomes a path segment (local JSON) or a document key (Firestore).
That closes a path-traversal exposure in the local backend and
prevents any ADK / webhook code path from accidentally reading or
writing another tenant's data by supplying a hand-crafted id.
"""

from __future__ import annotations

import re

from level_core.config import get_settings
from level_core.storage.base import UserStore

# Allowed characters for user_id after normalization. Callers use short
# stable ids (``u_<hex>`` for OAuth, ``u_demo_<scenario>_<slot>`` for
# demo), so a strict allowlist is safe and precludes any traversal or
# separator confusion.
_USER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class InvalidUserId(ValueError):
    """Raised when a caller supplies a user_id we refuse to slot into storage."""


def sanitize_user_id(user_id: str) -> str:
    """Return ``user_id`` if it matches the allowlist, else raise.

    Rules:
      * must be a str
      * 1..128 characters of ``[A-Za-z0-9._-]``
      * must not be a path traversal token (``.``, ``..``)
      * ``/``, ``\\``, whitespace, and control characters are rejected

    ``get_store`` calls this before touching either backend, so a
    caller with a hostile id fails loudly rather than silently
    escaping into another user's directory.
    """
    if not isinstance(user_id, str):
        raise InvalidUserId("user_id_must_be_str")
    trimmed = user_id.strip()
    if not trimmed:
        raise InvalidUserId("user_id_empty")
    if trimmed in {".", ".."}:
        raise InvalidUserId("user_id_traversal")
    if not _USER_ID_RE.match(trimmed):
        raise InvalidUserId("user_id_invalid_chars")
    return trimmed


def get_store(user_id: str) -> UserStore:
    safe_user_id = sanitize_user_id(user_id)
    settings = get_settings()
    if settings.is_local:
        from level_core.storage.local_json import make_local_store

        return make_local_store(safe_user_id)
    from level_core.storage.firestore import make_firestore_store

    return make_firestore_store(safe_user_id)
