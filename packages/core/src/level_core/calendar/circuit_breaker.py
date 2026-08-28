"""Per-user circuit breaker for Google Calendar / Gmail API calls.

Motivation: Google's API returns 401 on stale tokens, 403 on scope
mismatches, 429 on quota, 5xx on their own outages. Without a breaker
we retry PER USER on every chat turn and every /profile/refresh,
which:
  - burns quota + surfaces slow paths (each attempt waits on network),
  - masks the real error behind the retry backoff, and
  - keeps trying to hit calendars we know are broken right now.

Design (three-state):
  CLOSED  -> calls flow. Failures accumulate in a rolling window.
  OPEN    -> calls short-circuit with CircuitOpen for `open_seconds`.
  HALF_OPEN -> one probe call is allowed; success closes, failure
             re-opens with a fresh timer.

State is per-user (user_id), process-local. On Cloud Run replicas each
instance has its own view - fine for the hackathon; Redis-backed
state would be the next step.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from level_core.observability import get_logger

logger = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """Raised (or returned) when the breaker is open for a user.

    Callers should present a "Google is having a moment" message and
    fall back to cached data instead of retrying.
    """

    def __init__(self, user_id: str, retry_in_s: float) -> None:
        self.user_id = user_id
        self.retry_in_s = retry_in_s
        super().__init__(
            f"google circuit open for user {user_id}, retry in {retry_in_s:.1f}s"
        )


@dataclass
class _UserCircuit:
    state: CircuitState = CircuitState.CLOSED
    failures: deque[float] = field(default_factory=deque)
    opened_at: float = 0.0
    last_touch: float = 0.0

    def _prune(self, now: float, window_s: float) -> None:
        while self.failures and self.failures[0] < now - window_s:
            self.failures.popleft()


class CircuitBreaker:
    """Sliding-window failure count -> open after threshold in window.

    Defaults tuned for a caregiver-scale workload: 5 failures in 60s
    opens the circuit for 30s. Half-open probe lets us reprobe on the
    next real call.

    Idle-user eviction: a closed circuit with zero recent failures and
    no touch in ``idle_ttl_seconds`` is dropped. Previously the users
    dict grew forever on long-running Cloud Run instances. Eviction is
    safe because a fresh CLOSED circuit is indistinguishable from a
    never-created one.
    """

    _DEFAULT_IDLE_TTL_S = 3600.0
    _MAX_EVICT_PER_TOUCH = 128

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_seconds: float = 60.0,
        open_seconds: float = 30.0,
        idle_ttl_seconds: float | None = None,
    ) -> None:
        self._threshold = failure_threshold
        self._window = window_seconds
        self._open_s = open_seconds
        self._idle_ttl = float(
            idle_ttl_seconds
            if idle_ttl_seconds is not None
            else self._DEFAULT_IDLE_TTL_S
        )
        self._users: dict[str, _UserCircuit] = {}

    def _get(self, user_id: str) -> _UserCircuit:
        now = time.monotonic()
        self._evict_idle(now)
        c = self._users.get(user_id)
        if c is None:
            c = _UserCircuit(last_touch=now)
            self._users[user_id] = c
        else:
            c.last_touch = now
        return c

    def _evict_idle(self, now: float) -> None:
        if not self._users:
            return
        cutoff = now - self._idle_ttl
        dropped = 0
        for uid, c in list(self._users.items()):
            if (
                c.state == CircuitState.CLOSED
                and not c.failures
                and c.last_touch < cutoff
            ):
                self._users.pop(uid, None)
                dropped += 1
                if dropped >= self._MAX_EVICT_PER_TOUCH:
                    break

    def state(self, user_id: str) -> CircuitState:
        circuit = self._get(user_id)
        now = time.monotonic()
        if circuit.state == CircuitState.OPEN and now - circuit.opened_at >= self._open_s:
            circuit.state = CircuitState.HALF_OPEN
            logger.info("calendar.circuit_half_open", user=user_id)
        return circuit.state

    def before_call(self, user_id: str) -> None:
        """Raise CircuitOpen if the breaker is open for this user.

        HALF_OPEN allows exactly one probe call through (idempotent —
        the state machine only records success/failure after the
        call returns).
        """
        state = self.state(user_id)
        if state == CircuitState.OPEN:
            circuit = self._get(user_id)
            remaining = max(0.0, self._open_s - (time.monotonic() - circuit.opened_at))
            raise CircuitOpen(user_id, remaining)

    def record_success(self, user_id: str) -> None:
        circuit = self._get(user_id)
        if circuit.state != CircuitState.CLOSED:
            logger.info("calendar.circuit_close", user=user_id, from_state=circuit.state.value)
        circuit.state = CircuitState.CLOSED
        circuit.failures.clear()
        circuit.opened_at = 0.0

    def record_failure(self, user_id: str, err: Exception | None = None) -> None:
        circuit = self._get(user_id)
        now = time.monotonic()
        circuit._prune(now, self._window)
        circuit.failures.append(now)
        if circuit.state == CircuitState.HALF_OPEN:
            circuit.state = CircuitState.OPEN
            circuit.opened_at = now
            logger.warning(
                "calendar.circuit_open_from_half",
                user=user_id,
                error=str(err)[:120] if err else None,
            )
            return
        if len(circuit.failures) >= self._threshold:
            circuit.state = CircuitState.OPEN
            circuit.opened_at = now
            logger.warning(
                "calendar.circuit_open",
                user=user_id,
                failures_in_window=len(circuit.failures),
                error=str(err)[:120] if err else None,
            )

    def snapshot(self) -> dict[str, Any]:
        out = {
            "failure_threshold": self._threshold,
            "window_seconds": self._window,
            "open_seconds": self._open_s,
            "users": {},
        }
        now = time.monotonic()
        for uid, c in self._users.items():
            c._prune(now, self._window)
            out["users"][uid] = {
                "state": c.state.value,
                "failures_in_window": len(c.failures),
                "reopens_in_s": (
                    round(max(0.0, self._open_s - (now - c.opened_at)), 1)
                    if c.state == CircuitState.OPEN
                    else None
                ),
            }
        return out

    def reset(self) -> None:
        self._users.clear()


_google_breaker: CircuitBreaker | None = None


def get_google_breaker() -> CircuitBreaker:
    global _google_breaker
    if _google_breaker is None:
        _google_breaker = CircuitBreaker(
            failure_threshold=5,
            window_seconds=60.0,
            open_seconds=30.0,
        )
    return _google_breaker


async def guarded_google_call(
    user_id: str,
    fn: Any,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a googleapiclient call under the circuit breaker.

    - Raises CircuitOpen when the user's breaker is open (call
      sites can catch and return cached data / a friendly message).
    - Records success/failure so the state machine can trip.
    - Only trips on 5xx / 429 / connection errors; 401 / 403 /
      404 are user-config problems, not backend flakiness, so we
      let them surface without counting them as failures.
    """
    import asyncio

    breaker = get_google_breaker()
    breaker.before_call(user_id)
    try:
        if asyncio.iscoroutinefunction(fn):
            result = await fn(*args, **kwargs)
        else:
            result = await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as e:  # noqa: BLE001
        if _is_transient_google_error(e):
            breaker.record_failure(user_id, e)
        raise
    breaker.record_success(user_id)
    return result


def _is_transient_google_error(err: Exception) -> bool:
    """Only trip on backend flakiness, not user config problems."""
    code = getattr(err, "status_code", None) or getattr(err, "resp", None)
    if hasattr(code, "status"):
        try:
            code = int(code.status)
        except (TypeError, ValueError):
            code = None
    if code is None:
        code = getattr(err, "code", None)
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None
    if code_int is not None:
        if code_int in (401, 403, 404):
            return False
        if code_int == 429 or 500 <= code_int < 600:
            return True
    text = str(err).lower()
    # HTTP status code smuggled into the message (googleapiclient wraps
    # HttpError as an Exception whose str() begins with the status).
    for status_code in ("500", "502", "503", "504", "429"):
        if text.startswith(status_code) or f" {status_code} " in text:
            return True
    if "quota" in text or "rate limit" in text or "backend" in text:
        return True
    if "connection" in text or "timeout" in text or "unavailable" in text:
        return True
    return False


def circuit_stats() -> dict[str, Any]:
    """Snapshot for /v1/admin/calendar_circuit."""
    return get_google_breaker().snapshot()


def reset_breaker() -> None:
    """Test-only helper."""
    global _google_breaker
    _google_breaker = None
