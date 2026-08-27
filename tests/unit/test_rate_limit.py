"""Token bucket + chat rate-limit gate: burst, refill, isolation, 429."""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from level_api.rate_limit import (
    TokenBucketLimiter,
    chat_rate_limit_gate,
    check_chat_rate_limit,
    reset_limiter,
)


def test_bucket_allows_up_to_capacity_then_denies() -> None:
    limiter = TokenBucketLimiter(capacity=3, refill_per_second=0.0)
    for _ in range(3):
        assert limiter.check("u1").allowed is True
    denied = limiter.check("u1")
    assert denied.allowed is False
    assert denied.retry_after_s > 0


def test_refill_restores_tokens_over_time() -> None:
    limiter = TokenBucketLimiter(capacity=2, refill_per_second=10.0)
    for _ in range(2):
        assert limiter.check("u1").allowed
    assert limiter.check("u1").allowed is False
    time.sleep(0.15)
    assert limiter.check("u1").allowed is True


def test_different_users_get_separate_buckets() -> None:
    limiter = TokenBucketLimiter(capacity=1, refill_per_second=0.0)
    assert limiter.check("u1").allowed
    assert limiter.check("u1").allowed is False
    assert limiter.check("u2").allowed is True


def test_empty_user_id_never_rate_limits() -> None:
    reset_limiter()
    decision = check_chat_rate_limit("")
    assert decision.allowed is True


def test_gate_raises_429_when_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from level_api import rate_limit as rl

    reset_limiter()
    monkeypatch.setattr(
        rl,
        "get_chat_limiter",
        lambda: TokenBucketLimiter(capacity=1, refill_per_second=0.0),
    )
    tight = TokenBucketLimiter(capacity=1, refill_per_second=0.0)
    monkeypatch.setattr(rl, "_chat_limiter", tight)
    monkeypatch.setattr(rl, "get_chat_limiter", lambda: tight)

    chat_rate_limit_gate("test_user")
    with pytest.raises(HTTPException) as exc:
        chat_rate_limit_gate("test_user")
    assert exc.value.status_code == 429
    assert "retry_after_s" in exc.value.detail
    reset_limiter()
