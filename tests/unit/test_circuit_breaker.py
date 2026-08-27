"""Circuit breaker: state transitions, user isolation, transient vs non-transient."""

from __future__ import annotations

import time

import pytest

from level_core.calendar.circuit_breaker import (
    CircuitBreaker,
    CircuitOpen,
    CircuitState,
    _is_transient_google_error,
    guarded_google_call,
    reset_breaker,
)


def test_starts_closed() -> None:
    b = CircuitBreaker()
    assert b.state("u1") == CircuitState.CLOSED
    b.before_call("u1")


def test_opens_after_threshold_failures() -> None:
    b = CircuitBreaker(failure_threshold=3, window_seconds=60, open_seconds=1)
    for _ in range(3):
        b.record_failure("u1", Exception("500"))
    assert b.state("u1") == CircuitState.OPEN
    with pytest.raises(CircuitOpen) as exc:
        b.before_call("u1")
    assert exc.value.user_id == "u1"


def test_success_resets_failure_count() -> None:
    b = CircuitBreaker(failure_threshold=3, window_seconds=60, open_seconds=1)
    b.record_failure("u1", Exception("500"))
    b.record_failure("u1", Exception("500"))
    b.record_success("u1")
    b.record_failure("u1", Exception("500"))
    assert b.state("u1") == CircuitState.CLOSED


def test_half_open_after_timer_then_open_on_failure() -> None:
    b = CircuitBreaker(failure_threshold=2, window_seconds=60, open_seconds=0.05)
    b.record_failure("u1", Exception("500"))
    b.record_failure("u1", Exception("500"))
    assert b.state("u1") == CircuitState.OPEN
    time.sleep(0.08)
    assert b.state("u1") == CircuitState.HALF_OPEN
    b.record_failure("u1", Exception("500"))
    assert b.state("u1") == CircuitState.OPEN


def test_half_open_success_closes() -> None:
    b = CircuitBreaker(failure_threshold=2, window_seconds=60, open_seconds=0.05)
    b.record_failure("u1", Exception("500"))
    b.record_failure("u1", Exception("500"))
    time.sleep(0.08)
    _ = b.state("u1")
    b.record_success("u1")
    assert b.state("u1") == CircuitState.CLOSED


def test_different_users_isolated() -> None:
    b = CircuitBreaker(failure_threshold=2, window_seconds=60, open_seconds=30)
    b.record_failure("u1", Exception("500"))
    b.record_failure("u1", Exception("500"))
    assert b.state("u1") == CircuitState.OPEN
    assert b.state("u2") == CircuitState.CLOSED


def test_transient_error_detection() -> None:
    class Err500(Exception):
        status_code = 500

    class Err401(Exception):
        status_code = 401

    class Err429(Exception):
        status_code = 429

    assert _is_transient_google_error(Err500())
    assert _is_transient_google_error(Err429())
    assert not _is_transient_google_error(Err401())
    assert _is_transient_google_error(Exception("Connection timeout"))
    assert not _is_transient_google_error(Exception("invalid_grant"))


async def test_guarded_call_records_failure_on_transient() -> None:
    reset_breaker()

    def boom() -> None:
        err = Exception("500 backendError")
        raise err

    from level_core.calendar.circuit_breaker import get_google_breaker

    for _ in range(5):
        with pytest.raises(Exception):
            await guarded_google_call("u1", boom)
    assert get_google_breaker().state("u1") == CircuitState.OPEN
    reset_breaker()


async def test_guarded_call_does_not_trip_on_auth_error() -> None:
    reset_breaker()

    class AuthErr(Exception):
        status_code = 401

    def bad_auth() -> None:
        raise AuthErr("unauthorized")

    from level_core.calendar.circuit_breaker import get_google_breaker

    for _ in range(5):
        with pytest.raises(AuthErr):
            await guarded_google_call("u1", bad_auth)
    assert get_google_breaker().state("u1") == CircuitState.CLOSED
    reset_breaker()
