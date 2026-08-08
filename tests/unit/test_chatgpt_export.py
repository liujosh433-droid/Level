"""ChatGPT export parser tests."""

from __future__ import annotations

import io
import json
import zipfile

from level_core.ingest.chatgpt_export import parse_chatgpt_export, parse_conversations_json
from level_core.schemas.signal import SignalSource


def _sample_conversations() -> list[dict]:
    return [
        {
            "id": "c1",
            "title": "School decision",
            "mapping": {
                "n1": {
                    "message": {
                        "author": {"role": "user"},
                        "create_time": 1700000000.0,
                        "content": {
                            "parts": [
                                "Should I switch Maya to the dual-language school mid-year?"
                            ]
                        },
                    }
                },
                "n2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 1700000001.0,
                        "content": {"parts": ["That sounds exciting!"]},
                    }
                },
            },
        }
    ]


def test_parse_conversations_extracts_user_only() -> None:
    raw = json.dumps(_sample_conversations())
    signals = parse_conversations_json(raw, user_id="u1")
    assert len(signals) == 1
    assert signals[0].source is SignalSource.CHAT_EXPORT
    assert "Maya" in (signals[0].text or "")
    assert "exciting" not in (signals[0].text or "")


def test_parse_zip_export() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("conversations.json", json.dumps(_sample_conversations()))
    signals = parse_chatgpt_export(buf.getvalue(), user_id="u1", filename="export.zip")
    assert len(signals) == 1
    assert signals[0].external_id.startswith("chatgpt:")
