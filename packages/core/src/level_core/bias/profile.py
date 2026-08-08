"""Aggregation logic for turning per-turn bias events into a persistent BiasProfile.

Called by the learning loop (post-session and nightly) to update the user's
persistent Bias Profile in Firestore.

The aggregation uses an exponential moving average (EMA) per bias category
so recent events count more than old ones without discarding history. Also
tracks a streak count — how many consecutive sessions a bias has appeared
above a threshold — which the Challenger surfaces as: "this is the third
time this month you've weighed things this way — worth naming."
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from level_core.schemas.bias import (
    BiasCategory,
    BiasEvent,
    BiasProfile,
    BiasScore,
)

# Smoothing constant for the EMA. Higher = new events dominate faster.
EMA_ALPHA: float = 0.35

# Intensity threshold for "the bias was really present" (for streak counting).
STREAK_THRESHOLD: float = 0.4


@dataclass(slots=True)
class ProfileUpdate:
    """Summary of how a BiasProfile changed after processing a batch of events."""

    profile: BiasProfile
    increased: list[BiasCategory]
    decreased: list[BiasCategory]
    new_streaks: list[BiasCategory]
    session_seen: bool


def build_empty_profile(user_id: str) -> BiasProfile:
    return BiasProfile(
        user_id=user_id,
        scores=[BiasScore(category=cat) for cat in BiasCategory],
        session_count=0,
    )


class BiasAggregator:
    """Applies per-turn bias events to a persistent BiasProfile.

    Callers pass in the current profile (or None to start fresh) plus a
    batch of new events (typically all events from one session). The
    returned :class:`ProfileUpdate` describes what changed so the caller
    can log an audit event with meaningful deltas.
    """

    def __init__(self, alpha: float = EMA_ALPHA, streak_threshold: float = STREAK_THRESHOLD) -> None:
        self._alpha = alpha
        self._streak_threshold = streak_threshold

    def update(
        self,
        *,
        profile: BiasProfile | None,
        events: Iterable[BiasEvent],
    ) -> ProfileUpdate:
        events_list = list(events)
        if not events_list:
            return ProfileUpdate(
                profile=profile or build_empty_profile("unknown"),
                increased=[],
                decreased=[],
                new_streaks=[],
                session_seen=False,
            )

        user_id = events_list[0].user_id
        current = profile if profile is not None else build_empty_profile(user_id)

        # Group events by category → compute per-session mean intensity.
        session_intensity: dict[BiasCategory, list[float]] = {}
        for event in events_list:
            session_intensity.setdefault(event.category, []).append(event.intensity)

        # Update each score.
        score_lookup = {s.category: s for s in current.scores}
        for cat in BiasCategory:
            if cat not in score_lookup:
                score_lookup[cat] = BiasScore(category=cat)

        increased: list[BiasCategory] = []
        decreased: list[BiasCategory] = []
        new_streaks: list[BiasCategory] = []

        now = datetime.now(tz=timezone.utc)

        for cat in BiasCategory:
            score = score_lookup[cat]
            observed = session_intensity.get(cat, [])
            if observed:
                session_mean = sum(observed) / len(observed)
                prior = score.ema
                score.ema = round((self._alpha * session_mean) + ((1 - self._alpha) * prior), 4)
                score.total_observations += len(observed)
                score.last_seen_at = now

                if session_mean >= self._streak_threshold:
                    score.streak += 1
                    if score.streak >= 2:
                        new_streaks.append(cat)
                else:
                    score.streak = 0

                if score.ema > prior + 0.01:
                    increased.append(cat)
                elif score.ema < prior - 0.01:
                    decreased.append(cat)
            else:
                # No observation this session — slow decay of streak.
                if score.streak > 0:
                    score.streak = max(0, score.streak - 0)  # explicit no-op; decay only if we want it

        updated = BiasProfile(
            user_id=current.user_id,
            scores=[score_lookup[c] for c in BiasCategory],
            session_count=current.session_count + 1,
            last_updated_at=now,
            created_at=current.created_at,
            written_by=current.written_by,
            trace_id=current.trace_id,
        )

        return ProfileUpdate(
            profile=updated,
            increased=increased,
            decreased=decreased,
            new_streaks=new_streaks,
            session_seen=True,
        )


def apply_events_to_profile(
    *, profile: BiasProfile | None, events: Iterable[BiasEvent]
) -> ProfileUpdate:
    """Convenience wrapper — instantiate a default aggregator and apply."""
    return BiasAggregator().update(profile=profile, events=events)


__all__ = [
    "BiasAggregator",
    "EMA_ALPHA",
    "ProfileUpdate",
    "STREAK_THRESHOLD",
    "apply_events_to_profile",
    "build_empty_profile",
]
