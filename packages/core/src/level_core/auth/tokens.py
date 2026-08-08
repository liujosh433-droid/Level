"""OAuth token persistence (local JSON file, Firestore for cloud)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from level_core.config import Settings, get_settings
from level_core.schemas.user import OAuthToken, User


class TokenStore(Protocol):
    async def upsert_user(self, user: User) -> None: ...
    async def get_user(self, user_id: str) -> User | None: ...
    async def get_user_by_google_sub(self, google_sub: str) -> User | None: ...
    async def upsert_token(self, token: OAuthToken) -> None: ...
    async def get_google_token(self, user_id: str) -> OAuthToken | None: ...


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


class LocalFileTokenStore(InMemoryTokenStore):
    """In-memory store that also persists to disk so uvicorn reload keeps OAuth."""

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
        for u in raw.get("users") or []:
            try:
                user = User(**u)
            except Exception:  # noqa: BLE001
                continue
            self._users[user.user_id] = user
            if user.google_sub:
                self._by_sub[user.google_sub] = user.user_id
        for t in raw.get("tokens") or []:
            try:
                token = OAuthToken(**t)
            except Exception:  # noqa: BLE001
                continue
            self._tokens[token.user_id] = token

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
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
        return User(**snap.to_dict())

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
        return OAuthToken(**snap.to_dict())


_LOCAL_STORE: LocalFileTokenStore | InMemoryTokenStore | None = None


def _local_store_path() -> Path:
    # packages/core/src/level_core/auth/tokens.py → repo root = parents[5]
    # Prefer CWD (usually repo root when running make api).
    cwd = Path.cwd() / ".level" / "oauth_store.json"
    return cwd


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
