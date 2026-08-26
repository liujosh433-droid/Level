"""Model Armor: prompt-injection prefilter that runs before every LLM call.

The user-facing anti-injection fence in base.py (`<user_input>...
</user_input>` + system directive) is our defense-in-depth: the LLM is
told not to follow instructions inside the fence. Model Armor is the
belt: a deterministic prefilter that flags the message as suspicious
BEFORE we pay for a Gemini call.

Actions:
  - BLOCK: obvious prompt injection or credential-fishing attempts.
    Returns a canned reply, does not touch Gemini.
  - FLAG:  soft signal ("this contained 'ignore previous'"). Logged
    but the call proceeds. Surfaces in /admin/traces so a demo
    can show the guard working end-to-end.
  - CLEAN: no signal; proceed normally.

This is deliberately conservative. The failure mode we care about is
"caregiver's kid types something weird" not "actual adversary". If any
pattern here starts hitting real user messages, tune it down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ArmorVerdict(StrEnum):
    CLEAN = "clean"
    FLAG = "flag"
    BLOCK = "block"


@dataclass(frozen=True)
class ArmorResult:
    verdict: ArmorVerdict
    reason: str = ""
    matched_patterns: tuple[str, ...] = ()


# Patterns that indicate obvious prompt-injection. Ordered from
# strongest signal (block) to weakest (flag). Each pattern is short
# enough to fit on one grep line so a judge can audit the list quickly.
_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_previous",
        re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|the above)\s+", re.I),
    ),
    (
        "reveal_system",
        re.compile(r"(?:reveal|print|show me|leak)\s+(?:the\s+)?(?:system|hidden)\s+prompt", re.I),
    ),
    (
        "you_are_now",
        re.compile(r"\byou\s+are\s+now\s+(?:a|an)\s+", re.I),
    ),
    (
        "developer_mode",
        re.compile(r"\b(?:developer|dev|god|admin|jailbreak)\s+mode\b", re.I),
    ),
    (
        "credential_fish",
        re.compile(r"\b(?:api\s*key|access[_\s]token|refresh\s+token|password|secret)\b", re.I),
    ),
    (
        "exec_arbitrary",
        re.compile(r"\b(?:eval|exec|os\.system|__import__|subprocess)\s*\(", re.I),
    ),
)

_FLAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("role_swap", re.compile(r"\b(?:pretend|role[- ]?play|act as)\s+(?:you\s+are|a|an)\b", re.I)),
    ("delimiter_break", re.compile(r"</?(?:user_input|context|system)>", re.I)),
    ("all_caps_shout", re.compile(r"[A-Z]{20,}")),
)


def scan(user_input: str) -> ArmorResult:
    """Run the prefilter. Cheap; ~microseconds even on 4KB input."""
    if not user_input:
        return ArmorResult(verdict=ArmorVerdict.CLEAN)

    blocked: list[str] = []
    for name, pattern in _BLOCK_PATTERNS:
        if pattern.search(user_input):
            blocked.append(name)
    if blocked:
        return ArmorResult(
            verdict=ArmorVerdict.BLOCK,
            reason=f"matched:{','.join(blocked[:3])}",
            matched_patterns=tuple(blocked),
        )

    flagged: list[str] = []
    for name, pattern in _FLAG_PATTERNS:
        if pattern.search(user_input):
            flagged.append(name)
    if flagged:
        return ArmorResult(
            verdict=ArmorVerdict.FLAG,
            reason=f"matched:{','.join(flagged[:3])}",
            matched_patterns=tuple(flagged),
        )

    return ArmorResult(verdict=ArmorVerdict.CLEAN)


BLOCK_REPLY = (
    "That message looks like a prompt-injection attempt. I don\u2019t follow "
    "instructions that ask me to reveal my prompt, change roles, or run code. "
    "If you meant something else, rephrase and I\u2019ll try again."
)
