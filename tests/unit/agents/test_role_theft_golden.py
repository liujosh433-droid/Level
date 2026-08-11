"""Golden path: Keep'd child-care role + late dinner → role_theft with real fact_id."""

from __future__ import annotations

from level_core.agents.conductor import SessionInput, build_conductor
from level_core.guardrails.model_armor import LocalHeuristicModelArmor
from level_core.guardrails.outbound import OutboundGuardrail
from level_core.memory.fakes import build_in_memory_bank
from level_core.models.fakes import FakeEmbeddingClient, FakeGeminiClient, ScriptedResponse
from level_core.schemas.care import (
    CareProfile,
    CareRoleId,
    CareRoleState,
    ProtectedWindow,
)
from level_core.schemas.decision import Decision, DecisionStatus
from level_core.schemas.profile import BulletStatus
from level_core.schemas.signal import Fact, FactType
from level_core.schemas.turn import TurnStatus


class TestRoleTheftGoldenPath:
    async def test_late_dinner_challenges_keepd_pickup_with_real_citation(self) -> None:
        memory = build_in_memory_bank()
        embedder = FakeEmbeddingClient()

        fact = Fact(
            user_id="u-care",
            type=FactType.COMMITMENT,
            statement="Thursday school pickup for Maya is protected — I marked it Keep",
            salience=0.95,
        )
        await memory.facts.upsert(fact)
        [embedding] = await embedder.embed(texts=[fact.statement])
        await memory.vectors.upsert(
            user_id="u-care",
            fact_id=fact.fact_id,
            text=fact.statement,
            embedding=embedding,
        )

        care = CareProfile(
            user_id="u-care",
            version=3,
            roles=[
                CareRoleState(
                    role_id=CareRoleId.CHILD_CARE,
                    label="Child care",
                    salience=0.94,
                    weekly_load_hours=18.0,
                    status=BulletStatus.ACCEPTED,
                    people=["Maya"],
                    source_fact_ids=[fact.fact_id],
                    protected_windows=[
                        ProtectedWindow(
                            label="Thursday school pickup",
                            weekday=3,
                            start_hour=15,
                            end_hour=17,
                            evidence="Keep'd pickup window",
                        )
                    ],
                ),
                CareRoleState(
                    role_id=CareRoleId.PAID_WORK,
                    label="Work/Job",
                    salience=0.7,
                    weekly_load_hours=40.0,
                    status=BulletStatus.PENDING,
                ),
            ],
            conflict_summaries=[
                "Late networking dinners steal from Thursday pickup for Maya."
            ],
        )
        await memory.manifestos.save_care_profile(care)

        decision = Decision(user_id="u-care", status=DecisionStatus.OPEN)
        await memory.decisions.create(decision)

        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        "subject": "late Thursday networking dinner vs Maya pickup",
                        "options": ["Accept dinner", "Protect pickup", "Reschedule dinner"],
                        "stakes": "Steals from a Keep'd child-care window",
                        "time_pressure": "high",
                        "horizon": "days",
                        "reversibility": "reversible",
                    }
                ),
                ScriptedResponse(
                    json_payload={
                        "coverage_note": "Care Profile + Keep'd pickup fact available"
                    }
                ),
                ScriptedResponse(
                    json_payload={
                        "questions": [
                            {
                                "question": (
                                    "You Keep'd Thursday pickup for Maya — how does a late "
                                    "networking dinner not steal from that child-care window?"
                                ),
                                "citations": [
                                    {
                                        "fact_id": fact.fact_id,
                                        "quote": fact.statement,
                                        "relevance": 0.98,
                                    }
                                ],
                                "challenge_type": "role_theft",
                            }
                        ]
                    }
                ),
                ScriptedResponse(
                    json_payload={
                        "events": [
                            {
                                "category": "framing",
                                "intensity": 0.55,
                                "evidence": "framed dinner as optional networking",
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
                user_id="u-care",
                decision_id=decision.decision_id,
                user_text=(
                    "Can I take a late Thursday networking dinner? "
                    "It would help my career but overlaps Maya's pickup."
                ),
            )
        )

        assert turn.status is TurnStatus.COMPLETE
        assert len(turn.challenger_questions) == 1
        q = turn.challenger_questions[0]
        assert q.challenge_type == "role_theft"
        assert q.citations, "role_theft must cite real Memory Bank facts"
        assert q.citations[0].fact_id == fact.fact_id
        assert "Maya" in q.question or "pickup" in q.question.lower()

        # Retriever / Challenger saw the Care Profile (not generic priorities).
        challenger_prompts = [
            c.prompt for c in gemini.calls if "Care roles" in c.prompt or "role_theft" in c.prompt
        ]
        assert any(
            "Child care" in p or "Maya" in p or "pickup" in p.lower()
            for p in (c.prompt for c in gemini.calls)
        ), "expected care-role grounding in agent prompts"
        assert challenger_prompts or any("Care roles" in c.prompt for c in gemini.calls)
