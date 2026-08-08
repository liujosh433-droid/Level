"""Shared helpers used by every agent implementation.

Kept minimal on purpose — anything that would need to be shared across all
agents lives here so agent modules can be read top-to-bottom without
chasing imports.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError

from level_core.errors import InvalidAgentOutput

PROMPT_VERSION = "v1"

_M = TypeVar("_M", bound=BaseModel)


class AgentOutputModel(BaseModel):
    """Base class for the JSON schemas we require of Gemini.

    Distinct from :class:`level_core.schemas.base.LevelModel` because we want
    permissive coercion here — Gemini returns enum values as JSON strings,
    which strict mode would reject. We still forbid extra fields so hallucinated
    keys never sneak into typed downstream code.
    """

    model_config = ConfigDict(
        strict=False,
        extra="forbid",
        populate_by_name=True,
        use_enum_values=False,
    )


def prompt_sha(prompt: str) -> str:
    """SHA-256 of a prompt string — used to detect prompt drift across versions."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def safe_parse_json(text: str) -> object:
    """Parse JSON that may have prose or code fences around it.

    Gemini occasionally wraps JSON output in ```json ... ``` fences even
    when asked not to. This helper strips common wrappers before parsing.
    Raises :class:`json.JSONDecodeError` if the payload is unrecoverable.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove the opening fence including optional language hint, and the
        # trailing fence. Regex is anchored to keep this narrow.
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    # Some responses put a leading label like "JSON:" — drop it if present.
    if stripped.lower().startswith(("json:",)):
        stripped = stripped[5:].strip()
    return json.loads(stripped)


def parse_output(agent_name: str, text: str, schema: type[_M]) -> _M:
    """Parse ``text`` as JSON and validate against ``schema``.

    On failure, raises :class:`InvalidAgentOutput` with a descriptive
    message so the Conductor can either retry or degrade the turn.
    """
    try:
        payload = safe_parse_json(text)
    except json.JSONDecodeError as exc:
        raise InvalidAgentOutput(agent_name, f"invalid JSON: {exc.msg}") from exc
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise InvalidAgentOutput(agent_name, f"schema validation failed: {exc}") from exc


__all__ = [
    "AgentOutputModel",
    "PROMPT_VERSION",
    "parse_output",
    "prompt_sha",
    "safe_parse_json",
]
