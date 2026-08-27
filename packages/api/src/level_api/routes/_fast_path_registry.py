"""Registry of deterministic chat fast-paths.

Each fast-path is a regex-driven handler that runs BEFORE the router
LLM. If it matches, it returns a full reply dict; if not, it returns
None and the next path gets a chance. The router LLM is the final
safety net for anything no fast-path matched.

This registry gives the whole chat system three properties:

  1. **Discoverability.** /v1/admin/intents lists every intent Level
     handles, its example utterances, and its priority order. A judge
     (or a new engineer) can see the input universe at a glance without
     grepping the dispatcher.
  2. **Consistent priority.** The dispatch order was previously an
     ad-hoc sequence of `if ... return` blocks in chat.py. Making it
     data-driven means adding a new intent is one entry, not a code
     change to the dispatcher.
  3. **Instrumentation.** Every fast-path attempt logs `matched=True/False`
     with the intent name, so we can measure hit rates and see which
     patterns need broadening.

Handlers accept `(store, message, history)` (matching how the existing
functions in chat.py are shaped) and return a dict-or-None. Wrapping
them here is a pure organizational change - no new plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from level_core.storage.base import UserStore

FastPathHandler = Callable[
    [UserStore, str, list[dict[str, str]]], Awaitable[dict[str, Any] | None]
]


@dataclass(frozen=True)
class FastPath:
    """One deterministic intent that runs before the router LLM.

    - `priority`: lower runs first. Reserved priorities:
        0-9   : session state (pending confirmations, pending picks)
        10-19 : chit-chat / empathy / agenda-lookup (read-only)
        20-39 : mutating intents (priority, person, reminder, email, calendar)
        40-49 : reserved for future
    - `examples`: representative user utterances that hit this path.
      Surfaced via /v1/admin/intents so the input universe is
      documented in code, not in prose.
    """

    name: str
    handler: FastPathHandler
    priority: int
    description: str
    examples: tuple[str, ...] = field(default_factory=tuple)
    mutates_state: bool = False


_REGISTRY: list[FastPath] = []


def register(fp: FastPath) -> FastPath:
    """Add or replace a fast-path. Idempotent by name."""
    global _REGISTRY
    _REGISTRY = [x for x in _REGISTRY if x.name != fp.name]
    _REGISTRY.append(fp)
    _REGISTRY.sort(key=lambda x: (x.priority, x.name))
    return fp


def all_paths() -> list[FastPath]:
    """Ordered list of registered fast-paths (lowest priority first)."""
    return list(_REGISTRY)


def to_dict() -> list[dict[str, Any]]:
    """Serializable snapshot for /v1/admin/intents."""
    return [
        {
            "name": fp.name,
            "priority": fp.priority,
            "description": fp.description,
            "examples": list(fp.examples),
            "mutates_state": fp.mutates_state,
        }
        for fp in all_paths()
    ]
