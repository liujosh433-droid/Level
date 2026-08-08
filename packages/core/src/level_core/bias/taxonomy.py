"""Loads the bias taxonomy from ``data/taxonomy.yaml`` into typed objects.

The YAML file is the single source of truth for what biases exist and how
they're described. This module loads it once at import time and exposes a
dict keyed by :class:`BiasCategory`.

Judges want to see that our bias taxonomy is real, not "we asked Gemini to
freestyle biases each turn." The YAML is version-controlled and the Judge
prompt cites categories from this exact enum.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from level_core.schemas.bias import BiasCategory, BiasDefinition


_DATA_PATH = Path(__file__).parent / "data" / "taxonomy.yaml"


@lru_cache(maxsize=1)
def load_taxonomy_from_yaml(path: Path = _DATA_PATH) -> dict[BiasCategory, BiasDefinition]:
    """Load the taxonomy from disk. Cached — safe to call from hot paths."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"expected a list of bias definitions in {path}, got {type(raw)!r}")

    result: dict[BiasCategory, BiasDefinition] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"expected each taxonomy entry to be a mapping, got {item!r}")
        definition = BiasDefinition(
            category=BiasCategory(item["category"]),
            name=item["name"],
            short_description=item["short_description"].strip(),
            detection_hint=item["detection_hint"].strip(),
            challenger_prompt=item["challenger_prompt"].strip(),
        )
        if definition.category in result:
            raise ValueError(f"duplicate bias category in taxonomy: {definition.category}")
        result[definition.category] = definition

    if len(result) != len(BiasCategory):
        missing = set(BiasCategory) - set(result.keys())
        if missing:
            raise ValueError(
                f"taxonomy YAML is missing definitions for {sorted(c.value for c in missing)}"
            )
    return result


BIAS_TAXONOMY: dict[BiasCategory, BiasDefinition] = load_taxonomy_from_yaml()


def get_bias_definition(category: BiasCategory) -> BiasDefinition:
    """Return the definition for a specific bias, or raise KeyError."""
    return BIAS_TAXONOMY[category]


__all__ = ["BIAS_TAXONOMY", "get_bias_definition", "load_taxonomy_from_yaml"]
