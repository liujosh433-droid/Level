"""End-to-end proof that the feedback loop actually closes.

The unit tests in `test_feedback_endpoint.py` verify the WRITE side:
a click writes a memory / negative / audit row. This file verifies
the READ side: that the very next generator agent call actually
sees the resulting anti-example in its prompt context.

Without this test, the whole "captures feedback so it constantly
adapts" claim rests on prose only. This is the highest-value
integration test in the repo because it locks the contract that
the feedback endpoint and the generator agents share via
memory_bank.recall_split().
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from level_core.agents.email import run as email_run
from level_core.agents.fakes import clear_fakes, register_fake
from level_core.agents.summary import run as summary_run

from level_api.routes.feedback import FeedbackBody, submit_feedback


class _ContentsCapture:
    """Test double for `fake_call` that records the contents passed to
    each faked agent invocation. Lets us assert what the LLM actually
    saw in its <context> block."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from level_core.agents import fakes

        original = fakes.fake_call

        def wrapper(agent_name: str, contents: list[dict[str, Any]]):
            self.calls.append({"agent": agent_name, "contents": contents})
            return original(agent_name, contents)

        monkeypatch.setattr(fakes, "fake_call", wrapper)

    def prompt_text_for(self, agent: str) -> str:
        """Return the concatenated text parts sent to the named agent's
        most recent invocation. This is what the LLM would have read."""
        for call in reversed(self.calls):
            if call["agent"] == agent:
                pieces: list[str] = []
                for c in call["contents"]:
                    for p in c.get("parts") or []:
                        text = p.get("text")
                        if text:
                            pieces.append(text)
                return "\n".join(pieces)
        raise AssertionError(f"No {agent} call captured (calls: {[c['agent'] for c in self.calls]})")


@pytest.fixture
def contents_capture(monkeypatch: pytest.MonkeyPatch) -> _ContentsCapture:
    capture = _ContentsCapture()
    capture.install(monkeypatch)
    return capture


@pytest.mark.asyncio
async def test_email_not_me_reaches_next_email_call_as_avoid_example(  # type: ignore[no-untyped-def]
    store, contents_capture: _ContentsCapture
) -> None:
    """The full loop: user rejects an email tone -> the very next
    EmailAgent call sees that tone in its `avoid_examples` context.

    This is the single most important assertion in the repo about
    the feedback loop. If it ever fails, the "captures feedback so
    it constantly adapts" claim is broken and the demo won't hold up.
    """
    clear_fakes()

    # (1) Simulate that the caregiver clicked "not_me" on a previous
    # email draft whose body was overly formal.
    rejected_body = "Dear Ms. Anna, I am writing to formally notify you"
    await submit_feedback(
        FeedbackBody(
            agent="EmailAgent",
            field="email.body",
            value=rejected_body,
            verdict="not_me",
            reason="too formal",
            audit_id="aud_original_email",
        ),
        store=store,
    )

    # (2) Trigger the NEXT EmailAgent call. Register a benign fake
    # so the agent returns without error; we only care about what
    # got sent to the model in its context.
    register_fake(
        "EmailAgent",
        {
            "draft": {
                "subject": "Note about Jordan",
                "body": "Hi Anna,\n\nJordan is out today.\n\nThanks,\nJosh",
            }
        },
    )
    result = await email_run(
        store=store,
        intent="absence note",
        contact_display_name="Ms. Anna",
        kid_display_name="Jordan",
        signer_name="Josh",
        today="Wednesday, August 27, 2026",
    )
    assert result.value is not None

    # (3) The prompt sent to the next EmailAgent call must contain
    # the avoid_examples block with the rejected body.
    prompt = contents_capture.prompt_text_for("EmailAgent")
    assert "avoid_examples" in prompt, (
        "EmailAgent did not receive avoid_examples in its context; the "
        "click -> next-call loop is broken."
    )
    # Confirm the actual rejected value made it in (the JSON block is
    # embedded as a string, so a substring check is enough).
    assert "formally notify" in prompt, (
        "avoid_examples block was present but did not contain the "
        "rejected value from the feedback click."
    )


