"""Source connectors that produce Signals for the ingest pipeline.

Live calendar enters via the API (OAuth sync).
``FixtureConnector`` + ``demo_caregiver_signals`` exist only for opt-in
pitch scripts (``LEVEL_INGEST_FIXTURES=1`` / ``make demo-judge``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from level_core.schemas.signal import Signal, SignalSource


class SignalConnector(Protocol):
    """Anything that can produce Signals for a user."""

    source: SignalSource

    async def fetch(self, *, user_id: str) -> AsyncIterator[Signal]:
        """Yield new signals for ``user_id`` since the last successful pull."""
        ...


@dataclass(slots=True)
class FixtureConnector:
    """Deterministic demo connector — no network, no OAuth.

    Used only when jobs set ``LEVEL_INGEST_FIXTURES=1``.
    """

    source: SignalSource
    signals: Sequence[Signal]

    async def fetch(self, *, user_id: str) -> AsyncIterator[Signal]:
        for signal in self.signals:
            if signal.user_id == user_id and signal.source is self.source:
                yield signal


def demo_caregiver_signals(user_id: str = "demo-parent") -> list[Signal]:
    """Opt-in pitch narrative (Maya / school / work). Not used in normal runtime."""
    now = datetime.now(tz=timezone.utc)
    return [
        Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id="gcal-picture-day",
            occurred_at=now + timedelta(days=3),
            text=(
                "Calendar: Picture Day — Lincoln Elementary — Maya, Friday 8:15am. "
                "Note from me: need to figure out Maya's hair. Can't be late to "
                "the 9am standup."
            ),
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id="gcal-thu-pickup",
            occurred_at=now + timedelta(days=((3 - now.weekday()) % 7) or 7),
            text=(
                "Calendar: School pickup — Maya, Thursday 3:15pm. "
                "Protected window — leave work by 2:45."
            ),
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id="gcal-soccer",
            occurred_at=now
            + timedelta(days=((3 - now.weekday()) % 7) or 7)
            + timedelta(hours=1),
            text="Calendar: Soccer practice — Maya, Thursday 4:30pm at Lincoln field.",
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id="gcal-mom-visit",
            occurred_at=now + timedelta(days=5),
            text="Calendar: Visit Mom — clinic follow-up, Tuesday 11:00am.",
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id="gcal-standup",
            occurred_at=now + timedelta(days=1),
            text="Calendar: Work standup — team sync, weekday 9:00am.",
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id="gcal-promo-deadline",
            occurred_at=now + timedelta(days=10),
            text=(
                "Calendar: Promotion packet due. All-day. I told my manager I'd "
                "decide by end of month whether to apply."
            ),
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.MANUAL,
            external_id="manual-values-note",
            occurred_at=now - timedelta(days=14),
            text=(
                "Note titled 'what matters': I value being present for Maya during "
                "the school year. I said last year that switching schools mid-year "
                "was too disruptive and I wouldn't do it again."
            ),
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.VOICE_MEMO,
            external_id="voice-tuesday-vent",
            occurred_at=now - timedelta(days=2),
            text=(
                "Voice memo transcript: I'm so tired. Everyone says take the "
                "promotion. But Mondays are already the hardest — after-school "
                "pickup, dinner, homework. I don't know if I can add more."
            ),
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.CHAT_EXPORT,
            external_id="chat-claude-school",
            occurred_at=now - timedelta(days=5),
            text=(
                "Prior AI chat export: User asked Claude 'should I switch Maya to "
                "the dual-language school?' Claude said it sounded exciting. User "
                "replied 'yeah maybe, her friend is going.' No mention of last "
                "year's disruption."
            ),
        ),
        Signal(
            user_id=user_id,
            source=SignalSource.MANUAL,
            external_id="manual-co-parent",
            occurred_at=now - timedelta(days=21),
            text=(
                "Manual note: Co-parent works nights Tue/Thu. Those evenings I'm "
                "solo for bedtime. I committed to cooking Sunday dinner every week."
            ),
        ),
    ]


__all__ = [
    "FixtureConnector",
    "SignalConnector",
    "demo_caregiver_signals",
]
