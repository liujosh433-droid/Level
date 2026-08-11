"""OAuth token persistence (local JSON file, Firestore for cloud)."""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

from level_core.config import Settings, get_settings
from level_core.schemas.user import OAuthToken, User


class TokenStore(Protocol):
    async def upsert_user(self, user: User) -> None: ...
    async def get_user(self, user_id: str) -> User | None: ...
    async def get_user_by_google_sub(self, google_sub: str) -> User | None: ...
    async def upsert_token(self, token: OAuthToken) -> None: ...
    async def get_google_token(self, user_id: str) -> OAuthToken | None: ...
    async def delete_google_token(self, user_id: str) -> bool: ...


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._by_sub: dict[str, str] = {}
        self._tokens: dict[str, OAuthToken] = {}

    async def upsert_user(self, user: User) -> None:
        self._users[user.user_id] = user
        if user.google_sub:
            self._by_sub[user.google_sub] = user.user_id

    async def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    async def get_user_by_google_sub(self, google_sub: str) -> User | None:
        uid = self._by_sub.get(google_sub)
        return self._users.get(uid) if uid else None

    async def upsert_token(self, token: OAuthToken) -> None:
        self._tokens[token.user_id] = token

    async def get_google_token(self, user_id: str) -> OAuthToken | None:
        return self._tokens.get(user_id)

    async def delete_google_token(self, user_id: str) -> bool:
        return self._tokens.pop(user_id, None) is not None


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Exclusive lock so uvicorn --reload workers can't clobber each other."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class LocalFileTokenStore(InMemoryTokenStore):
    """In-memory store that also persists to disk so uvicorn reload keeps OAuth."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        with _file_lock(self._path):
            self._load_unlocked()

    def _ingest(self, raw: dict[str, Any], *, prefer_incoming: bool) -> None:
        for u in raw.get("users") or []:
            try:
                # LevelModel is strict=True; JSON ISO datetimes need strict=False.
                user = User.model_validate(u, strict=False)
            except Exception:  # noqa: BLE001
                continue
            existing = self._users.get(user.user_id)
            if existing is None:
                self._users[user.user_id] = user
            elif prefer_incoming and user.updated_at >= existing.updated_at:
                self._users[user.user_id] = user
            if user.google_sub:
                self._by_sub[user.google_sub] = user.user_id
        for t in raw.get("tokens") or []:
            try:
                token = OAuthToken.model_validate(t, strict=False)
            except Exception:  # noqa: BLE001
                continue
            existing = self._tokens.get(token.user_id)
            if existing is None:
                self._tokens[token.user_id] = token
            elif prefer_incoming and token.updated_at >= existing.updated_at:
                self._tokens[token.user_id] = token
            elif existing is not None and not existing.refresh_token and token.refresh_token:
                # Never drop a refresh token we already persisted.
                self._tokens[token.user_id] = token

    def _read_raw(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        last_err: Exception | None = None
        for _ in range(5):
            try:
                text = self._path.read_text(encoding="utf-8")
                if not text.strip():
                    return {"users": [], "tokens": []}
                data = json.loads(text)
                if isinstance(data, dict):
                    return data
                return {"users": [], "tokens": []}
            except (OSError, json.JSONDecodeError) as exc:
                last_err = exc
                time.sleep(0.04)
        # Corrupt mid-write — keep memory as-is; don't treat as empty store.
        if last_err is not None:
            return None
        return None

    def _load_unlocked(self) -> None:
        raw = self._read_raw()
        if raw is None:
            return
        self._ingest(raw, prefer_incoming=True)

    def _merge_disk_unlocked(self) -> None:
        """Pull in any users/tokens written by another process before we save."""
        raw = self._read_raw()
        if raw is None:
            return
        self._ingest(raw, prefer_incoming=False)

    def _save(self) -> None:
        with _file_lock(self._path):
            self._merge_disk_unlocked()
            # Never overwrite a populated file with a totally empty snapshot.
            if not self._users and not self._tokens:
                disk = self._read_raw()
                if disk and (disk.get("users") or disk.get("tokens")):
                    self._ingest(disk, prefer_incoming=True)
                    return
            payload = {
                "users": [u.model_dump(mode="json") for u in self._users.values()],
                "tokens": [t.model_dump(mode="json") for t in self._tokens.values()],
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    async def upsert_user(self, user: User) -> None:
        await super().upsert_user(user)
        self._save()

    async def upsert_token(self, token: OAuthToken) -> None:
        await super().upsert_token(token)
        self._save()

    async def delete_google_token(self, user_id: str) -> bool:
        removed = await super().delete_google_token(user_id)
        if removed:
            self._save()
        return removed


class FirestoreTokenStore:
    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = client

    def _db(self) -> Any:
        if self._client is not None:
            return self._client
        from google.cloud.firestore_v1 import AsyncClient

        self._client = AsyncClient(
            project=self._settings.gcp_project,
            database=self._settings.firestore_database,
        )
        return self._client

    async def upsert_user(self, user: User) -> None:
        db = self._db()
        await db.collection("users").document(user.user_id).set(
            user.model_dump(mode="json"), merge=True
        )
        if user.google_sub:
            await db.collection("google_subs").document(user.google_sub).set(
                {"user_id": user.user_id}, merge=True
            )

    async def get_user(self, user_id: str) -> User | None:
        snap = await self._db().collection("users").document(user_id).get()
        if not snap.exists:
            return None
        return User.model_validate(snap.to_dict() or {}, strict=False)

    async def get_user_by_google_sub(self, google_sub: str) -> User | None:
        snap = await self._db().collection("google_subs").document(google_sub).get()
        if not snap.exists:
            return None
        uid = (snap.to_dict() or {}).get("user_id")
        if not uid:
            return None
        return await self.get_user(uid)

    async def upsert_token(self, token: OAuthToken) -> None:
        await (
            self._db()
            .collection("users")
            .document(token.user_id)
            .collection("secrets")
            .document("google_oauth")
            .set(token.model_dump(mode="json"), merge=True)
        )

    async def get_google_token(self, user_id: str) -> OAuthToken | None:
        snap = await (
            self._db()
            .collection("users")
            .document(user_id)
            .collection("secrets")
            .document("google_oauth")
            .get()
        )
        if not snap.exists:
            return None
        return OAuthToken.model_validate(snap.to_dict() or {}, strict=False)

    async def delete_google_token(self, user_id: str) -> bool:
        ref = (
            self._db()
            .collection("users")
            .document(user_id)
            .collection("secrets")
            .document("google_oauth")
        )
        snap = await ref.get()
        if not snap.exists:
            return False
        await ref.delete()
        return True


_LOCAL_STORE: LocalFileTokenStore | InMemoryTokenStore | None = None


def _local_store_path() -> Path:
    # packages/core/src/level_core/auth/tokens.py → repo root = parents[5]
    # Prefer an absolute path so cwd changes / reload workers can't fork the store.
    repo_root = Path(__file__).resolve().parents[5]
    return repo_root / ".level" / "oauth_store.json"


def build_token_store(settings: Settings | None = None) -> TokenStore:
    settings = settings or get_settings()
    if settings.is_local:
        global _LOCAL_STORE
        if _LOCAL_STORE is None:
            _LOCAL_STORE = LocalFileTokenStore(_local_store_path())
        return _LOCAL_STORE
    return FirestoreTokenStore(settings=settings)


__all__ = [
    "FirestoreTokenStore",
    "InMemoryTokenStore",
    "LocalFileTokenStore",
    "TokenStore",
    "build_token_store",
]
