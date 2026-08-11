"""Extract care-relevant facts from a pasted ChatGPT Memory summary."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from level_core.config import get_settings
from level_core.errors import ModelUnavailable
from level_core.models.base import GenerationRequest, GeminiClient
from level_core.models.factory import build_gemini_client
from level_core.observability.logger import get_logger
from level_core.schemas.signal import Fact, FactType, Signal, SignalSource

_logger = get_logger(__name__)

_MAX_PASTE_CHARS = 24_000
_MAX_FACTS = 24


class MemoryExtract(BaseModel):
    """Care-relevant distillate of a ChatGPT Memory paste."""

    facts: list[str] = Field(
        default_factory=list,
        description=(
            "Short standalone facts about the user's life/care roles "
            "(people, logistics, work, recovery, non-negotiables)."
        ),
    )
    care_note: str = Field(
        default="",
        description="1–3 sentence distillate for Care Profile update.",
    )


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clean_fact(raw: str) -> str | None:
    line = re.sub(r"^[\s•\-\*\d\.\)\(]+", "", (raw or "").strip())
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) < 12:
        return None
    # Drop obvious UI chrome from ChatGPT Memory screens.
    low = line.lower()
    if low.startswith(("manage memory", "memory updated", "chatgpt may", "saved memories")):
        return None
    return line[:500]


def split_memory_lines(text: str, *, limit: int = _MAX_FACTS) -> list[str]:
    """Structural line split only — not care classification.

    Used when Gemini is unavailable so the paste isn't dropped entirely.
    Prefer AI extract on the live path.
    """
    chunks = re.split(r"[\n\r]+|(?<=[.!?])\s+(?=[A-Z])", text)
    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        cleaned = _clean_fact(chunk)
        if cleaned is None:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def heuristic_extract_memory_facts(text: str, *, limit: int = _MAX_FACTS) -> list[str]:
    """Deprecated alias for :func:`split_memory_lines` (tests)."""
    return split_memory_lines(text, limit=limit)


async def extract_from_chatgpt_memory(
    text: str,
    *,
    gemini: GeminiClient | None = None,
) -> MemoryExtract:
    """Use Gemini to pull care-relevant facts; line-split wrapper if model is down."""
    paste = (text or "").strip()
    if len(paste) < 20:
        raise ValueError("Paste a longer ChatGPT Memory summary (at least a few lines).")
    paste = paste[:_MAX_PASTE_CHARS]

    client = gemini or build_gemini_client(get_settings())
    settings = get_settings()
    system = (
        "You extract caregiver-relevant facts from a ChatGPT Memory summary the user pasted. "
        "Keep only durable life facts: people they care for, co-parent/helpers, work constraints, "
        "school/sports logistics, elder care, recovery/sleep, household non-negotiables, "
        "and clear preferences about time or energy. "
        "Drop trivia, coding prefs, travel wishlists, and one-off chat topics. "
        "Rewrite each fact as a clear standalone statement (first or third person is fine). "
        "care_note should be a short distillate Level can apply to a Care Profile."
    )
    prompt = (
        "Return JSON with:\n"
        "- facts: up to 24 short care-relevant statements\n"
        "- care_note: 1–3 sentences summarizing care roles / people / constraints\n\n"
        f"ChatGPT Memory paste:\n{paste}\n"
    )
    try:
        resp = await client.generate(
            GenerationRequest(
                model_id=settings.fast_model,
                prompt=prompt,
                system_instruction=system,
                response_schema=MemoryExtract.model_json_schema(),
                temperature=0.1,
                max_output_tokens=700,
                metadata={"task": "chatgpt_memory_extract"},
            )
        )
        raw = (resp.text or "").strip()
        extracted = MemoryExtract.model_validate(json.loads(raw))
        facts: list[str] = []
        seen: set[str] = set()
        for item in extracted.facts:
            cleaned = _clean_fact(item)
            if cleaned is None:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            facts.append(cleaned)
            if len(facts) >= _MAX_FACTS:
                break
        care_note = (extracted.care_note or "").strip()[:800]
        if not facts and not care_note:
            facts = heuristic_extract_memory_facts(paste)
            care_note = " ".join(facts[:6])[:800]
        elif not facts:
            facts = heuristic_extract_memory_facts(paste)
        return MemoryExtract(facts=facts, care_note=care_note or " ".join(facts[:6])[:800])
    except ModelUnavailable:
        _logger.warning("chatgpt_memory_extract_unavailable")
    except Exception:  # noqa: BLE001
        _logger.exception("chatgpt_memory_extract_failed")

    facts = heuristic_extract_memory_facts(paste)
    if not facts:
        raise ValueError(
            "Couldn't find usable lines in that paste. "
            "Copy your Memory list from ChatGPT Settings → Personalization → Memory."
        )
    return MemoryExtract(facts=facts, care_note=" ".join(facts[:6])[:800])


def memory_extract_to_facts(
    extract: MemoryExtract,
    *,
    user_id: str,
    paste: str,
) -> list[Fact]:
    """Turn extracted Memory lines into Facts (skip fragile re-normalization)."""
    fp = _fingerprint(paste.strip())
    facts: list[Fact] = []
    for i, raw in enumerate(extract.facts):
        statement = (raw or "").strip()
        if len(statement) < 12:
            continue
        # Keep statements readable; pad very short bullets for retrieval quality.
        if len(statement) < 20:
            statement = f"From ChatGPT Memory: {statement}"
        statement = statement[:500]
        # Typing is left generic — Gemini already filtered for care relevance.
        facts.append(
            Fact(
                user_id=user_id,
                type=FactType.CONSTRAINT,
                statement=statement,
                source_signal_ids=[f"chatgpt-memory:{fp}:{i}"],
                salience=0.7,
            )
        )
    return facts


def memory_extract_to_signals(
    extract: MemoryExtract,
    *,
    user_id: str,
    paste: str,
) -> list[Signal]:
    """Turn extracted facts into ingest Signals (CHAT_EXPORT source)."""
    fp = _fingerprint(paste.strip())
    now = datetime.now(tz=timezone.utc)
    signals: list[Signal] = []
    for i, fact in enumerate(extract.facts):
        text = fact if len(fact) >= 20 else f"From ChatGPT Memory: {fact}"
        signals.append(
            Signal(
                user_id=user_id,
                source=SignalSource.CHAT_EXPORT,
                external_id=f"chatgpt-memory:{fp}:{i}",
                occurred_at=now,
                text=f"[ChatGPT Memory]\n{text}",
                mime_type="text/plain",
            )
        )
    if not signals and extract.care_note:
        signals.append(
            Signal(
                user_id=user_id,
                source=SignalSource.CHAT_EXPORT,
                external_id=f"chatgpt-memory:{fp}:note",
                occurred_at=now,
                text=f"[ChatGPT Memory]\n{extract.care_note}",
                mime_type="text/plain",
            )
        )
    return signals


__all__ = [
    "MemoryExtract",
    "extract_from_chatgpt_memory",
    "heuristic_extract_memory_facts",
    "memory_extract_to_facts",
    "memory_extract_to_signals",
    "split_memory_lines",
]
