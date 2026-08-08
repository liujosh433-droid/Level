"""Tests for the bias taxonomy loader + BiasAggregator EMA logic."""

from __future__ import annotations

from datetime import datetime, timezone

from level_core.bias.profile import (
    STREAK_THRESHOLD,
    BiasAggregator,
    build_empty_profile,
)
from level_core.bias.taxonomy import BIAS_TAXONOMY
from level_core.schemas.bias import BiasCategory, BiasEvent


def _event(category: BiasCategory, intensity: float, *, user_id: str = "u1") -> BiasEvent:
    return BiasEvent(
        user_id=user_id,
        decision_id="d1",
        turn_id="t1",
        category=category,
        intensity=intensity,
        evidence="quote",
    )


class TestTaxonomy:
    def test_taxonomy_defines_every_category(self) -> None:
        for category in BiasCategory:
            assert category in BIAS_TAXONOMY, f"missing definition for {category.value}"

    def test_definition_shape(self) -> None:
        definition = BIAS_TAXONOMY[BiasCategory.SUNK_COST]
        assert definition.name.lower().startswith("sunk")
        assert definition.detection_hint
        assert definition.challenger_prompt


class TestBiasAggregator:
    def test_empty_events_returns_unchanged_profile(self) -> None:
        aggregator = BiasAggregator()
        profile = build_empty_profile("u1")
        update = aggregator.update(profile=profile, events=[])
        assert update.session_seen is False
        assert update.profile.session_count == 0

    def test_single_event_updates_ema(self) -> None:
        aggregator = BiasAggregator(alpha=0.5)
        profile = build_empty_profile("u1")
        update = aggregator.update(
            profile=profile,
            events=[_event(BiasCategory.SUNK_COST, intensity=0.8)],
        )
        assert update.session_seen is True
        score = next(s for s in update.profile.scores if s.category is BiasCategory.SUNK_COST)
        assert score.ema > 0.35
        assert score.total_observations == 1
        assert score.last_seen_at is not None
        assert score.last_seen_at.tzinfo is timezone.utc
        assert BiasCategory.SUNK_COST in update.increased

    def test_streak_accumulates_across_sessions(self) -> None:
        aggregator = BiasAggregator()
        profile = build_empty_profile("u1")
        assert STREAK_THRESHOLD > 0
        for _ in range(3):
            update = aggregator.update(
                profile=profile,
                events=[_event(BiasCategory.OPTIMISM, intensity=0.9)],
            )
            profile = update.profile
        score = next(s for s in profile.scores if s.category is BiasCategory.OPTIMISM)
        assert score.streak >= 2

    def test_creates_full_profile_when_none_provided(self) -> None:
        aggregator = BiasAggregator()
        update = aggregator.update(
            profile=None,
            events=[_event(BiasCategory.CONFIRMATION, intensity=0.3)],
        )
        assert update.profile.user_id == "u1"
        assert len(update.profile.scores) == len(BiasCategory)
        assert update.profile.last_updated_at is not None
        assert isinstance(update.profile.last_updated_at, datetime)
