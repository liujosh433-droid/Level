"""Tests for the schema layer — round-trip serialization + validation constraints."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from level_core.schemas.bias import BiasCategory, BiasEvent, BiasProfile
from level_core.schemas.decision import Decision, DecisionFrame, DecisionStatus
from level_core.schemas.signal import Fact, FactType, Signal, SignalSource
from level_core.schemas.turn import ChallengeQuestion, Citation, Turn, TurnRole, TurnStatus


class TestSignal:
    def test_signal_round_trips_through_json(self) -> None:
        signal = Signal(
            user_id="u1",
            source=SignalSource.GCAL,
            external_id="event-1",
            text="Picture day on Friday",
        )
        payload = signal.model_dump_json()
        clone = Signal.model_validate_json(payload)
        assert clone.signal_id == signal.signal_id
        assert clone.source is SignalSource.GCAL

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Signal(
                user_id="u1",
                source=SignalSource.GCAL,
                external_id="e1",
                totally_unknown_field="oops",  # type: ignore[call-arg]
            )


class TestFact:
    def test_salience_bounded(self) -> None:
        with pytest.raises(ValidationError):
            Fact(
                user_id="u1",
                type=FactType.VALUE_STATEMENT,
                statement="I care about being present",
                salience=1.5,
            )

    def test_defaults(self) -> None:
        fact = Fact(
            user_id="u1",
            type=FactType.COMMITMENT,
            statement="I will cook Sunday dinner every week",
        )
        assert fact.salience == 0.5
        assert fact.confidence == 0.8


class TestDecisionFrame:
    def test_requires_at_least_two_options(self) -> None:
        with pytest.raises(ValidationError):
            DecisionFrame(
                subject="Should we switch schools",
                options=["Switch"],
                stakes="High — impacts child's routine",
            )

    def test_valid_frame(self) -> None:
        frame = DecisionFrame(
            subject="Should we switch schools",
            options=["Switch", "Stay"],
            stakes="Impacts the child's routine and my commute",
            time_pressure="medium",
            horizon="months",
            reversibility="hard_to_reverse",
        )
        assert frame.time_pressure == "medium"
        assert len(frame.options) == 2


class TestDecision:
    def test_new_decision_defaults(self) -> None:
        decision = Decision(user_id="u1")
        assert decision.status is DecisionStatus.OPEN
        assert decision.frame is None


class TestTurn:
    def test_pending_turn(self) -> None:
        turn = Turn(
            user_id="u1",
            decision_id="d1",
            role=TurnRole.USER,
            user_text="I'm not sure",
        )
        assert turn.status is TurnStatus.PENDING
        assert turn.challenger_questions == []


class TestChallengeQuestion:
    def test_challenge_question_with_citation(self) -> None:
        q = ChallengeQuestion(
            question="You said you couldn't take another year at that job — what's changed?",
            citations=[Citation(fact_id="fact-1", quote="couldn't take another year", relevance=0.9)],
            challenge_type="value_alignment",
        )
        assert q.citations[0].fact_id == "fact-1"


class TestBiasEvent:
    def test_intensity_bounded(self) -> None:
        with pytest.raises(ValidationError):
            BiasEvent(
                user_id="u1",
                decision_id="d1",
                turn_id="t1",
                category=BiasCategory.SUNK_COST,
                intensity=2.0,
                evidence="quote",
            )


class TestBiasProfile:
    def test_empty_profile(self) -> None:
        profile = BiasProfile(user_id="u1")
        assert profile.scores == []
        assert profile.session_count == 0
