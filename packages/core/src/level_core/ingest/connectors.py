"""Source connectors that produce Signals for the ingest pipeline.

Each connector yields zero or more :class:`Signal` objects. In local / demo
mode we use :class:`FixtureConnector` (deterministic sample data). In cloud
mode, live Google Calendar connectors use the user's OAuth token.
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

    Used for local smoke tests and the hackathon demo narrative so judges
    can reproduce the Memory Bank without granting Calendar scopes.
    """

    source: SignalSource
    signals: Sequence[Signal]

    async def fetch(self, *, user_id: str) -> AsyncIterator[Signal]:
        for signal in self.signals:
            if signal.user_id == user_id and signal.source is self.source:
                yield signal


def demo_caregiver_signals(user_id: str = "demo-parent") -> list[Signal]:
    """The demo narrative: a busy single parent juggling school + work.

    These become the Memory Bank facts the Challenger cites during the
    4-minute demo video.
    """
    now = datetime.now(tz=timezone.utc)
    return [
        Signal(
            user_id=user_id,
            source=SignalSource.GCAL,
            external_id="gcal-picture-day",
            occurred_at=now + timedelta(days=3),
            text=(
                "Calendar: Picture Day — Lincoln Elementary, Friday 8:15am. "
                "Note from me: need to figure out Maya's hair. Can't be late to "
                "the 9am standup."
            ),
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


@dataclass(slots=True)
class GoogleCalendarConnector:
    """Live Google Calendar connector (cloud mode).

    Expects a refreshable OAuth token available via ADC / Secret Manager.
    Until OAuth wiring is complete, ``fetch`` yields nothing and logs —
    the fixture path covers the demo.
    """

    source: SignalSource = SignalSource.GCAL
    calendar_id: str = "primary"

    async def fetch(self, *, user_id: str) -> AsyncIterator[Signal]:
        # Live pull lands when OAuth client credentials are configured.
        _ = (user_id, self.calendar_id)
        return
        if False:  # pragma: no cover — keeps this an async generator
            yield Signal(
                user_id=user_id,
                source=self.source,
                external_id="unused",
                text="",
            )


@dataclass(slots=True)
class ChatExportConnector:
    """Reads dropped ChatGPT/Claude/Gemini export files from a GCS prefix."""

    source: SignalSource = SignalSource.CHAT_EXPORT
    gcs_prefix: str = ""

    async def fetch(self, *, user_id: str) -> AsyncIterator[Signal]:
        _ = (user_id, self.gcs_prefix)
        return
        if False:  # pragma: no cover
            yield Signal(
                user_id=user_id, source=self.source, external_id="unused", text=""
            )


@dataclass(slots=True)
class VoiceMemoConnector:
    """Reads uploaded voice memos from GCS (expects pre-transcribed text)."""

    source: SignalSource = SignalSource.VOICE_MEMO
    gcs_prefix: str = ""

    async def fetch(self, *, user_id: str) -> AsyncIterator[Signal]:
        _ = (user_id, self.gcs_prefix)
        return
        if False:  # pragma: no cover
            yield Signal(
                user_id=user_id, source=self.source, external_id="unused", text=""
            )


__all__ = [
    "ChatExportConnector",
    "FixtureConnector",
    "GoogleCalendarConnector",
    "SignalConnector",
    "VoiceMemoConnector",
    "demo_caregiver_signals",
]
