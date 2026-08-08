#!/usr/bin/env python3
"""Validate and print the bias taxonomy (sanity check for prompt contracts)."""

from __future__ import annotations

import os
import sys


def main() -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "core", "src"))
    from level_core.bias.taxonomy import BIAS_TAXONOMY

    print(f"Loaded {len(BIAS_TAXONOMY)} bias categories:\n")
    for category, definition in BIAS_TAXONOMY.items():
        print(f"  {category.value:20}  {definition.name}")
        print(f"  {'':20}  {definition.short_description[:90]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
