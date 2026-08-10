"""Ingested signals + the structured facts we extract from them.

A ``Signal`` is the raw ingested unit (a calendar event, an email, a voice
memo, a document). A ``Fact`` is a Pydantic-typed structured extraction
that the ``IngestNormalizer`` agent produces from one or more Signals.

Facts — not raw signals — are what the Retriever surfaces to the Challenger.
This keeps the challenger's context tight and prevents PII from raw email
bodies from ever reaching the reasoning model.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from level_core.schemas.base import TraceableModel, _new_id


class SignalSource(str, Enum):
    """Where a signal came from."""

    GCAL = "gcal"
    GMAIL = "gmail"
    VOICE_MEMO = "voice_memo"
    CHAT_EXPORT = "chat_export"
    PHOTO = "photo"
    MANUAL = "manual"


class FactType(str, Enum):
    """Taxonomy of structured extractions the Normalizer can produce.

    Kept intentionally small — a good taxonomy is easier for the Retriever
    to reason over than a sprawling one.
    """

    VALUE_STATEMENT = "value_statement"        # "I care about being present for my kids"
    COMMITMENT = "commitment"                  # "I'll cook Sunday dinner every week"
    CONSTRAINT = "constraint"                  # "I can't work past 6pm on Mondays"
    PREFERENCE = "preference"                  # "I prefer async communication"
    CONCERN = "concern"                        # "I'm worried about her grades"
    EVENT = "event"                            # "picture day is Friday"
    DECISION_HISTORY = "decision_history"      # "we tried X last year and it didn't work"
    RELATIONSHIP = "relationship"              # "co-parent works nights"


class Signal(TraceableModel):
    """A raw ingested piece of information from a source.

    We keep the raw content only when small (calendar events, chat messages);
    large blobs (voice recordings, photos) go to Cloud Storage and we store
    only a ``storage_uri`` here.
    """

    signal_id: str = Field(default_factory=_new_id)
    user_id: str

    source: SignalSource
    external_id: str = Field(
        description="Stable id from the source (e.g. gcal eventId) for idempotent ingestion."
    )

    occurred_at: datetime | None = Field(
        default=None,
        description="When the underlying event/document is dated (not when we ingested it).",
    )

    text: str | None = Field(
        default=None,
        description="Inline text content (for small signals). Large content is in storage_uri.",
    )
    storage_uri: str | None = Field(
        default=None,
        description="gs:// URI for large binary content (audio, images, exported chats).",
    )

    mime_type: str | None = None
    scrubbed_pii: bool = Field(
        default=False,
        description="True if this signal has passed inbound Model Armor and had PII redacted.",
    )
    superseded_by: str | None = Field(
        default=None,
        description="If this signal was superseded by a newer version, the newer signal_id.",
    )


class Fact(TraceableModel):
    """A structured extraction from one or more Signals.

    Every Fact carries a ``salience`` in [0, 1] used by the Retriever to
    weight results, and an ``embedding_hash`` used to detect when we should
    re-embed after the Fact is edited.
    """

    fact_id: str = Field(default_factory=_new_id)
    user_id: str

    type: FactType
    statement: str = Field(
        description="Concise first-person natural-language rendering of the fact."
    )
    source_signal_ids: list[str] = Field(
        default_factory=list,
        description="Signals that contributed to this fact. Enables traceback for citations.",
    )

    salience: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "How central this fact is to the user's decision context. "
            "Higher = more likely to surface as evidence."
        ),
    )
    confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Normalizer's confidence that this extraction is faithful to the signal.",
    )

    valid_from: datetime | None = None
    valid_until: datetime | None = None

    embedding_hash: str | None = Field(
        default=None,
        description="SHA of the statement text at last embed time. Rebuild embedding when it changes.",
    )
    superseded_by: str | None = None


__all__ = ["Fact", "FactType", "Signal", "SignalSource"]
