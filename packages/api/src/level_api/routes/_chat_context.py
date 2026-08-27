"""Per-request context for one chat turn.

Every chat handler used to independently call `tz_for_store(store)`,
`store.agenda.list()`, `store.people.list()`, `store.priorities.list()`,
etc. In cloud mode those are Firestore round-trips and each turn was
re-issuing 2-3x more reads than necessary.

`ChatContext` builds once per request and hands memoized async
accessors to every downstream fast-path or handler. First read pays
the Firestore cost, subsequent reads are in-memory.

The accessors are *lazy* on purpose — a "hi" chit-chat turn never
touches Firestore, so lazy loading keeps that path at ~10ms.

Context propagation uses a ContextVar so downstream handlers can
`get_chat_ctx()` without every function signature growing a `ctx`
parameter. `bind_chat_ctx()` is the top-of-request setup; the token
is released via the returned context manager.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from level_core.schemas import CachedEvent
from level_core.schemas.care import CarePerson
from level_core.storage.base import UserStore
from level_core.tz import tz_for_store


@dataclass
class ChatContext:
    """One-turn view over the caregiver's data, memoized.

    Instantiate at the top of `_dispatch_message` and pass to every
    fast-path / handler that needs shared state.
    """

    store: UserStore
    message: str
    history: list[dict[str, str]]

    _tz: ZoneInfo | None = field(default=None, init=False, repr=False)
    _people: list[CarePerson] | None = field(default=None, init=False, repr=False)
    _contacts: list[Any] | None = field(default=None, init=False, repr=False)
    _agenda: list[CachedEvent] | None = field(default=None, init=False, repr=False)
    _priorities: list[Any] | None = field(default=None, init=False, repr=False)
    _usuals: list[Any] | None = field(default=None, init=False, repr=False)
    _profile: dict[str, Any] | None = field(default=None, init=False, repr=False)

    async def tz(self) -> ZoneInfo:
        if self._tz is None:
            self._tz = await tz_for_store(self.store)
        return self._tz

    async def people(self) -> list[CarePerson]:
        if self._people is None:
            self._people = list(await self.store.people.list())
        return self._people

    async def contacts(self) -> list[Any]:
        if self._contacts is None:
            self._contacts = list(await self.store.contacts.list())
        return self._contacts

    async def agenda(self) -> list[CachedEvent]:
        if self._agenda is None:
            self._agenda = list(await self.store.agenda.list())
        return self._agenda

    async def priorities(self) -> list[Any]:
        if self._priorities is None:
            self._priorities = list(await self.store.priorities.list())
        return self._priorities

    async def usuals(self) -> list[Any]:
        if self._usuals is None:
            self._usuals = list(await self.store.usuals.list())
        return self._usuals

    async def profile(self) -> dict[str, Any]:
        if self._profile is None:
            self._profile = await self.store.profile.read() or {}
        return self._profile

    def invalidate_agenda(self) -> None:
        """Handler mutated the agenda (create/move/delete). Force reload."""
        self._agenda = None

    def invalidate_people(self) -> None:
        self._people = None

    def invalidate_priorities(self) -> None:
        self._priorities = None


_current_ctx: ContextVar[ChatContext | None] = ContextVar(
    "_chat_ctx", default=None
)


@contextmanager
def bind_chat_ctx(ctx: ChatContext) -> Iterator[ChatContext]:
    """Set `ctx` as the active chat context for the duration of a request.

    Usage:
        with bind_chat_ctx(ChatContext(...)) as ctx:
            return await _dispatch_message(store, message, history)
    """
    token: Token[ChatContext | None] = _current_ctx.set(ctx)
    try:
        yield ctx
    finally:
        _current_ctx.reset(token)


def get_chat_ctx() -> ChatContext | None:
    """Return the active chat context, or None outside a chat request.

    Handlers should tolerate None (background jobs, tests) and fall
    back to direct store reads.
    """
    return _current_ctx.get()


async def ctx_agenda(store: UserStore) -> list[CachedEvent]:
    """Memoized `store.agenda.list()` when inside a chat request.

    Falls through to a direct read outside of one, so the same helper
    works for background jobs and tests without extra branches.
    """
    ctx = get_chat_ctx()
    if ctx is not None and ctx.store is store:
        return await ctx.agenda()
    return list(await store.agenda.list())


async def ctx_people(store: UserStore) -> list[CarePerson]:
    ctx = get_chat_ctx()
    if ctx is not None and ctx.store is store:
        return await ctx.people()
    return list(await store.people.list())


async def ctx_contacts(store: UserStore) -> list[Any]:
    ctx = get_chat_ctx()
    if ctx is not None and ctx.store is store:
        return await ctx.contacts()
    return list(await store.contacts.list())


async def ctx_priorities(store: UserStore) -> list[Any]:
    ctx = get_chat_ctx()
    if ctx is not None and ctx.store is store:
        return await ctx.priorities()
    return list(await store.priorities.list())


async def ctx_usuals(store: UserStore) -> list[Any]:
    ctx = get_chat_ctx()
    if ctx is not None and ctx.store is store:
        return await ctx.usuals()
    return list(await store.usuals.list())


async def ctx_tz(store: UserStore) -> ZoneInfo:
    ctx = get_chat_ctx()
    if ctx is not None and ctx.store is store:
        return await ctx.tz()
    return await tz_for_store(store)
