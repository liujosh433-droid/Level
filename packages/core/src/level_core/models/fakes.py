"""Fake Gemini clients for tests and local development.

:class:`FakeGeminiClient` replays a scripted list of responses in FIFO
order. If the script is exhausted it falls back to a static default so
tests fail loudly rather than hanging.

Deterministic embeddings are produced by hashing the input into a small
float vector — good enough for the in-memory vector store's cosine ranking
to behave.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from level_core.models.base import GenerationRequest, GenerationResponse


@dataclass(slots=True)
class ScriptedResponse:
    """One scripted response the FakeGeminiClient will return.

    Attributes:
        text: The response text. If ``json_payload`` is provided, this is
            derived from it automatically.
        json_payload: Convenience for structured responses — this dict is
            JSON-serialized and returned as ``text``.
        input_tokens / output_tokens: Reported in the response for
            observability tests.
        finish_reason: Reported finish reason ("stop", "safety", etc.).
        expected_model: If set, the client asserts the request used this
            model id and raises otherwise. Useful for catching misrouting.
    """

    text: str | None = None
    json_payload: Any = None
    input_tokens: int = 100
    output_tokens: int = 100
    finish_reason: str = "stop"
    expected_model: str | None = None

    def resolve_text(self) -> str:
        if self.json_payload is not None:
            import json

            return json.dumps(self.json_payload)
        return self.text or ""


@dataclass(slots=True)
class FakeGeminiClient:
    """Test double for :class:`GeminiClient`.

    Instantiate with an ordered list of scripted responses. Each call to
    ``generate`` returns the next one. Requests are recorded on
    ``self.calls`` so tests can assert on them.
    """

    script: deque[ScriptedResponse] = field(default_factory=deque)
    default: ScriptedResponse = field(
        default_factory=lambda: ScriptedResponse(text="[fake gemini default response]")
    )
    calls: list[GenerationRequest] = field(default_factory=list)

    @classmethod
    def scripted(cls, responses: Sequence[ScriptedResponse]) -> FakeGeminiClient:
        return cls(script=deque(responses))

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.calls.append(request)

        scripted = self.script.popleft() if self.script else self.default
        if scripted.expected_model is not None and scripted.expected_model != request.model_id:
            raise AssertionError(
                f"FakeGeminiClient: expected model={scripted.expected_model!r} "
                f"but got {request.model_id!r}"
            )
        return GenerationResponse(
            text=scripted.resolve_text(),
            input_tokens=scripted.input_tokens,
            output_tokens=scripted.output_tokens,
            model_id=request.model_id,
            finish_reason=scripted.finish_reason,
        )


@dataclass(slots=True)
class FakeEmbeddingClient:
    """Deterministic fake embedder — hashes text to a 32-d float vector.

    Not semantically meaningful, but stable enough for the in-memory vector
    store's cosine-similarity ranking to behave in tests (identical texts
    hash to identical vectors → similarity = 1.0).
    """

    dims: int = 32
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, *, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Repeat to fill dims, convert bytes to floats in [-1, 1].
        needed = self.dims
        raw = (digest * ((needed // len(digest)) + 1))[:needed]
        return [(byte - 128) / 128.0 for byte in raw]


__all__ = ["FakeEmbeddingClient", "FakeGeminiClient", "ScriptedResponse"]
