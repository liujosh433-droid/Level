"""Base Pydantic models shared across the schema layer.

Every domain object should ultimately inherit from :class:`LevelModel` so we
get consistent config (strict, from-attributes, immutable serialization) for
free.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _new_id() -> str:
    """Short URL-safe unique id.

    Firestore document ids can be up to 1500 bytes; we cap at 26 chars for
    readability. UUID4 base32 is collision-safe for our scale.
    """
    return uuid.uuid4().hex[:26]


class LevelModel(BaseModel):
    """Base class for every domain model.

    - ``strict`` prevents silent type coercion (e.g. "42" → 42).
    - ``validate_assignment`` catches bugs where we mutate a field with the
      wrong type after construction.
    - ``extra="forbid"`` catches typos in kwargs immediately.
    - ``populate_by_name`` lets Firestore round-trip snake_case field names.
    """

    model_config = ConfigDict(
        strict=True,
        validate_assignment=True,
        extra="forbid",
        populate_by_name=True,
        frozen=False,
    )


class TimestampedModel(LevelModel):
    """Adds ``created_at`` and ``updated_at`` in UTC.

    Callers must remember to bump ``updated_at`` on mutation (or use
    :meth:`touch`).
    """

    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    def touch(self) -> None:
        """Update ``updated_at`` to now."""
        self.updated_at = _now_utc()


class TraceableModel(TimestampedModel):
    """A timestamped model that also carries provenance metadata.

    Used for domain objects that record *who* wrote them and under what
    OpenTelemetry trace so we can jump from a Firestore doc to a full
    reasoning chain in Cloud Trace.
    """

    written_by: str | None = Field(
        default=None,
        description="The registered agent version that produced this doc, e.g. 'challenger@v3'.",
    )
    trace_id: str | None = Field(
        default=None,
        description="OpenTelemetry trace id (hex, 32 chars) for the reasoning chain that produced this doc.",
    )


__all__ = ["LevelModel", "TimestampedModel", "TraceableModel", "_new_id", "_now_utc"]
