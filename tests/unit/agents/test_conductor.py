"""End-to-end Conductor test using fake Gemini + in-memory Memory Bank."""

from __future__ import annotations

from level_core.agents.conductor import SessionInput, build_conductor
from level_core.guardrails.model_armor import LocalHeuristicModelArmor
from level_core.guardrails.outbound import OutboundGuardrail
from level_core.memory.fakes import build_in_memory_bank
from level_core.models.fakes import FakeEmbeddingClient, FakeGeminiClient, ScriptedResponse
from level_core.schemas.decision import Decision, DecisionStatus
from level_core.schemas.signal import Fact, FactType
from level_core.schemas.turn import TurnStatus


class TestConductorHappyPath:
    async def test_full_turn_produces_completed_turn(self) -> None:
        memory = build_in_memory_bank()
        # Seed a fact + a decision.
        fact = Fact(
            user_id="u1",
            type=FactType.VALUE_STATEMENT,
            statement="I value stability during the school year",
        )
        await memory.facts.upsert(fact)
        # Seed the vector store so the retriever finds the fact.
        embedder = FakeEmbeddingClient()
        [embedding] = await embedder.embed(texts=[fact.statement])
        await memory.vectors.upsert(
            user_id="u1", fact_id=fact.fact_id, text=fact.statement, embedding=embedding
        )
        decision = Decision(user_id="u1", status=DecisionStatus.OPEN)
        await memory.decisions.create(decision)

        # Scripted Gemini: framer, then retriever coverage note, then challenger, then judge.
        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        "subject": "should we switch schools",
                        "options": ["Switch", "Stay"],
                        "stakes": "Impacts my son's routine",
                        "time_pressure": "medium",
                        "horizon": "months",
                        "reversibility": "hard_to_reverse",
                    }
                ),
                ScriptedResponse(json_payload={"coverage_note": "Reasonable coverage"}),
                ScriptedResponse(
                    json_payload={
                        "questions": [
                            {
                                "question": (
                                    "You said stability during the school year matters — how does "
                                    "switching fit that?"
                                ),
                                "citations": [
                                    {
                                        "fact_id": fact.fact_id,
                                        "quote": "I value stability during the school year",
                                        "relevance": 0.95,
                                    }
                                ],
                                "challenge_type": "value_alignment",
                            }
                        ]
                    }
                ),
                ScriptedResponse(
                    json_payload={
                        "events": [
                            {
                                "category": "framing",
                                "intensity": 0.4,
                                "evidence": "framed as one-shot switch",
                                "challenger_response_addressed_it": True,
                            }
                        ]
                    }
                ),
            ]
        )

        conductor = build_conductor(
            memory=memory,
            gemini=gemini,
            embedder=embedder,
            guardrail=OutboundGuardrail(client=LocalHeuristicModelArmor()),
        )

        turn = await conductor.run_turn(
            SessionInput(
                user_id="u1",
                decision_id=decision.decision_id,
                user_text="I think we should switch",
            )
        )

        assert turn.status is TurnStatus.COMPLETE
        assert len(turn.challenger_questions) == 1
        assert turn.challenger_questions[0].challenge_type == "value_alignment"
        assert len(turn.bias_event_ids) == 1

        # Persisted correctly.
        events = await memory.turns.list_bias_events_for_user(user_id="u1")
        assert len(events) == 1
        assert events[0].category.value == "framing"


class TestConductorHallucinationBlocked:
    async def test_hallucinated_citation_produces_blocked_turn(self) -> None:
        memory = build_in_memory_bank()
        embedder = FakeEmbeddingClient()
        decision = Decision(user_id="u1", status=DecisionStatus.OPEN)
        await memory.decisions.create(decision)

        # Retriever skips the LLM when there are no facts/manifesto, so the
        # script is: framer → challenger (hallucinated) → repair challenger
        # (still hallucinated). Guardrail blocks after the retry is exhausted.
        bad_challenge = ScriptedResponse(
            json_payload={
                "questions": [
                    {
                        "question": "You said X — what changed?",
                        "citations": [
                            {
                                "fact_id": "ghost-fact-id",
                                "quote": "invented",
                                "relevance": 0.9,
                            }
                        ],
                        "challenge_type": "assumption",
                    }
                ]
            }
        )
        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        "subject": "should we switch schools",
                        "options": ["Switch", "Stay"],
                        "stakes": "Big deal",
                        "time_pressure": "medium",
                        "horizon": "months",
                        "reversibility": "hard_to_reverse",
                    }
                ),
                bad_challenge,
                bad_challenge,
            ]
        )

        conductor = build_conductor(
            memory=memory,
            gemini=gemini,
            embedder=embedder,
            guardrail=OutboundGuardrail(client=LocalHeuristicModelArmor()),
        )
        turn = await conductor.run_turn(
            SessionInput(user_id="u1", decision_id=decision.decision_id, user_text="hi")
        )
        assert turn.status is TurnStatus.BLOCKED
        assert turn.degradation_reason is not None
        assert "guardrail" in turn.degradation_reason
