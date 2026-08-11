"""ChatGPT Memory paste extraction tests."""

from __future__ import annotations

import pytest

from level_core.ingest.chatgpt_memory import (
    heuristic_extract_memory_facts,
    memory_extract_to_signals,
    MemoryExtract,
)


def test_heuristic_extracts_care_lines() -> None:
    paste = """
    User has a son named Jordan who plays soccer.
    User's mother needs dinner drop-offs twice a week.
    Works hybrid Tuesdays and Thursdays.
    """
    facts = heuristic_extract_memory_facts(paste)
    joined = " ".join(facts).lower()
    assert "jordan" in joined
    assert "mother" in joined or "dinner" in joined
    assert "hybrid" in joined


def test_memory_extract_to_facts() -> None:
    from level_core.ingest.chatgpt_memory import memory_extract_to_facts

    extract = MemoryExtract(
        facts=["Has a child named Jordan.", "Works full-time."],
        care_note="Child care for Jordan.",
    )
    facts = memory_extract_to_facts(extract, user_id="u1", paste="Jordan\nfull-time")
    assert len(facts) == 2
    assert all(len(f.statement) >= 20 for f in facts)
    assert "Jordan" in facts[0].statement


def test_memory_extract_to_signals() -> None:
    extract = MemoryExtract(
        facts=["Has a child named Jordan.", "Takes care of Mom on weeknights."],
        care_note="Child care for Jordan; elder care for Mom.",
    )
    signals = memory_extract_to_signals(
        extract,
        user_id="u1",
        paste="Jordan\nMom weeknights",
    )
    assert len(signals) == 2
    assert signals[0].source.value == "chat_export"
    assert signals[0].text.startswith("[ChatGPT Memory]")
    assert "Jordan" in signals[0].text


@pytest.mark.asyncio
async def test_extract_falls_back_when_gemini_unavailable() -> None:
    from level_core.errors import ModelUnavailable
    from level_core.ingest.chatgpt_memory import extract_from_chatgpt_memory
    from level_core.models.base import GenerationRequest, GenerationResponse

    class BoomClient:
        async def generate(self, request: GenerationRequest) -> GenerationResponse:
            raise ModelUnavailable("quota")

    paste = (
        "User's daughter Maya has Thursday pickup at 3pm.\n"
        "User is a solo parent with no co-parent.\n"
        "User recovers on Sunday evenings."
    )
    out = await extract_from_chatgpt_memory(paste, gemini=BoomClient())  # type: ignore[arg-type]
    assert out.facts
    assert any("Maya" in f or "pickup" in f.lower() for f in out.facts)
