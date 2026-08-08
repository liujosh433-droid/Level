"""ADK-registered agents + our own deterministic Conductor orchestration.

Each agent module exports:

- ``NAME`` / ``VERSION`` / ``DESCRIPTION`` — registered in the Agent Registry
- A prompt (module-level string)
- An input struct (dataclass) and output schema (Pydantic model)
- A class whose ``run()`` method is what the Conductor and tests call
- ``build_adk_agent()`` — an ``LlmAgent`` instance for the Registry

The Conductor sequences Framer → Retriever → Challenger → Judge via its
own deterministic control flow (giving us tight structured-output validation
and per-agent degradation handling) while also assembling an ADK
``SequentialAgent`` for the Registry.
"""

from level_core.agents.base import PROMPT_VERSION, prompt_sha, safe_parse_json
from level_core.agents.challenger import Challenger, ChallengerInput
from level_core.agents.conductor import Conductor, SessionInput
from level_core.agents.framer import Framer, FramerInput
from level_core.agents.ingest_normalizer import IngestNormalizer, IngestNormalizerInput
from level_core.agents.judge import Judge, JudgeInput
from level_core.agents.registry import AgentRegistry, InMemoryAgentRegistry
from level_core.agents.retriever import Retriever, RetrieverInput

__all__ = [
    "AgentRegistry",
    "Challenger",
    "ChallengerInput",
    "Conductor",
    "Framer",
    "FramerInput",
    "InMemoryAgentRegistry",
    "IngestNormalizer",
    "IngestNormalizerInput",
    "Judge",
    "JudgeInput",
    "PROMPT_VERSION",
    "Retriever",
    "RetrieverInput",
    "SessionInput",
    "prompt_sha",
    "safe_parse_json",
]
