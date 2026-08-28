"""Per-user rate + cost gate for Gemini calls.

Design (v3, atomic reservation): each user has a small counter document
stored under `profile["_gate_counters"]` with three windows:

  {
    "hour_bucket": "2026-08-26T21",       # UTC hour bucket
    "hour_calls":  12,
    "day_bucket":  "2026-08-26",           # UTC day bucket
    "day_calls":   87,
    "day_cost_usd": 1.234500,
  }

``reserve_slot()`` reads AND increments the counter inside a single
``profile.mutate()`` call, which is transactional on Firestore and lock-
guarded on the local JSON backend. This closes the TOCTOU that let
concurrent callers all pass a pre-mutation check_gate() at the same
instant and blow past the caps. When the LLM call finishes, callers
follow up with:

  * ``settle_cost(...)`` — bump the recorded ``day_cost_usd`` by the
    real cost estimate (we can't know the token count up front, so we
    reserve at a fixed prepay and true-up here).
  * ``release_slot(...)`` — decrement the counters when the LLM call
    failed to produce a chargeable response (e.g. LLM unavailable,
    quota exhausted before contact) so retries aren't unfairly gated.

``check_gate()`` remains available for pure read-only checks (e.g. a
soft-degrade decision that doesn't consume budget), and the module
keeps a bootstrap path (``_hydrate_from_audit``) so upgrading an
existing user is transparent.

Design (Collaborative Partner rubric): the chat router is EXEMPT from
the standard cost cap so users always get a response. It has its own
softer cap so a runaway loop still trips a limit. Downstream extractor
and generative agents respect the base cap and degrade to deterministic
fallbacks when blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from level_core.config import get_settings
from level_core.storage.base import UserStore

# Agent names that must never be gated by the standard daily cost cap.
# The router is what turns a chat message into an action at all —
# blocking it makes the product feel broken. Its softer cap is the
# safety valve for runaway loops.
ROUTER_EXEMPT_AGENTS: frozenset[str] = frozenset({"ChatRouterAgent"})

GATE_COUNTER_KEY = "_gate_counters"

# Reservation prepay: the token count isn't known until the LLM
# responds, so ``reserve_slot`` books this amount against the daily
# cost cap up front and ``settle_cost`` trues it up. Small enough that
# a burst of parallel reservations doesn't over-charge users, large
# enough that a naive short-response call doesn't over-issue budget.
RESERVATION_PREPAY_USD = 0.005


@dataclass
class GateDecision:
    blocked: bool
    reason: str = ""
    used_hourly: int = 0
    used_daily: int = 0
    cost_today_usd: float = 0.0
    # True when a soft-degrade path should be used instead of returning
    # nothing. The router sets this so chat.py can emit a canned reply.
    soft_degrade: bool = False


@dataclass
class Reservation:
    """Handle returned by reserve_slot when admission is granted.

    Callers pass this back to settle_cost/release_slot so the counter
    can be trued up or refunded without a second read.
    """

    granted: bool
    decision: GateDecision
    prepay_usd: float = 0.0


@dataclass
class Charge:
    cost_usd: float
    when: float  # unix seconds — timezone naive is fine, we bucket in UTC


def _hour_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H")


def _day_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _roll_windows(counters: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Reset stale hour/day buckets to zero. Pure function; no I/O."""
    counters = dict(counters)
    if counters.get("hour_bucket") != _hour_bucket(now):
        counters["hour_bucket"] = _hour_bucket(now)
        counters["hour_calls"] = 0
    if counters.get("day_bucket") != _day_bucket(now):
        counters["day_bucket"] = _day_bucket(now)
        counters["day_calls"] = 0
        counters["day_cost_usd"] = 0.0
    return counters


async def _read_counters(store: UserStore, now: datetime) -> dict[str, Any]:
    """Return a snapshot of the counter, rolling stale windows to 0.

    First call ever bootstraps the counter from ai_audit so upgrading
    users don't get a free window. See _hydrate_from_audit.
    """
    profile = await store.profile.read() or {}
    counters = profile.get(GATE_COUNTER_KEY)
    if not isinstance(counters, dict) or "day_bucket" not in counters:
        counters = await _hydrate_from_audit(store, now)
        # Best-effort persist so subsequent calls are O(1). We swallow
        # errors here because the gate must never block chat.
        try:
            profile[GATE_COUNTER_KEY] = counters
            await store.profile.write(profile)
        except Exception:  # noqa: BLE001
            pass
        return counters

    return _roll_windows(counters, now)


