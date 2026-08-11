"""Tests for the Challenger agent."""

from __future__ import annotations

from level_core.agents.challenger import Challenger, ChallengerInput
from level_core.models.fakes import FakeGeminiClient, ScriptedResponse
from level_core.schemas.decision import DecisionFrame
from level_core.schemas.signal import Fact, FactType


def _frame() -> DecisionFrame:
    return DecisionFrame(
        subject="should we switch schools",
        options=["Switch to the new school", "Stay at the current school"],
        stakes="Impacts routine and my commute",
        time_pressure="medium",
        horizon="months",
        reversibility="hard_to_reverse",
    )


def _fact(fact_id: str, statement: str, type_: FactType = FactType.VALUE_STATEMENT) -> Fact:
    return Fact(
        user_id="u1",
        type=type_,
        statement=statement,
        fact_id=fact_id,  # type: ignore[call-arg]
    )


class TestChallenger:
    async def test_produces_challenge_questions_with_citations(self) -> None:
        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        "questions": [
                            {
                                "question": (
                                    "You mentioned last month that you value stability during the "
                                    "school year — what changed?"
                                ),
                                "citations": [
                                    {
                                        "fact_id": "fact-a",
                                        "quote": "I value stability during the school year",
                                        "relevance": 0.95,
                                    }
                                ],
                                "challenge_type": "value_alignment",
                            }
                        ]
                    }
                )
            ]
        )
        challenger = Challenger(gemini=gemini, model_id="gemini-3.5-pro")
        questions = await challenger.run(
            ChallengerInput(
                frame=_frame(),
                retrieved_facts=[_fact("fact-a", "I value stability during the school year")],
                manifesto_snippet=None,
                bias_profile=None,
                user_text="I think we should switch",
                coverage_note="Ok coverage",
            )
        )
        assert len(questions) == 1
        assert questions[0].challenge_type == "value_alignment"
        assert questions[0].citations[0].fact_id == "fact-a"
        assert questions[0].written_by is not None

    async def test_max_three_questions_enforced_by_schema(self) -> None:
        # Even if the LLM emits more, Pydantic validation would reject; test
        # that a valid 3-question response is accepted.
        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        "questions": [
                            {
                                "question": (
                                    "You told me you couldn't take another year of the current "
                                    "commute — is that still true?"
                                ),
                                "citations": [
                                    {
                                        "fact_id": "fact-a",
                                        "quote": "commute is killing me",
                                        "relevance": 0.9,
                                    }
                                ],
                                "challenge_type": "assumption",
                            },
                            {
                                "question": (
                                    "What would need to be true about the new school for you to "
                                    "regret this in a year?"
                                ),
                                "citations": [],
                                "challenge_type": "time_horizon",
                            },
                            {
                                "question": (
                                    "You said last time that stability matters — how does switching "
                                    "square with that?"
                                ),
                                "citations": [
                                    {
                                        "fact_id": "fact-b",
                                        "quote": "stability matters",
                                        "relevance": 0.8,
                                    }
                                ],
                                "challenge_type": "value_alignment",
                            },
                        ]
                    }
                )
            ]
        )
        challenger = Challenger(gemini=gemini, model_id="gemini-3.5-pro")
        questions = await challenger.run(
            ChallengerInput(
                frame=_frame(),
                retrieved_facts=[
                    _fact("fact-a", "The commute is killing me"),
                    _fact("fact-b", "Stability matters more this year"),
                ],
                manifesto_snippet="I want to be present for my son.",
                bias_profile=None,
                user_text="Switching feels right",
                coverage_note="Good coverage",
            )
        )
        assert len(questions) == 3

    async def test_role_theft_with_care_profile_snippet(self) -> None:
        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        "questions": [
                            {
                                "question": (
                                    "You marked Thursday pickup as Keep — how does a late "
                                    "networking dinner not steal from that child-care window?"
                                ),
                                "citations": [
                                    {
                                        "fact_id": "fact-care",
                                        "quote": "Child care — protecting school and pickup",
                                        "relevance": 0.97,
                                    }
                                ],
                                "challenge_type": "role_theft",
                            }
                        ]
                    }
                )
            ]
        )
        challenger = Challenger(gemini=gemini, model_id="gemini-3.5-pro")
        questions = await challenger.run(
            ChallengerInput(
                frame=_frame(),
                retrieved_facts=[
                    _fact(
                        "fact-care",
                        "Child care — protecting school and pickup with Jordan",
                        FactType.RELATIONSHIP,
                    )
                ],
                manifesto_snippet=None,
                bias_profile=None,
                user_text="Can I take a late Thursday networking dinner?",
                coverage_note="Strong care-role coverage",
                care_profile_snippet=(
                    "Care roles you hold:\n"
                    "- Child care (Jordan): salience 0.92 — e.g. Thursday ~15:00 care block"
                ),
            )
        )
        assert questions[0].challenge_type == "role_theft"
        prompt_used = gemini.calls[-1].prompt
        assert "Care roles they hold" in prompt_used
        assert "Thursday" in prompt_used or "Child care" in prompt_used
