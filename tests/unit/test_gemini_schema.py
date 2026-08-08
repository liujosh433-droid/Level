"""Gemini response_schema sanitization."""

from __future__ import annotations

from level_core.models.gemini import sanitize_schema_for_gemini


def test_strips_additional_properties_and_inlines_defs() -> None:
    raw = {
        "title": "Foo",
        "type": "object",
        "additionalProperties": False,
        "$defs": {
            "Item": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"question": {"type": "string", "default": ""}},
                "required": ["question"],
            }
        },
        "properties": {
            "questions": {
                "type": "array",
                "items": {"$ref": "#/$defs/Item"},
            }
        },
        "required": ["questions"],
    }
    cleaned = sanitize_schema_for_gemini(raw)
    assert "additionalProperties" not in cleaned
    assert "$defs" not in cleaned
    assert "title" not in cleaned
    item = cleaned["properties"]["questions"]["items"]
    assert item["properties"]["question"] == {"type": "string"}
    assert "additionalProperties" not in item
    assert "$ref" not in item