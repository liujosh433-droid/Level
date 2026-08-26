"""Per-user rate + cost gate for Gemini calls.

Design (v2, this session): each user has a small counter document stored
under `profile["_gate_counters"]` with three windows:

  {
    "hour_bucket": "2026-08-26T21",       # UTC hour bucket
    "hour_calls":  12,
    "day_bucket":  "2026-08-26",           # UTC day bucket
    "day_calls":   87,
    "day_cost_usd": 1.234500,
  }

`check_gate()` reads THIS ONE DOC (O(1)) instead of scanning the entire
`ai_audit` collection (O(N) — the v1 pattern). At Firestore scale that's
the difference between 1 doc read per chat turn and 500-5000. See
docs/STATE_AND_LIFECYCLE.md for the full rationale.

`record_charge()` bumps the counter after every successful call. When
the window boundary rolls over the counter resets to 1.

We keep a bootstrap path (`_hydrate_from_audit`) so upgrading an
existing user is transparent: first read backfills the counter from the
last hour + day of `ai_audit` and then stays in sync.

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
class Charge:
    cost_usd: float
    when: float  # unix seconds — timezone naive is fine, we bucket in UTC


def _hour_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H")


def _day_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


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

    # Roll stale windows: an hour + day cache that outlives its bucket
    # is invalid, not "keep the count".
    if counters.get("hour_bucket") != _hour_bucket(now):
        counters = dict(counters)
        counters["hour_bucket"] = _hour_bucket(now)
        counters["hour_calls"] = 0
    if counters.get("day_bucket") != _day_bucket(now):
        counters = dict(counters)
        counters["day_bucket"] = _day_bucket(now)
        counters["day_calls"] = 0
        counters["day_cost_usd"] = 0.0
    return counters


async def _hydrate_from_audit(
    store: UserStore, now: datetime
) -> dict[str, Any]:
    """One-time backfill of the counter from ai_audit.

    Only runs on first gate check per user (or if the counter was wiped).
    Costs one scan; every subsequent request is O(1).
    """
    hour_ago = now - timedelta(hours=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    used_hourly = 0
    used_daily = 0
    cost_today = 0.0
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


async def check_gate(
    store: UserStore, *, agent: str | None = None
) -> GateDecision:
    """Check whether the current user has budget for one more agent call.

    O(1): one document read (or a single bootstrap scan on first ever
    call). Router calls get a softer cap so chat is never silent.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    counters = await _read_counters(store, now)
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


async def record_charge(store: UserStore, charge: Charge) -> None:
    """Increment the counter for one accepted call.

    O(1): read + increment + write of the counter doc. Under Firestore
    this is a single RMW; we don't need a transaction because a per-user
    counter has no cross-user contention. Under our local JSON backend
    it's an atomic-per-process update.

    Robust to loss: if the counter blows up mid-write, `check_gate`
    bootstraps from ai_audit on the very next call.
    """
    now = datetime.now(UTC)
    try:
        profile = await store.profile.read() or {}
        counters = profile.get(GATE_COUNTER_KEY)
        if not isinstance(counters, dict) or "day_bucket" not in counters:
            counters = await _hydrate_from_audit(store, now)
        counters = dict(counters)
        if counters.get("hour_bucket") != _hour_bucket(now):
            counters["hour_bucket"] = _hour_bucket(now)
            counters["hour_calls"] = 0
        if counters.get("day_bucket") != _day_bucket(now):
            counters["day_bucket"] = _day_bucket(now)
            counters["day_calls"] = 0
            counters["day_cost_usd"] = 0.0
        counters["hour_calls"] = int(counters.get("hour_calls", 0)) + 1
        counters["day_calls"] = int(counters.get("day_calls", 0)) + 1
        counters["day_cost_usd"] = float(counters.get("day_cost_usd", 0.0)) + float(
            charge.cost_usd
        )
        counters["updated_at"] = now.isoformat()
        profile[GATE_COUNTER_KEY] = counters
        await store.profile.write(profile)
    except Exception:  # noqa: BLE001 - counter must never break a call
        return None
