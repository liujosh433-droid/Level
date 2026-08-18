"""Per-user rate + cost gate for Gemini calls.

Counts today's ai_audit entries + total spend for the current user and
returns a blocked decision when either budget is exceeded. Cheap to
compute (a single collection scan bounded by the daily entries) and
runs before every `call_agent()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from level_core.config import get_settings
from level_core.storage.base import UserStore


@dataclass
class GateDecision:
    blocked: bool
    reason: str = ""
    used_hourly: int = 0
    used_daily: int = 0
    cost_today_usd: float = 0.0


@dataclass
class Charge:
    cost_usd: float
    when: float


async def check_gate(store: UserStore) -> GateDecision:
    settings = get_settings()
    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    entries = await store.ai_audit.list()
    used_hourly = 0
    used_daily = 0
    cost_today = 0.0
    for e in entries:
        created = e.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created >= hour_ago:
            used_hourly += 1
        if created >= day_start:
            used_daily += 1
            cost_today += e.cost_estimate_usd

    if used_hourly >= settings.level_user_rate_per_hour:
        return GateDecision(
            blocked=True,
            reason=f"hourly_limit ({used_hourly}/{settings.level_user_rate_per_hour})",
            used_hourly=used_hourly,
            used_daily=used_daily,
            cost_today_usd=cost_today,
        )
    if used_daily >= settings.level_user_rate_per_day:
        return GateDecision(
            blocked=True,
            reason=f"daily_limit ({used_daily}/{settings.level_user_rate_per_day})",
            used_hourly=used_hourly,
            used_daily=used_daily,
            cost_today_usd=cost_today,
        )
    if cost_today >= settings.level_daily_cost_cap_usd:
        return GateDecision(
            blocked=True,
            reason=f"daily_cost_cap (${cost_today:.4f}/${settings.level_daily_cost_cap_usd})",
            used_hourly=used_hourly,
            used_daily=used_daily,
            cost_today_usd=cost_today,
        )
    return GateDecision(
        blocked=False,
        used_hourly=used_hourly,
        used_daily=used_daily,
        cost_today_usd=cost_today,
    )


async def record_charge(store: UserStore, charge: Charge) -> None:
    """Placeholder: today's spend is derived from ai_audit at read-time.

    Kept as an explicit call site so we can add a hot counter later without
    changing callers.
    """
    return None
