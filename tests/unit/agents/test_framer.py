"""Tests for the Framer agent."""

from __future__ import annotations

import pytest

from level_core.agents.framer import Framer, FramerInput
from level_core.errors import InvalidAgentOutput
from level_core.models.fakes import FakeGeminiClient, ScriptedResponse


class TestFramer:
    async def test_produces_decision_frame(self) -> None:
        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        "subject": "should we switch schools",
                        "options": ["Switch to the new school", "Stay at the current school"],
                        "stakes": "Impacts my son's routine and my commute",
                        "time_pressure": "medium",
                        "horizon": "months",
                        "reversibility": "hard_to_reverse",
                    }
                )
            ]
        )
        framer = Framer(gemini=gemini, model_id="gemini-3.5-pro")
        frame = await framer.run(
            FramerInput(user_prompt="idk if we should switch schools next year")
        )
        assert frame.subject.startswith("should we")
        assert len(frame.options) == 2
        assert frame.time_pressure == "medium"
        assert frame.written_by is not None
        assert frame.written_by.startswith("framer@")

    async def test_invalid_output_raises_invalid_agent_output(self) -> None:
        gemini = FakeGeminiClient.scripted([ScriptedResponse(text="not json at all")])
        framer = Framer(gemini=gemini, model_id="gemini-3.5-pro")
        with pytest.raises(InvalidAgentOutput):
            await framer.run(FramerInput(user_prompt="hello"))

    async def test_schema_violation_raises_invalid_agent_output(self) -> None:
        gemini = FakeGeminiClient.scripted(
            [
                ScriptedResponse(
                    json_payload={
                        # missing subject/options → schema violation
                        "stakes": "yes",
                        "time_pressure": "medium",
                        "horizon": "months",
                        "reversibility": "hard_to_reverse",
                    }
                )
            ]
        )
        framer = Framer(gemini=gemini, model_id="gemini-3.5-pro")
        with pytest.raises(InvalidAgentOutput):
            await framer.run(FramerInput(user_prompt="test"))
