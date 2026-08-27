"""Router cache: normalize + TTL + LRU + skip-clarify semantics."""

from __future__ import annotations

import time

from level_core.agents.router_cache import (
    _RouterCache,
    cache_stats,
    clear_cache,
    get_cached,
    make_key,
    store_cached,
)
from level_core.schemas import ChatRouterDecision, ChatRouterIntent, ChatRouterPath


def _decision(
    *,
    path: ChatRouterPath = ChatRouterPath.GENERAL,
    intent: ChatRouterIntent = ChatRouterIntent.ASK,
    confidence: float = 0.9,
    needs_clarification: bool = False,
    source_span: str = "hi",
) -> ChatRouterDecision:
    return ChatRouterDecision(
        path=path,
        intent=intent,
        source_span=source_span,
        confidence=confidence,
        needs_clarification=needs_clarification,
    )


def test_normalize_equates_case_and_trailing_punctuation() -> None:
    clear_cache()
    d = _decision()
    store_cached(user_id="u1", message="How are you?", history=None, value=d)
    hit = get_cached(user_id="u1", message="how are you", history=None)
    assert hit is not None
    assert hit.path == d.path


def test_different_users_are_isolated() -> None:
    clear_cache()
    store_cached(user_id="u1", message="hi", history=None, value=_decision())
    assert get_cached(user_id="u2", message="hi", history=None) is None


def test_history_digest_disambiguates_follow_ups() -> None:
    clear_cache()
    d_book = _decision(
        path=ChatRouterPath.SCHEDULE,
        intent=ChatRouterIntent.BOOK_NOW,
        source_span="book that",
    )
    d_email = _decision(
        path=ChatRouterPath.EMAIL,
        intent=ChatRouterIntent.SEND_EMAIL,
        source_span="book that",
    )
    hist_book = [{"role": "assistant", "text": "want me to book that Tuesday 2pm?"}]
    hist_email = [{"role": "assistant", "text": "which teacher should I email?"}]
    store_cached(
        user_id="u", message="book that", history=hist_book, value=d_book
    )
    store_cached(
        user_id="u", message="book that", history=hist_email, value=d_email
    )
    assert (
        get_cached(user_id="u", message="book that", history=hist_book).path  # type: ignore[union-attr]
        == ChatRouterPath.SCHEDULE
    )
    assert (
        get_cached(user_id="u", message="book that", history=hist_email).path  # type: ignore[union-attr]
        == ChatRouterPath.EMAIL
    )


def test_clarifications_and_low_confidence_are_not_cached() -> None:
    clear_cache()
    store_cached(
        user_id="u",
        message="book something",
        history=None,
        value=_decision(needs_clarification=True),
    )
    store_cached(
        user_id="u",
        message="book something",
        history=None,
        value=_decision(confidence=0.3),
    )
    assert get_cached(user_id="u", message="book something", history=None) is None


def test_ttl_expires_stale_entries() -> None:
    cache = _RouterCache(max_entries=10, ttl_s=1)
    key = make_key(user_id="u", message="hi", history=None)
    cache.set(key, _decision())
    assert cache.get(key) is not None
    time.sleep(1.05)
    assert cache.get(key) is None


def test_lru_evicts_oldest_when_full() -> None:
    cache = _RouterCache(max_entries=2, ttl_s=60)
    k1 = make_key(user_id="u", message="one", history=None)
    k2 = make_key(user_id="u", message="two", history=None)
    k3 = make_key(user_id="u", message="three", history=None)
    cache.set(k1, _decision(source_span="one"))
    cache.set(k2, _decision(source_span="two"))
    _ = cache.get(k1)
    cache.set(k3, _decision(source_span="three"))
    assert cache.get(k2) is None
    assert cache.get(k1) is not None
    assert cache.get(k3) is not None


def test_stats_reports_hit_and_miss_counters() -> None:
    clear_cache()
    stats0 = cache_stats()
    assert stats0["hits"] == 0
    store_cached(user_id="u", message="hi", history=None, value=_decision())
    _ = get_cached(user_id="u", message="hi", history=None)
    _ = get_cached(user_id="u", message="hi", history=None)
    _ = get_cached(user_id="u", message="miss", history=None)
    stats1 = cache_stats()
    assert stats1["hits"] == 2
    assert stats1["misses"] == 1
