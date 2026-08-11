"""Persist pending calendar commitment proposals (local JSON / Firestore)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from level_core.config import Settings, get_settings
from level_core.schemas.commitment import CommitmentProposal


class ProposalStore(Protocol):
    async def save(self, proposal: CommitmentProposal) -> None: ...
    async def get(self, proposal_id: str) -> CommitmentProposal | None: ...
    async def list_for_user(self, user_id: str, *, limit: int = 20) -> list[CommitmentProposal]: ...


class InMemoryProposalStore:
    def __init__(self) -> None:
        self._items: dict[str, CommitmentProposal] = {}

    async def save(self, proposal: CommitmentProposal) -> None:
        self._items[proposal.proposal_id] = proposal

    async def get(self, proposal_id: str) -> CommitmentProposal | None:
        return self._items.get(proposal_id)

    async def list_for_user(self, user_id: str, *, limit: int = 20) -> list[CommitmentProposal]:
        rows = [p for p in self._items.values() if p.user_id == user_id]
        rows.sort(key=lambda p: p.created_at, reverse=True)
        return rows[:limit]


class LocalFileProposalStore(InMemoryProposalStore):
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
        for row in raw.get("proposals") or []:
            try:
                prop = CommitmentProposal(**row)
            except Exception:  # noqa: BLE001
                continue
            self._items[prop.proposal_id] = prop

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "proposals": [p.model_dump(mode="json") for p in self._items.values()],
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    async def save(self, proposal: CommitmentProposal) -> None:
        await super().save(proposal)
        self._save()


class FirestoreProposalStore:
    """Cloud proposals: users/{uid}/proposals/{id} + top-level index for get-by-id."""

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

    async def save(self, proposal: CommitmentProposal) -> None:
        db = self._db()
        payload = proposal.model_dump(mode="json")
        await (
            db.collection("users")
            .document(proposal.user_id)
            .collection("proposals")
            .document(proposal.proposal_id)
            .set(payload, merge=True)
        )
        await db.collection("commitment_proposals").document(proposal.proposal_id).set(
            payload,
            merge=True,
        )

    async def get(self, proposal_id: str) -> CommitmentProposal | None:
        snap = await self._db().collection("commitment_proposals").document(proposal_id).get()
        if not snap.exists:
            return None
        return CommitmentProposal.model_validate(snap.to_dict() or {}, strict=False)

    async def list_for_user(self, user_id: str, *, limit: int = 20) -> list[CommitmentProposal]:
        query = (
            self._db()
            .collection("users")
            .document(user_id)
            .collection("proposals")
            .limit(max(1, min(limit, 50)))
        )
        rows: list[CommitmentProposal] = []
        async for snap in query.stream():
            try:
                rows.append(
                    CommitmentProposal.model_validate(snap.to_dict() or {}, strict=False)
                )
            except Exception:  # noqa: BLE001
                continue
        rows.sort(key=lambda p: p.created_at, reverse=True)
        return rows[:limit]


_STORE: ProposalStore | None = None


def build_proposal_store(settings: Settings | None = None) -> ProposalStore:
    global _STORE
    if _STORE is not None:
        return _STORE
    settings = settings or get_settings()
    if settings.is_local:
        # Absolute path under repo so cwd / reload workers share one file.
        repo_root = Path(__file__).resolve().parents[5]
        path = repo_root / ".level" / "commitment_proposals.json"
        _STORE = LocalFileProposalStore(path)
    else:
        _STORE = FirestoreProposalStore(settings=settings)
    return _STORE


__all__ = [
    "FirestoreProposalStore",
    "InMemoryProposalStore",
    "LocalFileProposalStore",
    "ProposalStore",
    "build_proposal_store",
]
