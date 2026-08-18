"""Test-only fake agent responses.

Enabled by setting `LEVEL_AI_MODE=replay` (default in tests) or explicit
`register_fake(name, response)` calls in unit tests.
"""

from __future__ import annotations

import json
from typing import Any

from level_core.agents.base import _RawResponse

_fakes: dict[str, list[dict[str, Any]]] = {}


def register_fake(agent_name: str, response: dict[str, Any] | list[Any] | str) -> None:
    """Queue a response for the next `call_agent(agent_name, ...)`.

    Repeated calls to `register_fake` queue additional responses.
    """
    payload = response if isinstance(response, str) else json.dumps(response, default=str)
    _fakes.setdefault(agent_name, []).append({"text": payload})


def clear_fakes() -> None:
    _fakes.clear()


def is_faked(agent_name: str) -> bool:
    return bool(_fakes.get(agent_name))


def fake_call(agent_name: str, contents: list[dict[str, Any]]) -> _RawResponse:
    queue = _fakes.get(agent_name) or []
    if not queue:
        return _RawResponse(text="{}", input_tokens=0, output_tokens=0)
    item = queue.pop(0)
    return _RawResponse(
        text=item["text"],
        input_tokens=item.get("input_tokens", 100),
        output_tokens=item.get("output_tokens", 50),
    )
