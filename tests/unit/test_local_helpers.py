"""Local-only helpers that were unmeasured because the suite never
hits Google. These stay in-process: no network, no credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from level_core.agents.identity import sign as sign_identity
from level_core.agents.identity import verify as verify_identity
from level_core.agents.invoke import (
    LLMUnavailable,
    _is_quota_error,
    _parse_retry_after,
    _try_gemma,
    _vertex_fallback_model,
)
from level_core.auth.sessions import require_user_id, sign_state, verify_state
from level_core.auth.tokens import clear_tokens, load_tokens, save_tokens
from level_core.calendar.circuit_breaker import get_google_breaker, reset_breaker
from level_core.calendar.person_guard import (
    attendee_token_union,
    evaluate_proposed_name,
    is_family_relation_word,
    is_responsibility_word,
)
from level_core.calendar.sync import (
    _cached_event_id,
    _fingerprint,
    _first_name_tokens,
    _merge_preserving_ai,
    _parse_time,
    _rebuild_daily_agenda,
    _sync_window,
    _to_cached_event,
    _watch_expires_at,
    _within_window,
    agenda_is_fresh,
    ensure_watch,
    refresh_agenda,
)
from level_core.calendar.webhook import _constant_time_equal, verify_channel
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    EventTime,
    UserSession,
)
from level_core.storage.factory import InvalidUserId, get_store, sanitize_user_id


def test_person_guard_drops_responsibility_and_empty() -> None:
    assert is_responsibility_word("Grocery")
    assert is_responsibility_word("")
    assert is_responsibility_word("!!!")
    assert not is_responsibility_word("Nova")
    assert is_family_relation_word("Papa")
    assert not is_family_relation_word("Jordan")

    empty = evaluate_proposed_name("   ", attendees=frozenset())
    assert empty.kept is False
    assert empty.reason == "empty_name"

    dropped = evaluate_proposed_name("Commute", attendees=frozenset())
    assert dropped.kept is False
    assert dropped.reason == "responsibility_word"

    family = evaluate_proposed_name("Mom", attendees=frozenset())
    assert family.kept is True
    assert family.reason == "family_word"

    confirmed = evaluate_proposed_name("Nova", attendees=frozenset({"nova"}))
    assert confirmed.kept is True
    assert confirmed.reason == "attendee_confirmed"

    uncertain = evaluate_proposed_name("Jordan", attendees=frozenset())
    assert uncertain.kept is True
    assert uncertain.reason == "uncertain"


def test_attendee_token_union_normalizes() -> None:
    now = datetime.now(UTC)
    events = [
        CachedEvent(
            event_id="e1",
            calendar_id="primary",
            summary="Soccer",
            time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
            attendee_tokens=[" Nova ", "THEO", ""],
        )
    ]
    assert attendee_token_union(events) == frozenset({"nova", "theo"})


def test_webhook_constant_time_and_verify_missing() -> None:
    assert _constant_time_equal(None, "x") is False
    assert _constant_time_equal("", "x") is False
    assert _constant_time_equal("abc", "abc") is True
    assert _constant_time_equal("abc", "abd") is False


@pytest.mark.asyncio
async def test_verify_channel_matches_stored_watch(store) -> None:  # type: ignore[no-untyped-def]
    assert await verify_channel(store, channel_id="c1", channel_token="t1") is False
    await store.calendar_sync.write({"watch_channel": {"id": "c1", "token": "t1"}})
    assert await verify_channel(store, channel_id="c1", channel_token="t1") is True
    assert await verify_channel(store, channel_id="c1", channel_token="nope") is False
    assert await verify_channel(store, channel_id="other", channel_token="t1") is False


@pytest.mark.asyncio
async def test_token_kv_roundtrip(store) -> None:  # type: ignore[no-untyped-def]
    assert await load_tokens(store) is None
    await save_tokens(store, payload={"access_token": "abc", "email": "a@b.co"})
    loaded = await load_tokens(store)
    assert loaded is not None
    assert loaded["access_token"] == "abc"
    await clear_tokens(store)
    assert await load_tokens(store) == {}


def test_identity_sign_and_verify_tamper() -> None:
    ident = sign_identity(name="ChatRouterAgent", version="2.0.0", prompt_hash="abc")
    roundtrip = verify_identity(ident.token)
    assert roundtrip is not None
    assert roundtrip.name == "ChatRouterAgent"
    assert roundtrip.prompt_hash == "abc"
    assert verify_identity("") is None
    assert verify_identity("not-a-token") is None
    assert verify_identity(ident.token + "x") is None
    assert verify_identity("!!!!.????") is None


def test_session_state_and_require_user() -> None:
    assert require_user_id(None) is None
    assert require_user_id("") is None
    from level_core.auth.sessions import build_session_cookie

    cookie = build_session_cookie(UserSession(user_id="u_abc", email="a@b.co"))
    assert require_user_id(cookie) == "u_abc"

    signed = sign_state("state-1", "verifier-1")
    assert verify_state(signed, "state-1") == "verifier-1"
    assert verify_state(signed, "wrong") is False
    assert verify_state(None, "state-1") is False
    assert verify_state(signed + "x", "state-1") is False
    no_pkce = sign_state("state-2", None)
    assert verify_state(no_pkce, "state-2") is True


def test_sanitize_user_id_rejects_hostile() -> None:
    assert sanitize_user_id("u_abc.def-1") == "u_abc.def-1"
    with pytest.raises(InvalidUserId):
        sanitize_user_id("")  # type: ignore[arg-type]
    with pytest.raises(InvalidUserId):
        sanitize_user_id("   ")
    with pytest.raises(InvalidUserId):
        sanitize_user_id(".")
    with pytest.raises(InvalidUserId):
        sanitize_user_id("..")
    with pytest.raises(InvalidUserId):
        sanitize_user_id("u/../other")
    with pytest.raises(InvalidUserId):
        sanitize_user_id(123)  # type: ignore[arg-type]
    with pytest.raises(InvalidUserId):
        get_store("../escape")


def test_invoke_error_helpers() -> None:
    assert _parse_retry_after(Exception("Please retry in 12.4s")) == 12
    assert _parse_retry_after(Exception("no hint")) is None

    class Err429(Exception):
        code = 429

    class Err400(Exception):
        status_code = 400

    assert _is_quota_error(Err429())
    assert _is_quota_error(Exception("RESOURCE_EXHAUSTED"))
    assert not _is_quota_error(Err400())
    assert _vertex_fallback_model("gemini-3.5-pro") == "gemini-2.5-pro"
    assert _vertex_fallback_model("gemini-3.5-flash") == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_try_gemma_skips_ineligible_and_unconfigured(store) -> None:  # type: ignore[no-untyped-def]
    class _Spec:
        name = "EmailAgent"

    assert await _try_gemma(_Spec(), []) is None  # type: ignore[arg-type]

    class _Eligible:
        name = "ChatRouterAgent"

    # No Gemma model / project in local tests → None, not a raise.
    assert await _try_gemma(_Eligible(), []) is None  # type: ignore[arg-type]


def test_get_gemini_client_raises_when_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from level_core.config import get_settings
    from level_core.agents.invoke import _get_gemini_client

    settings = get_settings()
    monkeypatch.setattr(settings, "google_api_key", "")
    monkeypatch.setattr(settings, "google_cloud_project", "")
    with pytest.raises(LLMUnavailable):
        _get_gemini_client()


def test_to_cached_event_edges() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    assert (
        _to_cached_event(
            {"id": "x", "status": "cancelled", "summary": "Gone"},
            calendar_id="primary",
            tz=tz,
        )
        is None
    )
    assert (
        _to_cached_event(
            {"id": "x", "summary": "No times"},
            calendar_id="primary",
            tz=tz,
        )
        is None
    )
    all_day = _to_cached_event(
        {
            "id": "day1",
            "summary": "All-day family",
            "start": {"date": "2026-08-20"},
            "end": {"date": "2026-08-21"},
        },
        calendar_id="primary",
        tz=tz,
    )
    assert all_day is not None
    assert all_day.time.all_day is True

    leveled = _to_cached_event(
        {
            "id": "evt2",
            "summary": "Theo soccer practice",
            "start": {"dateTime": "2026-08-20T16:00:00-07:00"},
            "end": {"dateTime": "2026-08-20T17:00:00-07:00"},
            "extendedProperties": {"private": {"origin": "level", "level_reason": "put_back"}},
            "attendees": [
                {"displayName": "Theo Ball"},
                {"email": "me@example.com"},
                {"displayName": "Self"},
            ],
        },
        calendar_id="other-cal",
        tz=tz,
    )
    assert leveled is not None
    assert leveled.origin == "level"
    assert leveled.level_reason == "put_back"
    assert leveled.event_id == "other-cal:evt2"
    assert "Theo" in leveled.attendee_tokens
    assert leveled.activity_type is ActivityType.SPORTS_SOCCER


def test_sync_pure_helpers() -> None:
    tz = ZoneInfo("UTC")
    assert _cached_event_id("primary", "e1") == "e1"
    assert _cached_event_id("cal-2", "e1") == "cal-2:e1"
    assert _parse_time({}, tz=tz) is None
    assert _parse_time({"dateTime": "2026-08-20T16:00:00Z"}, tz=tz) is not None
    assert _first_name_tokens([]) == []
    assert _first_name_tokens([{"displayName": "Me"}, {"email": "self@x.com"}]) == []

    now = datetime.now(UTC)
    existing = CachedEvent(
        event_id="e1",
        calendar_id="primary",
        summary="Work",
        time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
        activity_type=ActivityType.WORK,
        classified_at=now,
        matched_person_ids=["p_self"],
        matched_reminder_ids=["r1"],
        etag="old",
    )
    incoming = existing.model_copy(update={"etag": "new", "activity_type": None, "classified_at": None})
    merged = _merge_preserving_ai(existing, incoming)
    assert merged.activity_type is ActivityType.WORK
    assert merged.matched_person_ids == ["p_self"]
    assert merged.etag == "new"

    naive = existing.model_copy(
        update={"time": EventTime(start=now.replace(tzinfo=None), end=now, tz="UTC")}
    )
    assert _within_window(existing, now - timedelta(hours=1), now + timedelta(hours=2))
    assert _within_window(naive, now - timedelta(hours=1), now + timedelta(hours=2))
    assert _fingerprint([existing])
    assert agenda_is_fresh({"last_pull_at": "not-a-date"}) is False
    assert _watch_expires_at(None) is None
    assert _watch_expires_at({"expiration": "nope"}) is None
    assert _watch_expires_at({"expiration": 0}) is None
    future_ms = int((datetime.now(UTC) + timedelta(days=5)).timestamp() * 1000)
    parsed = _watch_expires_at({"expiration": str(future_ms)})
    assert parsed is not None


@pytest.mark.asyncio
async def test_sync_window_and_rebuild_and_watch_skip(store) -> None:  # type: ignore[no-untyped-def]
    from level_core.config import get_settings

    settings = get_settings()
    now = datetime.now(UTC)
    lo, hi = await _sync_window(store, settings=settings, now=now)
    assert lo < now < hi

    await store.profile.write({"calendar_window_days_back": 3, "calendar_window_days_forward": 2})
    lo2, hi2 = await _sync_window(store, settings=settings, now=now)
    assert (now - lo2).days == 3
    assert (hi2 - now).days == 2

    ev = CachedEvent(
        event_id="e1",
        calendar_id="primary",
        summary="Work",
        time=EventTime(start=now, end=now + timedelta(hours=1), tz="UTC"),
    )
    await _rebuild_daily_agenda(store, [ev], tz=ZoneInfo("UTC"))
    days = await store.daily_agenda.list()
    assert len(days) == 1
    # Second rebuild with the same ids is a no-op write.
    await _rebuild_daily_agenda(store, [ev], tz=ZoneInfo("UTC"))
    days2 = await store.daily_agenda.list()
    assert len(days2) == 1

    # Local public URL is http:// — Google won't accept it, so skip.
    assert await ensure_watch(store) is False


@pytest.mark.asyncio
async def test_refresh_agenda_short_circuits_open_breaker(store) -> None:  # type: ignore[no-untyped-def]
    reset_breaker()
    breaker = get_google_breaker()
    for _ in range(5):
        breaker.record_failure(store.user_id, Exception("500"))
    result = await refresh_agenda(store)
    assert result.last_error is not None
    assert "circuit_open" in result.last_error
    assert result.added == 0
    reset_breaker()
