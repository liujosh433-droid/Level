"""Parse OpenAI ChatGPT data exports into Level Signals.

Supports:
- A raw ``conversations.json`` file
- A zip containing ``conversations.json`` (the official export format)

We extract the *user's* messages (not the assistant's) as signals, capped
and chunked so a giant history doesn't blow the normalizer.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from level_core.schemas.signal import Signal, SignalSource


def _ts_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _walk_messages(conversation: dict[str, Any]) -> list[tuple[datetime | None, str]]:
    """Return (timestamp, text) for user messages in one conversation."""
    mapping = conversation.get("mapping") or {}
    title = (conversation.get("title") or "untitled").strip()
    out: list[tuple[datetime | None, str]] = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        message = node.get("message") or {}
        author = (message.get("author") or {}).get("role")
        if author != "user":
            continue
        content = message.get("content") or {}
        parts = content.get("parts") or []
        texts = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
        if not texts:
            continue
        body = "\n".join(texts)
        # Prefix with conversation title for retrieval context.
        text = f"[ChatGPT · {title}]\n{body}"
        out.append((_ts_to_dt(message.get("create_time")), text))
    out.sort(key=lambda t: t[0] or datetime.min.replace(tzinfo=timezone.utc))
    return out


def parse_conversations_json(
    raw: bytes | str,
    *,
    user_id: str,
    max_messages: int = 200,
    min_chars: int = 40,
) -> list[Signal]:
    """Parse conversations.json bytes/str into Signals."""
    data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("conversations.json must be a JSON array")

    signals: list[Signal] = []
    for conv_idx, conversation in enumerate(data):
        if not isinstance(conversation, dict):
            continue
        conv_id = str(conversation.get("id") or conv_idx)
        for msg_idx, (occurred_at, text) in enumerate(_walk_messages(conversation)):
            if len(text) < min_chars:
                continue
            signals.append(
                Signal(
                    user_id=user_id,
                    source=SignalSource.CHAT_EXPORT,
                    external_id=f"chatgpt:{conv_id}:{msg_idx}",
                    occurred_at=occurred_at,
                    text=text[:8000],
                    mime_type="application/json",
                )
            )
            if len(signals) >= max_messages:
                return signals
    return signals


def parse_chatgpt_export(
    payload: bytes,
    *,
    user_id: str,
    filename: str = "",
    max_messages: int = 200,
) -> list[Signal]:
    """Parse a ChatGPT export zip or bare conversations.json."""
    name = filename.lower()
    if name.endswith(".zip") or zipfile.is_zipfile(io.BytesIO(payload)):
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            candidates = [
                n
                for n in zf.namelist()
                if n.endswith("conversations.json") and not n.startswith("__MACOSX")
            ]
            if not candidates:
                raise ValueError("zip has no conversations.json")
            # Prefer root-level conversations.json
            candidates.sort(key=lambda n: (n.count("/"), len(n)))
            raw = zf.read(candidates[0])
            return parse_conversations_json(raw, user_id=user_id, max_messages=max_messages)

    return parse_conversations_json(payload, user_id=user_id, max_messages=max_messages)


__all__ = ["parse_chatgpt_export", "parse_conversations_json"]
