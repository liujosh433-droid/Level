"""In-process LRU cache for `ChatRouterAgent` decisions.

The router is the LLM call that MUST happen on every chat turn not
caught by a deterministic fast-path. In practice a huge share of that
traffic is repetitive: greetings, "what's on today", "book Tuesday
2pm dentist", "help", "who is Nova's teacher". Caching the router's
classification for those messages saves a Gemini call per repeat and
smooths out latency during quota pressure.

Cache design:
- Keyed on `(user_id, normalized(message), sha1(recent_history))`.
  Same user + same normalized text + same nearby context -> same
  route. Different users get isolated caches (privacy + roster diffs
  change routing).
- TTL: 15 minutes. Long enough to absorb "book Tuesday 2pm dentist"
  followed by an "add reminder" follow-up; short enough that a user
  editing their roster sees fresh decisions within a coffee break.
- Bounded LRU: max 2000 entries process-wide. On overflow the least
  recently *touched* entry is evicted.
- Skips cache for messages with pending confirmations upstream (the
  caller decides) and for anything that requested a clarification
  (writing a clarification back to cache would trap the user in a
  loop when they answer).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from level_core.schemas import ChatRouterDecision

_DEFAULT_TTL_S = 15 * 60
_DEFAULT_MAX_ENTRIES = 2000


@dataclass(frozen=True)
class CacheKey:
    user_id: str
    normalized_message: str
    history_digest: str


@dataclass
class _Entry:
    value: ChatRouterDecision
    expires_at: float


_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[?!.,;:]+$")


def _normalize(message: str) -> str:
    """Lowercase, strip trailing punctuation, collapse whitespace.

    Keeps the SEMANTICS - we don't strip content words, just noise.
    "How are you?" and "how are you" cache to the same slot;
    "book Tuesday 2pm" and "book tuesday 2pm" also.
    """
    text = (message or "").strip().lower()
    text = _WHITESPACE.sub(" ", text)
    text = _PUNCT.sub("", text)
    return text


def _digest_history(history: list[dict[str, str]] | None) -> str:
    """Hash the last 3 turns so short follow-ups get a distinct key.

    "book that" after "email Nova's teacher" is a different classification
    than "book that" after "I want lunch this week". Truncate each turn
    to 200 chars so long transcripts don't inflate the key.
    """
    if not history:
        return "0"
    tail = history[-3:]
    joined = "|".join(
        f"{(t.get('role') or '')[:1]}:{(t.get('text') or '')[:200]}"
        for t in tail
    )
    return hashlib.sha1(joined.encode()).hexdigest()[:12]


class _RouterCache:
    """Process-local LRU with monotonic TTL."""

    def __init__(
        self, *, max_entries: int = _DEFAULT_MAX_ENTRIES, ttl_s: int = _DEFAULT_TTL_S
    ) -> None:
        self._store: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_s
        self.hits = 0
        self.misses = 0

    def get(self, key: CacheKey) -> ChatRouterDecision | None:
        entry = self._store.get(key)
        now = time.monotonic()
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= now:
            del self._store[key]
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        return entry.value

    def set(self, key: CacheKey, value: ChatRouterDecision) -> None:
        # Skip caching pathological entries: clarifications would trap
        # the user in a loop (they answer "3pm", we route to the same
        # clarification), and low-confidence decisions shouldn't
        # cement.
        if getattr(value, "needs_clarification", False):
            return
        try:
            confidence = float(getattr(value, "confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.5:
            return
        self._store[key] = _Entry(
            value=value,
            expires_at=time.monotonic() + self._ttl,
        )
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        hit_rate = (self.hits / total) if total else 0.0
        return {
            "entries": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(hit_rate, 4),
            "max_entries": self._max,
            "ttl_s": self._ttl,
        }

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0


_cache = _RouterCache()


def make_key(
    *, user_id: str, message: str, history: list[dict[str, str]] | None
) -> CacheKey:
    return CacheKey(
        user_id=user_id or "anon",
        normalized_message=_normalize(message),
        history_digest=_digest_history(history),
    )


def get_cached(
    *, user_id: str, message: str, history: list[dict[str, str]] | None
) -> ChatRouterDecision | None:
    """Return a cached router decision or None if expired / missing."""
    return _cache.get(
        make_key(user_id=user_id, message=message, history=history)
    )


def store_cached(
    *,
    user_id: str,
    message: str,
    history: list[dict[str, str]] | None,
    value: ChatRouterDecision,
) -> None:
    _cache.set(
        make_key(user_id=user_id, message=message, history=history), value
    )


def cache_stats() -> dict[str, Any]:
    """Snapshot for /v1/admin/router_cache. Cheap to call."""
    return _cache.stats()


def clear_cache() -> None:
    """Test-only helper — production code should let TTL expire."""
    _cache.clear()
