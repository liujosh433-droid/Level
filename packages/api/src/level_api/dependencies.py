"""FastAPI dependencies.

Wires the request-scoped objects (Conductor, MemoryBank, Gateway, Registry).
Held in module-level singletons because the underlying resources (Firestore
async client, Vertex clients) are safe to share across requests and
expensive to construct per-request.
"""

from __future__ import annotations

from functools import lru_cache

from level_core.agents.conductor import Conductor, build_conductor
from level_core.agents.registry import AgentRegistry, build_registry
from level_core.auth.tokens import TokenStore, build_token_store
from level_core.calendar.event_cues import EventCueStore, build_event_cue_store
from level_core.calendar.proposals import ProposalStore, build_proposal_store
from level_core.calendar.sync_state import CalendarSyncStore, build_calendar_sync_store
from level_core.config import Settings, get_settings
from level_core.gateway.router import AgentGateway
from level_core.guardrails.outbound import OutboundGuardrail
from level_core.memory.base import MemoryBank
from level_core.memory.factory import build_memory
from level_core.models.factory import build_embedding_client, build_gemini_client


@lru_cache(maxsize=1)
def cached_settings() -> Settings:
    return get_settings()


@lru_cache(maxsize=1)
def cached_memory() -> MemoryBank:
    return build_memory(cached_settings())


@lru_cache(maxsize=1)
def cached_registry() -> AgentRegistry:
    return build_registry(cached_settings().is_local)


@lru_cache(maxsize=1)
def cached_gateway() -> AgentGateway:
    return AgentGateway()


@lru_cache(maxsize=1)
def cached_token_store() -> TokenStore:
    return build_token_store(cached_settings())


@lru_cache(maxsize=1)
def cached_proposal_store() -> ProposalStore:
    return build_proposal_store(cached_settings())


@lru_cache(maxsize=1)
def cached_event_cue_store() -> EventCueStore:
    return build_event_cue_store(cached_settings())


@lru_cache(maxsize=1)
def cached_calendar_sync_store() -> CalendarSyncStore:
    return build_calendar_sync_store(cached_settings())


@lru_cache(maxsize=1)
def cached_conductor() -> Conductor:
    settings = cached_settings()
    return build_conductor(
        memory=cached_memory(),
        gemini=build_gemini_client(settings),
        embedder=build_embedding_client(settings),
        guardrail=OutboundGuardrail(settings=settings),
        settings=settings,
    )


def get_conductor() -> Conductor:
    return cached_conductor()


def get_memory() -> MemoryBank:
    return cached_memory()


def get_registry() -> AgentRegistry:
    return cached_registry()


def get_gateway() -> AgentGateway:
    return cached_gateway()


def get_token_store() -> TokenStore:
    return cached_token_store()


def get_proposal_store() -> ProposalStore:
    return cached_proposal_store()


def get_event_cue_store() -> EventCueStore:
    return cached_event_cue_store()


def get_calendar_sync_store() -> CalendarSyncStore:
    return cached_calendar_sync_store()


__all__ = [
    "cached_calendar_sync_store",
    "cached_conductor",
    "cached_event_cue_store",
    "cached_gateway",
    "cached_memory",
    "cached_proposal_store",
    "cached_registry",
    "cached_settings",
    "cached_token_store",
    "get_calendar_sync_store",
    "get_conductor",
    "get_event_cue_store",
    "get_gateway",
    "get_memory",
    "get_proposal_store",
    "get_registry",
    "get_token_store",
]
