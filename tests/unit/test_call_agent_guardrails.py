"""call_agent guardrails: PII strip, hallucination guard, safety default, fakes."""

from __future__ import annotations

import pytest
from level_core.agents.base import AgentSpec, call_agent
from level_core.agents.fakes import register_fake
from level_core.agents.pii import strip_pii
from pydantic import BaseModel, Field


class MiniSchema(BaseModel):
    text: str
    source_span: str


class MiniList(BaseModel):
    items: list[MiniSchema] = Field(default_factory=list)


def test_strip_pii_replaces_emails_and_phones() -> None:
    original = "Reach me at jane.doe@example.org or +1 415-555-0100."
    scrubbed = strip_pii(original)
    assert "<email>" in scrubbed
    assert "<phone>" in scrubbed
    assert "jane" not in scrubbed or "@" not in scrubbed


@pytest.mark.asyncio
async def test_source_span_hallucination_drops_value(store) -> None:  # type: ignore[no-untyped-def]
    register_fake(
        "MiniAgent",
        {
            "items": [
                {"text": "real", "source_span": "totally"},
                {"text": "fake", "source_span": "never-said-this"},
            ]
        },
    )
    spec = AgentSpec(
        name="MiniAgent",
        model="flash",
        system="test",
        response_schema=MiniList,
    )
    result = await call_agent(spec, user_input="totally", store=store)
    assert result.value is not None
    values = [i.text for i in result.value.items]  # type: ignore[union-attr]
    assert values == ["real"]
    assert any("source_span" in d for d in result.fields_dropped)
    assert result.hallucinated is True


@pytest.mark.asyncio
async def test_schema_invalid_returns_safe_default(store) -> None:  # type: ignore[no-untyped-def]
    register_fake("MiniAgent", "not-json-at-all")
    spec = AgentSpec(
        name="MiniAgent",
        model="flash",
        system="test",
        response_schema=MiniList,
    )
    result = await call_agent(spec, user_input="anything", store=store)
    assert result.value is None
    assert result.hallucinated is True


@pytest.mark.asyncio
async def test_audit_entry_written(store) -> None:  # type: ignore[no-untyped-def]
    register_fake(
        "MiniAgent",
        {"items": [{"text": "a", "source_span": "a"}]},
    )
    spec = AgentSpec(
        name="MiniAgent",
        model="flash",
        system="test",
        response_schema=MiniList,
    )
    await call_agent(spec, user_input="a", store=store)
    entries = await store.ai_audit.list()
    assert entries and entries[0].agent == "MiniAgent"
    assert entries[0].input_tokens >= 0