async def _hydrate_from_audit(
    store: UserStore, now: datetime
) -> dict[str, Any]:
    """One-time backfill of the counter from ai_audit.

    Only runs on first gate check per user (or if the counter was wiped).
    Uses a bounded window query when the backend exposes one so we don't
    pull the entire audit collection.
    """
    hour_ago = now - timedelta(hours=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used_hourly = 0
    used_daily = 0
    cost_today = 0.0
    since_fn = getattr(store.ai_audit, "list_since", None)
    entries = None
    if callable(since_fn):
        try:
            entries = await since_fn(day_start)
        except TypeError:
            entries = None
    if entries is None:
        entries = await store.ai_audit.list()
    for e in entries:
        created = e.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created >= hour_ago:
            used_hourly += 1
        if created >= day_start:
            used_daily += 1
            cost_today += e.cost_estimate_usd
    return {
        "hour_bucket": _hour_bucket(now),
        "hour_calls": used_hourly,
        "day_bucket": _day_bucket(now),
        "day_calls": used_daily,
        "day_cost_usd": cost_today,
        "hydrated_at": now.isoformat(),
    }


def _evaluate_caps(
    counters: dict[str, Any], agent: str | None
) -> GateDecision:
    """Pure cap evaluation; no I/O. Used by both read-only check and reserve."""
    settings = get_settings()
    used_hourly = int(counters.get("hour_calls", 0))
    used_daily = int(counters.get("day_calls", 0))
    cost_today = float(counters.get("day_cost_usd", 0.0))
    is_router = agent in ROUTER_EXEMPT_AGENTS

    if used_hourly >= settings.level_user_rate_per_hour:
        return GateDecision(
            blocked=True,
            reason=f"hourly_limit ({used_hourly}/{settings.level_user_rate_per_hour})",
            used_hourly=used_hourly,
            used_daily=used_daily,
            cost_today_usd=cost_today,
            soft_degrade=is_router,
        )
    if used_daily >= settings.level_user_rate_per_day:
        return GateDecision(
            blocked=True,
            reason=f"daily_limit ({used_daily}/{settings.level_user_rate_per_day})",
            used_hourly=used_hourly,
            used_daily=used_daily,
            cost_today_usd=cost_today,
            soft_degrade=is_router,
        )
    cap = settings.level_daily_cost_cap_usd
    if is_router:
        cap = cap * settings.level_router_cost_cap_multiplier
    if cost_today >= cap:
        return GateDecision(
            blocked=True,
            reason=f"daily_cost_cap (${cost_today:.4f}/${cap:.4f})",
            used_hourly=used_hourly,
            used_daily=used_daily,
            cost_today_usd=cost_today,
            soft_degrade=is_router,
        )
    return GateDecision(
        blocked=False,
        used_hourly=used_hourly,
        used_daily=used_daily,
        cost_today_usd=cost_today,
    )


async def check_gate(
    store: UserStore, *, agent: str | None = None
) -> GateDecision:
    """Read-only cap check for callers that don't want to reserve budget.

    Returns whether one more call *would* be allowed right now. Callers
    that will actually issue a call should prefer ``reserve_slot`` — the
    read-only variant is TOCTOU-vulnerable under concurrency and is
    exposed only for endpoints that need a soft-degrade decision without
    consuming budget (e.g. surface a card telling the user they're
    rate-limited).
    """
    now = datetime.now(UTC)
    counters = await _read_counters(store, now)
    return _evaluate_caps(counters, agent)


async def reserve_slot(
    store: UserStore,
    *,
    agent: str | None = None,
    prepay_usd: float = RESERVATION_PREPAY_USD,
) -> Reservation:
    """Atomically check caps and reserve one call slot.

    Reads the counter, evaluates caps, and (if not blocked) increments
    the hourly/daily call counts + adds ``prepay_usd`` to the daily
    cost — all inside one ``profile.mutate()``. On Firestore this is
    a transaction; on the local JSON backend it's a per-user file lock.
    Either way, N concurrent callers cannot all pass the check
    simultaneously and then blow past the cap.

    Callers must follow up with ``settle_cost`` on success (true up
    the real cost) or ``release_slot`` on failure (refund the slot).
    """
    now = datetime.now(UTC)
    # Bootstrap outside the mutation body so we don't do a nested
    # collection read from inside the transaction.
    seed = await _read_counters(store, now)
    outcome: dict[str, Any] = {}

    def _reserve(profile: dict[str, Any]) -> dict[str, Any]:
        profile = dict(profile)
        counters = profile.get(GATE_COUNTER_KEY)
        if not isinstance(counters, dict) or "day_bucket" not in counters:
            counters = seed
        counters = _roll_windows(counters, now)
        decision = _evaluate_caps(counters, agent)
        outcome["decision"] = decision
        if decision.blocked:
            profile[GATE_COUNTER_KEY] = counters
            return profile
        counters["hour_calls"] = int(counters.get("hour_calls", 0)) + 1
        counters["day_calls"] = int(counters.get("day_calls", 0)) + 1
        counters["day_cost_usd"] = float(
            counters.get("day_cost_usd", 0.0)
        ) + float(prepay_usd)
        counters["updated_at"] = now.isoformat()
        outcome["decision"] = _evaluate_caps(counters, agent)
        profile[GATE_COUNTER_KEY] = counters
        return profile

    try:
        await store.profile.mutate(_reserve)
    except Exception:  # noqa: BLE001 - never let the gate crash a call
        # Best-effort: fall back to read-only check so we still gate.
        # The lost increment is racier under load but preserves availability.
        return Reservation(
            granted=not (await check_gate(store, agent=agent)).blocked,
            decision=await check_gate(store, agent=agent),
            prepay_usd=0.0,
        )

    decision: GateDecision = outcome.get("decision") or GateDecision(blocked=True)
    return Reservation(
        granted=not decision.blocked,
        decision=decision,
        prepay_usd=prepay_usd if not decision.blocked else 0.0,
    )


async def settle_cost(
    store: UserStore,
    reservation: Reservation,
    *,
    actual_cost_usd: float,
) -> None:
    """True up a granted reservation to the real cost estimate.

    The reservation booked ``prepay_usd`` up front to make admission
    atomic. This adjusts by (actual - prepay). Never let a settle
    failure surface to the caller — a stale counter is recoverable
    next request, a crashed chat flow isn't.
    """
    if not reservation.granted:
        return
    delta = float(actual_cost_usd) - float(reservation.prepay_usd)
    if delta == 0.0:
        return
    now = datetime.now(UTC)

    def _settle(profile: dict[str, Any]) -> dict[str, Any]:
        profile = dict(profile)
        counters = profile.get(GATE_COUNTER_KEY)
        if not isinstance(counters, dict) or "day_bucket" not in counters:
            return profile
        counters = _roll_windows(counters, now)
        counters["day_cost_usd"] = max(
            0.0, float(counters.get("day_cost_usd", 0.0)) + delta
        )
        counters["updated_at"] = now.isoformat()
        profile[GATE_COUNTER_KEY] = counters
        return profile

    try:
        await store.profile.mutate(_settle)
    except Exception:  # noqa: BLE001
        return None


async def release_slot(
    store: UserStore, reservation: Reservation
) -> None:
    """Refund a granted reservation — hourly/daily calls and prepay cost.

    Called when the LLM call didn't produce a chargeable response
    (LLMUnavailable, QuotaExhausted before contact, etc.) so retries
    aren't unfairly rate-limited.
    """
    if not reservation.granted:
        return
    now = datetime.now(UTC)

    def _refund(profile: dict[str, Any]) -> dict[str, Any]:
        profile = dict(profile)
        counters = profile.get(GATE_COUNTER_KEY)
        if not isinstance(counters, dict) or "day_bucket" not in counters:
            return profile
        counters = _roll_windows(counters, now)
        counters["hour_calls"] = max(0, int(counters.get("hour_calls", 0)) - 1)
        counters["day_calls"] = max(0, int(counters.get("day_calls", 0)) - 1)
        counters["day_cost_usd"] = max(
            0.0,
            float(counters.get("day_cost_usd", 0.0))
            - float(reservation.prepay_usd),
        )
        counters["updated_at"] = now.isoformat()
        profile[GATE_COUNTER_KEY] = counters
        return profile

    try:
        await store.profile.mutate(_refund)
    except Exception:  # noqa: BLE001
        return None


async def record_charge(store: UserStore, charge: Charge) -> None:
    """Legacy: increment the counter for one accepted call.

    Retained so old call sites that hadn't been migrated to the
    reserve/settle pattern still update the counter correctly. Prefer
    ``reserve_slot`` + ``settle_cost`` in new code so admission and
    charge share one transaction.
    """
    now = datetime.now(UTC)
    try:
        existing_profile = await store.profile.read() or {}
        existing_counters = existing_profile.get(GATE_COUNTER_KEY)
        if not isinstance(existing_counters, dict) or "day_bucket" not in existing_counters:
            hydrated: dict[str, Any] = await _hydrate_from_audit(store, now)
        else:
            hydrated = existing_counters

        def _bump(profile: dict[str, Any]) -> dict[str, Any]:
            profile = dict(profile)
            counters = profile.get(GATE_COUNTER_KEY)
            if not isinstance(counters, dict) or "day_bucket" not in counters:
                counters = hydrated
            counters = _roll_windows(counters, now)
            counters["hour_calls"] = int(counters.get("hour_calls", 0)) + 1
            counters["day_calls"] = int(counters.get("day_calls", 0)) + 1
            counters["day_cost_usd"] = float(counters.get("day_cost_usd", 0.0)) + float(
                charge.cost_usd
            )
            counters["updated_at"] = now.isoformat()
            profile[GATE_COUNTER_KEY] = counters
            return profile

        await store.profile.mutate(_bump)
    except Exception:  # noqa: BLE001 - counter must never break a call
        return None
