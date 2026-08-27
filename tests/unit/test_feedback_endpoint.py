"""Feedback endpoint: verifies the causal loop is real end-to-end.

These tests exercise the three cheap wins we shipped:

1. `submit_feedback` always writes a `FeedbackChip` AiAuditEntry so
   /admin/traces can render the click as a step in the trace tree.
2. When `audit_id` is threaded in from the artifact reply, the chip
   audit row's `parent_audit_id` links back to it - closing the
   causal graph from original agent call to chip click.
3. Generator agent feedback (EmailAgent, SummaryAgent) writes an
   `avoid`-tagged memory instead of a mismatched extractor negative.
   Extractor agent feedback still writes a real NegativeFeedback
   row.
"""

from __future__ import annotations

import pytest

from level_core.schemas import NegativeAgent
from level_core.storage.care_store import recent_negatives

from level_api.routes.feedback import FeedbackBody, submit_feedback


@pytest.mark.asyncio
async def test_keep_writes_memory_and_feedback_chip_audit(store) -> None:  # type: ignore[no-untyped-def]
    """Keep click for an email should write a positive memory AND a
    linked FeedbackChip audit row so /admin/traces shows the edge."""
    body = FeedbackBody(
        agent="EmailAgent",
        field="email.body",
        value="Hi Ms. Anna, Jordan will be out sick today. Thank you, Josh",
        verdict="keep",
        audit_id="aud_original_email",
    )
    result = await submit_feedback(body, store=store)

    assert result["status"] == "recorded"
    assert result["learned"] == "yes"
    chip_id = result["chip_audit_id"]
    assert chip_id.startswith("aud_")

    audits = await store.ai_audit.list()
    chip = next((a for a in audits if a.audit_id == chip_id), None)
    assert chip is not None
    assert chip.agent == "FeedbackChip"
    assert chip.model == "human"
    assert chip.parent_audit_id == "aud_original_email"
    assert chip.response["verdict"] == "keep"
    assert chip.response["routed_to"] == "memory_bank"

    profile = await store.profile.read() or {}
    memories = (profile.get("memory_bank") or {}).get("memories") or []
    assert any("Jordan" in m.get("text", "") for m in memories)


@pytest.mark.asyncio
async def test_email_not_me_writes_avoid_memory_not_reminder_negative(store) -> None:  # type: ignore[no-untyped-def]
    """The old alias sent EmailAgent adjust/not-me into the REMINDER
    negatives bucket, which ReminderAgent read but couldn't act on
    (it doesn't produce prose). The new behavior writes an
    avoid-tagged memory that EmailAgent reads on its next call."""
    body = FeedbackBody(
        agent="EmailAgent",
        field="email.body",
        value="Dear Ms. Anna, I am writing to inform you...",
        verdict="not_me",
        reason="too formal",
        audit_id="aud_original_email",
    )
    result = await submit_feedback(body, store=store)

    assert result["status"] == "learned"
    assert result["learned"] == "yes"
    assert result["memory_id"]  # a memory row was written

    # No reminder negative should exist (the old buggy behavior).
    reminder_negs = await recent_negatives(
        store, agent=NegativeAgent.REMINDER, limit=10
    )
    assert reminder_negs == []

    # An avoid-tagged memory should exist so EmailAgent's next call
    # sees it as an anti-example.
    profile = await store.profile.read() or {}
    memories = (profile.get("memory_bank") or {}).get("memories") or []
    avoid = [m for m in memories if "avoid" in (m.get("tags") or [])]
    assert len(avoid) == 1
    assert "too formal" not in avoid[0]["text"]  # reason is metadata, not text
    assert "emailagent" in avoid[0]["tags"]

    # The FeedbackChip audit is written with parent_audit_id set so
    # /admin/traces can link the click to the original draft.
    audits = await store.ai_audit.list()
    chip = next((a for a in audits if a.agent == "FeedbackChip"), None)
    assert chip is not None
    assert chip.parent_audit_id == "aud_original_email"
    assert chip.response["routed_to"] == "memory_bank_avoid"


@pytest.mark.asyncio
async def test_reminder_not_me_writes_real_negative(store) -> None:  # type: ignore[no-untyped-def]
    """Extractor agents (Reminder here) still route through the
    NegativeFeedback bucket the corresponding agent reads on its
    next call. This is the tight loop that already existed."""
    body = FeedbackBody(
        agent="ReminderAgent",
        field="reminder.text",
        value="Bring my charger",
        verdict="not_me",
        audit_id="aud_original_reminder",
    )
    result = await submit_feedback(body, store=store)

    assert result["status"] == "learned"
    assert result["negative_id"]

    negs = await recent_negatives(store, agent=NegativeAgent.REMINDER, limit=10)
    assert len(negs) == 1
    assert negs[0].value == "Bring my charger"

    audits = await store.ai_audit.list()
    chip = next((a for a in audits if a.agent == "FeedbackChip"), None)
    assert chip is not None
    assert chip.parent_audit_id == "aud_original_reminder"
    assert chip.response["routed_to"] == "negatives.ReminderAgent"


@pytest.mark.asyncio
async def test_feedback_without_audit_id_still_writes_chip(store) -> None:  # type: ignore[no-untyped-def]
    """Fast-path replies (regex parses, template email fallback) have
    no audit_id. The click still writes a FeedbackChip audit row so
    /admin/traces sees the click even though it can't link a parent."""
    body = FeedbackBody(
        agent="ReminderAgent",
        field="reminder.text",
        value="Water plants",
        verdict="adjust",
        # No audit_id - fast-path parse_reminder saved this reminder
    )
    result = await submit_feedback(body, store=store)

    assert result["status"] == "learned"
    audits = await store.ai_audit.list()
    chip = next((a for a in audits if a.agent == "FeedbackChip"), None)
    assert chip is not None
    assert chip.parent_audit_id is None
    # trace_id falls back to the chip's own audit_id when parent is None
    assert chip.trace_id == chip.audit_id


@pytest.mark.asyncio
async def test_summary_adjust_also_routes_to_avoid_memory(store) -> None:  # type: ignore[no-untyped-def]
    """SummaryAgent adjust/not-me should route through memory_bank
    with an avoid tag, same as EmailAgent - it's the second
    generator agent that doesn't have a dedicated negatives bucket."""
    body = FeedbackBody(
        agent="SummaryAgent",
        field="summary.text",
        value="You've got a busy morning with three overlapping calls.",
        verdict="adjust",
        reason="overly cheerful",
    )
    result = await submit_feedback(body, store=store)

    assert result["memory_id"]
    profile = await store.profile.read() or {}
    memories = (profile.get("memory_bank") or {}).get("memories") or []
    avoid = [m for m in memories if "avoid" in (m.get("tags") or [])]
    assert len(avoid) == 1
    assert "summaryagent" in avoid[0]["tags"]
    assert "adjust" in avoid[0]["tags"]