@pytest.mark.asyncio
async def test_email_keep_reaches_next_email_call_as_memory_bank(  # type: ignore[no-untyped-def]
    store, contents_capture: _ContentsCapture
) -> None:
    """Companion to the not_me test: a keep click writes a positive
    memory that lands in `memory_bank` (not `avoid_examples`) on the
    next EmailAgent call. Confirms the split is disjoint."""
    clear_fakes()

    kept_body = "Hi Anna, Jordan is out sick today. Thanks, Josh"
    await submit_feedback(
        FeedbackBody(
            agent="EmailAgent",
            field="email.body",
            value=kept_body,
            verdict="keep",
            audit_id="aud_original_email",
        ),
        store=store,
    )

    register_fake(
        "EmailAgent",
        {
            "draft": {
                "subject": "Note about Jordan",
                "body": "Hi Anna,\n\nJordan is out.\n\nThanks,\nJosh",
            }
        },
    )
    await email_run(
        store=store,
        intent="absence note",
        contact_display_name="Ms. Anna",
        kid_display_name="Jordan",
        signer_name="Josh",
    )

    prompt = contents_capture.prompt_text_for("EmailAgent")
    assert "memory_bank" in prompt, "keep click did not surface in next call's memory_bank"
    assert "Jordan is out sick today" in prompt
    # And it must NOT have leaked into avoid_examples (the split
    # would be broken if it did).
    context_json_start = prompt.find("<context>")
    context_json_end = prompt.find("</context>")
    context_block = prompt[context_json_start:context_json_end] if context_json_start >= 0 else prompt
    # If avoid_examples appears, the memory it contains must NOT be
    # the one we just kept.
    if '"avoid_examples"' in context_block:
        parsed_start = context_block.find("{")
        parsed = json.loads(context_block[parsed_start:])
        avoid_texts = [m.get("text") for m in (parsed.get("avoid_examples") or [])]
        assert kept_body not in avoid_texts, (
            "Keep memory leaked into avoid_examples - the recall_split "
            "contract is broken."
        )


@pytest.mark.asyncio
async def test_summary_not_me_reaches_next_summary_call_as_avoid_example(  # type: ignore[no-untyped-def]
    store, contents_capture: _ContentsCapture
) -> None:
    """SummaryAgent uses the same recall_split helper. If either
    caller drifts on the avoid contract this test catches it, so
    email.py + summary.py stay locked to the same behavior."""
    clear_fakes()

    rejected_summary = "You've got a jam-packed morning ahead, all systems go"
    await submit_feedback(
        FeedbackBody(
            agent="SummaryAgent",
            field="summary.text",
            value=rejected_summary,
            verdict="not_me",
            reason="too chirpy",
        ),
        store=store,
    )

    register_fake("SummaryAgent", {"summary": "You have three anchors today."})
    await summary_run(
        store=store,
        date_label="Wed Aug 27",
        event_lines=["9am standup"],
        missing_usual_lines=[],
        reminder_lines=[],
    )

    prompt = contents_capture.prompt_text_for("SummaryAgent")
    assert "avoid_examples" in prompt
    assert "jam-packed morning" in prompt


@pytest.mark.asyncio
async def test_avoid_and_positive_memories_coexist_in_next_call(  # type: ignore[no-untyped-def]
    store, contents_capture: _ContentsCapture
) -> None:
    """Both signal kinds should land in the SAME next call: positive
    memories in memory_bank, avoid memories in avoid_examples, no
    crossover. This is the disjointness contract recall_split
    guarantees."""
    clear_fakes()

    # A keep memory: reinforce a particular tone.
    await submit_feedback(
        FeedbackBody(
            agent="EmailAgent",
            field="email.body",
            value="Hi Anna, Jordan will be out today. Thanks, Josh",
            verdict="keep",
            audit_id="aud_kept",
        ),
        store=store,
    )
    # An avoid memory: reject a different tone.
    await submit_feedback(
        FeedbackBody(
            agent="EmailAgent",
            field="email.body",
            value="Dear Ms. Anna, please be advised that",
            verdict="not_me",
            reason="too stiff",
            audit_id="aud_rejected",
        ),
        store=store,
    )

    register_fake(
        "EmailAgent",
        {"draft": {"subject": "X", "body": "Hi Anna, thanks, Josh"}},
    )
    await email_run(
        store=store,
        intent="absence note",
        contact_display_name="Ms. Anna",
        kid_display_name="Jordan",
        signer_name="Josh",
    )

    prompt = contents_capture.prompt_text_for("EmailAgent")
    ctx_start = prompt.find("<context>")
    ctx_end = prompt.find("</context>")
    assert ctx_start >= 0 and ctx_end > ctx_start
    ctx_json = prompt[ctx_start + len("<context>"): ctx_end]
    parsed = json.loads(ctx_json)

    pos_texts = [m["text"] for m in (parsed.get("memory_bank") or [])]
    avoid_texts = [m["text"] for m in (parsed.get("avoid_examples") or [])]

    assert any("Jordan will be out today" in t for t in pos_texts)
    assert any("please be advised" in t for t in avoid_texts)
    # Disjoint: no text appears in both lists.
    assert not (set(pos_texts) & set(avoid_texts))
