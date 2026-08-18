"""Gate blocks calls when hourly rate or daily cost exceeded."""

from __future__ import annotations

import pytest
from level_core.agents.gate import check_gate
from level_core.config import get_settings
from level_core.schemas import AiAuditEntry


@pytest.mark.asyncio
async def test_gate_allows_by_default(store) -> None:  # type: ignore[no-untyped-def]
    decision = await check_gate(store)
    assert decision.blocked is False


@pytest.mark.asyncio
async def test_gate_blocks_after_hourly_limit(monkeypatch: pytest.MonkeyPatch, store) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LEVEL_USER_RATE_PER_HOUR", "2")
    get_settings.cache_clear()

    for i in range(3):
        await store.ai_audit.upsert(
            AiAuditEntry(
                audit_id=f"a_{i}",
                agent="Test",
                model="gemini-3.5-flash",
                prompt_hash="h",
                response={},
                cost_estimate_usd=0.0,
            )
        )
    decision = await check_gate(store)
    assert decision.blocked is True
    assert "hourly" in decision.reason


@pytest.mark.asyncio
async def test_gate_blocks_on_cost_cap(monkeypatch: pytest.MonkeyPatch, store) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LEVEL_DAILY_COST_CAP_USD", "0.001")
    get_settings.cache_clear()

    await store.ai_audit.upsert(
        AiAuditEntry(
            audit_id="cost1",
            agent="Test",
            model="gemini-3.5-pro",
            prompt_hash="h",
            response={},
            cost_estimate_usd=0.05,
        )
    )
    decision = await check_gate(store)
    assert decision.blocked is True
    assert "cost" in decision.reason
