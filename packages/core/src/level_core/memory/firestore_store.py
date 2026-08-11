"""Firestore-backed implementations of the Memory Bank repositories.

Imports are lazy inside methods so importing this module does not require
``google-cloud-firestore`` to be installed in environments that only run
against fakes (e.g. many CI setups).

Schema follows the layout documented in ARCHITECTURE.md:

    users/{user_id}/signals/{signal_id}
    users/{user_id}/facts/{fact_id}
    users/{user_id}/decisions/{decision_id}
    users/{user_id}/decisions/{decision_id}/turns/{turn_id}
    users/{user_id}/bias_events/{event_id}
    users/{user_id}/manifesto
    users/{user_id}/bias_profile
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from level_core.config import Settings, get_settings
from level_core.errors import NotFound
from level_core.observability.tracer import traced
from level_core.schemas.bias import BiasEvent, BiasProfile, Manifesto
from level_core.schemas.care import CareProfile
from level_core.schemas.decision import Decision
from level_core.schemas.profile import ProfileSnapshot
from level_core.schemas.signal import Fact, Signal
from level_core.schemas.turn import Turn

if TYPE_CHECKING:
    from google.cloud.firestore_v1 import AsyncClient


def _client(settings: Settings | None = None) -> AsyncClient:
    from google.cloud.firestore_v1 import AsyncClient as _AsyncClient

    settings = settings or get_settings()
    return _AsyncClient(project=settings.gcp_project, database=settings.firestore_database)


def _user_ref(client: AsyncClient, user_id: str) -> Any:
    return client.collection("users").document(user_id)


def _to_dict(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _from_dict(model_cls: type[Any], data: dict[str, Any] | None) -> Any:
    """Parse Firestore JSON (ISO datetimes, enum strings) into LevelModels."""
    return model_cls.model_validate(data or {}, strict=False)


class FirestoreSignalRepository:
    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client: AsyncClient = client or _client()

    @traced("firestore.signal.upsert")
    async def upsert(self, signal: Signal) -> None:
        ref = _user_ref(self._client, signal.user_id).collection("signals").document(signal.signal_id)
        await ref.set(_to_dict(signal), merge=True)

    @traced("firestore.signal.get")
    async def get(self, *, user_id: str, signal_id: str) -> Signal:
        ref = _user_ref(self._client, user_id).collection("signals").document(signal_id)
        snap = await ref.get()
        if not snap.exists:
            raise NotFound("signals", signal_id)
        return _from_dict(Signal, snap.to_dict())

    @traced("firestore.signal.list_by_source")
    async def list_by_source(
        self, *, user_id: str, source: str, since_cursor: str | None = None  # noqa: ARG002
    ) -> list[Signal]:
        col = _user_ref(self._client, user_id).collection("signals")
        query = col.where("source", "==", source)
        docs = query.stream()
        return [_from_dict(Signal, doc.to_dict()) async for doc in docs]


class FirestoreFactRepository:
    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client: AsyncClient = client or _client()

    @traced("firestore.fact.upsert")
    async def upsert(self, fact: Fact) -> None:
        ref = _user_ref(self._client, fact.user_id).collection("facts").document(fact.fact_id)
        await ref.set(_to_dict(fact), merge=True)

    @traced("firestore.fact.get")
    async def get(self, *, user_id: str, fact_id: str) -> Fact:
        ref = _user_ref(self._client, user_id).collection("facts").document(fact_id)
        snap = await ref.get()
        if not snap.exists:
            raise NotFound("facts", fact_id)
        return _from_dict(Fact, snap.to_dict())

    @traced("firestore.fact.get_many")
    async def get_many(self, *, user_id: str, fact_ids: Iterable[str]) -> list[Fact]:
        col = _user_ref(self._client, user_id).collection("facts")
        ids = list(fact_ids)
        if not ids:
            return []
        refs = [col.document(fid) for fid in ids]
        snaps = await self._client.get_all(refs)
        return [_from_dict(Fact, s.to_dict()) for s in snaps if s.exists]

    @traced("firestore.fact.list_for_user")
    async def list_for_user(self, *, user_id: str, limit: int = 100) -> list[Fact]:
        col = _user_ref(self._client, user_id).collection("facts").limit(limit)
        out: list[Fact] = []
        async for doc in col.stream():
            try:
                out.append(_from_dict(Fact, doc.to_dict()))
            except Exception:  # noqa: BLE001
                continue
        return out

    @traced("firestore.fact.delete")
    async def delete(self, *, user_id: str, fact_id: str) -> None:
        ref = _user_ref(self._client, user_id).collection("facts").document(fact_id)
        await ref.delete()


class FirestoreDecisionRepository:
    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client: AsyncClient = client or _client()

    def _decision_ref(self, decision: Decision) -> Any:
        return (
            _user_ref(self._client, decision.user_id)
            .collection("decisions")
            .document(decision.decision_id)
        )

    @traced("firestore.decision.create")
    async def create(self, decision: Decision) -> None:
        await self._decision_ref(decision).set(_to_dict(decision))

    @traced("firestore.decision.get")
    async def get(self, *, user_id: str, decision_id: str) -> Decision:
        ref = _user_ref(self._client, user_id).collection("decisions").document(decision_id)
        snap = await ref.get()
        if not snap.exists:
            raise NotFound("decisions", decision_id)
        return _from_dict(Decision, snap.to_dict())

    @traced("firestore.decision.update")
    async def update(self, decision: Decision) -> None:
        decision.touch()
        await self._decision_ref(decision).set(_to_dict(decision), merge=True)

    @traced("firestore.decision.append_turn")
    async def append_turn(self, turn: Turn) -> None:
        ref = (
            _user_ref(self._client, turn.user_id)
            .collection("decisions")
            .document(turn.decision_id)
            .collection("turns")
            .document(turn.turn_id)
        )
        await ref.set(_to_dict(turn))

    @traced("firestore.decision.list_turns")
    async def list_turns(self, *, user_id: str, decision_id: str) -> list[Turn]:
        col = (
            _user_ref(self._client, user_id)
            .collection("decisions")
            .document(decision_id)
            .collection("turns")
        )
        return [_from_dict(Turn, doc.to_dict()) async for doc in col.stream()]

    @traced("firestore.decision.list_for_user")
    async def list_for_user(self, *, user_id: str, limit: int = 50) -> list[Decision]:
        col = _user_ref(self._client, user_id).collection("decisions").limit(limit)
        return [_from_dict(Decision, doc.to_dict()) async for doc in col.stream()]


class FirestoreTurnRepository:
    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client: AsyncClient = client or _client()

    @traced("firestore.bias_event.append")
    async def append_bias_event(self, event: BiasEvent) -> None:
        ref = (
            _user_ref(self._client, event.user_id)
            .collection("bias_events")
            .document(event.event_id)
        )
        await ref.set(_to_dict(event))

    @traced("firestore.bias_event.list")
    async def list_bias_events_for_user(
        self, *, user_id: str, limit: int = 500
    ) -> list[BiasEvent]:
        col = _user_ref(self._client, user_id).collection("bias_events").limit(limit)
        return [_from_dict(BiasEvent, doc.to_dict()) async for doc in col.stream()]


class FirestoreManifestoRepository:
    def __init__(self, client: AsyncClient | None = None) -> None:
        self._client: AsyncClient = client or _client()

    @traced("firestore.manifesto.get")
    async def get_current_manifesto(self, *, user_id: str) -> Manifesto | None:
        ref = _user_ref(self._client, user_id).collection("state").document("manifesto")
        snap = await ref.get()
        if not snap.exists:
            return None
        return _from_dict(Manifesto, snap.to_dict())

    @traced("firestore.manifesto.save")
    async def save_manifesto(self, manifesto: Manifesto) -> None:
        ref = (
            _user_ref(self._client, manifesto.user_id)
            .collection("state")
            .document("manifesto")
        )
        history_ref = (
            _user_ref(self._client, manifesto.user_id)
            .collection("manifesto_versions")
            .document(str(manifesto.version))
        )
        await asyncio.gather(
            ref.set(_to_dict(manifesto)),
            history_ref.set(_to_dict(manifesto)),
        )

    @traced("firestore.bias_profile.get")
    async def get_bias_profile(self, *, user_id: str) -> BiasProfile | None:
        ref = _user_ref(self._client, user_id).collection("state").document("bias_profile")
        snap = await ref.get()
        if not snap.exists:
            return None
        return _from_dict(BiasProfile, snap.to_dict())

    @traced("firestore.bias_profile.save")
    async def save_bias_profile(self, profile: BiasProfile) -> None:
        ref = (
            _user_ref(self._client, profile.user_id)
            .collection("state")
            .document("bias_profile")
        )
        await ref.set(_to_dict(profile))

    @traced("firestore.profile_snapshot.get")
    async def get_profile_snapshot(self, *, user_id: str) -> ProfileSnapshot | None:
        ref = _user_ref(self._client, user_id).collection("state").document("profile_snapshot")
        snap = await ref.get()
        if not snap.exists:
            return None
        return _from_dict(ProfileSnapshot, snap.to_dict())

    @traced("firestore.profile_snapshot.save")
    async def save_profile_snapshot(self, snapshot: ProfileSnapshot) -> None:
        ref = (
            _user_ref(self._client, snapshot.user_id)
            .collection("state")
            .document("profile_snapshot")
        )
        await ref.set(_to_dict(snapshot))

    @traced("firestore.care_profile.get")
    async def get_care_profile(self, *, user_id: str) -> CareProfile | None:
        ref = _user_ref(self._client, user_id).collection("state").document("care_profile")
        snap = await ref.get()
        if not snap.exists:
            return None
        return _from_dict(CareProfile, snap.to_dict())

    @traced("firestore.care_profile.save")
    async def save_care_profile(self, profile: CareProfile) -> None:
        ref = (
            _user_ref(self._client, profile.user_id)
            .collection("state")
            .document("care_profile")
        )
        await ref.set(_to_dict(profile))


__all__ = [
    "FirestoreDecisionRepository",
    "FirestoreFactRepository",
    "FirestoreManifestoRepository",
    "FirestoreSignalRepository",
    "FirestoreTurnRepository",
]
