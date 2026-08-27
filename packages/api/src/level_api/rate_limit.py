"""In-process token-bucket rate limiter, keyed by user_id.

Sits on top of the LLM gate (which caps *model* calls) with a
different responsibility: it caps *HTTP requests* to /v1/chat so a
single user (or a compromised token) can't hammer the fast-paths and
Firestore reads even when the model isn't invoked.

Design:
- Token bucket per user_id. Capacity = burst budget, refill = steady
  rate. Sliding-window in effect.
- Process-local. On multi-instance deploys each replica has its own
  bucket, so effective ceiling is `capacity * n_replicas`. That's fine
  for a hackathon; Redis would be the next step.
- Refuses over-limit with 429 + Retry-After. Never blocks.
- Explicit `check_rate_limit(user_id)` -> `RateLimitDecision` so
  handlers can also degrade gracefully (return "slow down" reply)
  instead of always 429'ing when we want a softer touch.

Config lives in `Settings` so the limits can be tuned without a
redeploy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from level_core.config import get_settings
from level_core.observability import get_logger

logger = get_logger(__name__)


@dataclass
class RateLimitDecision:
    allowed: bool
    remaining: float
    retry_after_s: float


class _Bucket:
    __slots__ = ("tokens", "last_refill")

    def __init__(self, initial: float) -> None:
        self.tokens = initial
        self.last_refill = time.monotonic()


class TokenBucketLimiter:
    """Per-key token bucket. Thread-safe enough for single-worker Cloud Run.

    We don't lock: worst case is two concurrent asyncio tasks both
    read the same bucket and both allow. In a chat endpoint that's a
    non-issue - the extra call is one turn of slop, not a bypass.
    """

    def __init__(self, *, capacity: int, refill_per_second: float) -> None:
        self._capacity = float(capacity)
        self._rate = float(refill_per_second)
        self._buckets: dict[str, _Bucket] = {}

    def _refill(self, bucket: _Bucket, now: float) -> None:
        elapsed = now - bucket.last_refill
        if elapsed <= 0:
            return
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._rate)
        bucket.last_refill = now

    def check(self, key: str) -> RateLimitDecision:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(self._capacity)
            self._buckets[key] = bucket
        self._refill(bucket, now)
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return RateLimitDecision(
                allowed=True, remaining=bucket.tokens, retry_after_s=0.0
            )
        # Time until 1 whole token is available again.
        deficit = 1 - bucket.tokens
        retry_after = deficit / self._rate if self._rate > 0 else 60.0
        return RateLimitDecision(
            allowed=False, remaining=bucket.tokens, retry_after_s=retry_after
        )

    def stats(self) -> dict[str, Any]:
        return {
            "capacity": self._capacity,
            "refill_per_second": self._rate,
            "active_keys": len(self._buckets),
        }


_chat_limiter: TokenBucketLimiter | None = None


def get_chat_limiter() -> TokenBucketLimiter:
    global _chat_limiter
    if _chat_limiter is None:
        settings = get_settings()
        capacity = int(getattr(settings, "level_chat_rate_burst", 20))
        per_min = int(getattr(settings, "level_chat_rate_per_min", 30))
        _chat_limiter = TokenBucketLimiter(
            capacity=capacity, refill_per_second=per_min / 60.0
        )
    return _chat_limiter


def check_chat_rate_limit(user_id: str) -> RateLimitDecision:
    """Public entrypoint for the chat rate check.

    Handlers can call this to get a decision object (so they choose
    to soft-degrade); the FastAPI dependency `chat_rate_limit_gate`
    below turns a rejection into a 429 automatically.
    """
    if not user_id:
        return RateLimitDecision(allowed=True, remaining=1.0, retry_after_s=0.0)
    return get_chat_limiter().check(user_id)


def chat_rate_limit_gate(user_id: str) -> None:
    """Raise 429 if the given user is over budget.

    Use as a shared helper inside the chat route (not as a Depends,
    because we need the store first to resolve `user_id`).
    """
    decision = check_chat_rate_limit(user_id)
    if decision.allowed:
        return
    logger.warning(
        "chat.rate_limited",
        user=user_id,
        retry_after_s=round(decision.retry_after_s, 2),
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": "rate_limited",
            "retry_after_s": round(decision.retry_after_s, 1),
            "message": (
                "You\u2019re sending messages faster than I can safely process. "
                "Take a breath and try again in a moment."
            ),
        },
        headers={"Retry-After": str(int(decision.retry_after_s) + 1)},
    )


def rate_limit_stats() -> dict[str, Any]:
    """Snapshot for /v1/admin/rate_limit."""
    return get_chat_limiter().stats()


def reset_limiter() -> None:
    """Test-only helper - rebuild the singleton."""
    global _chat_limiter
    _chat_limiter = None
