"""Deterministic (no-LLM) generation of "Level noticed while you slept"
proactive cards.

Previously lived inside ``packages/jobs/src/level_jobs/nightly.py`` as
a private helper. Extracted here so the demo seeder can also call it
without a cross-package import (``level_core`` must not depend on
``level_jobs``). Both callers share the same schema for
``profile["proactive_cards"]``.

Deterministic + cheap - no LLM, no external network. Safe to invoke
inline from an HTTP request handler when needed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from level_core.calendar.usuals import (
    current_week_bounds,
    missing_usuals_this_week,
    typical_time_range,
)
from level_core.observability import get_logger
from level_core.schemas.agenda import CachedEvent
from level_core.schemas.care import CarePerson
from level_core.storage.base import UserStore

logger = get_logger(__name__)

# Profile KV key + card cap. Kept as module constants so callers
# (nightly job, demo seeder, admin snapshot) can reference the same
# names without stringly-typed drift.
PROACTIVE_CARDS_KEY = "proactive_cards"
MAX_PROACTIVE_CARDS = 5


def _format_time_range(start: str | None, end: str | None) -> str | None:
    """Render "4:30pm-5:30pm" as a compact parenthetical for card bodies.

    Returns ``None`` when either endpoint is unknown so the caller
    can fall through to the un-timed template. Uses ``\u2013`` (en
    dash) between the endpoints — the caregivers judging the demo
    read this on a phone; en-dash is the typographic convention for
    time ranges and reads cleaner than a hyphen.
    """
    if not start or not end:
        return None
    return f"{start}\u2013{end}"


def _strip_owner_prefix(activity_label: str, display_name: str) -> str:
    """Strip a leading owner-name prefix from a title hint.

    ``title_hint`` is majority-voted from event summaries and often
    already carries the person's name ("Helen weekly grocery drop",
    "Nova ballet"). Stitching that into the possessive template
    produces "Helen's Helen weekly grocery drop is missing" - the
    duplicated name reads like a typo.

    Matches ``<name> `` and ``<name>'s `` (case-insensitive) at the
    start of the label. Returns the original label unchanged when
    the prefix isn't there or when stripping it would leave the
    label empty (defensive: we'd rather duplicate than say
    "'s nothing is missing").
    """
    name = display_name.strip()
    if not name:
        return activity_label
    # ``\b`` matches a name that ends at a word boundary so we don't
    # eat a prefix out of an unrelated word ("Helena" wouldn't match
    # "Helen"). ``'s`` is optional so both "Helen weekly grocery
    # drop" and "Helen's grocery" are covered.
    pattern = re.compile(rf"^{re.escape(name)}(?:'s)?\s+", re.IGNORECASE)
    stripped = pattern.sub("", activity_label, count=1).strip()
    return stripped or activity_label


async def regenerate_proactive_cards(
    store: UserStore,
    *,
    tz: ZoneInfo,
    events: list[CachedEvent],
    people: list[CarePerson] | None = None,
) -> int:
    """Write this week's missing-usual cards to ``profile["proactive_cards"]``.

    Returns the number of cards written. Zero means "no gaps found";
    any previously-stored cards for a stale week are cleaned up in
    that branch so /today stops rendering them.

    Called from two places:
    - The nightly Cloud Run Job runs against every real user once a
      night.
    - The demo seeder invokes it inline right after seeding usuals +
      agenda so a judge who just clicked "Try demo" lands on a /today
      with the "Level noticed while you slept" section already
      populated - no need to wait for the nightly job to run.

    ``people`` is optional context: callers that already loaded the
    roster (the demo seeder does, right after ``_seed_people``) can
    pass it in to skip one Firestore ``people.list()`` round trip.
    When None we fall back to reading it here.
    """
    today = datetime.now(tz).date()
    week_start, week_end = current_week_bounds(today)
    usuals = await store.usuals.list()
    if people is None:
        people = await store.people.list()
    events_by_id = {e.event_id: e for e in events}

    week_events = [
        e
        for e in events
        if week_start <= e.time.start.astimezone(tz).date() < week_end
    ]
    missing = missing_usuals_this_week(
        usuals=usuals,
        week_events=week_events,
        as_of_date=today,
        events_by_id=events_by_id,
        people=people,
        tz=tz,
    )

    if not missing:
        profile = dict(await store.profile.read() or {})
        if profile.pop(PROACTIVE_CARDS_KEY, None) is not None:
            await store.profile.write(profile)
        return 0

    # Reuse the roster we already have instead of re-listing - this
    # is what shaves a second full people.list() off every call.
    people_by_id = {p.person_id: p for p in people}
    # ``typical_time_range`` needs the raw usuals to trace back through
    # ``source_event_uids``. Cheap ordinary local list, one shared
    # across every card in this run.
    usuals = await store.usuals.list()
    cards: list[dict[str, Any]] = []
    for g in missing[:MAX_PROACTIVE_CARDS]:
        primary = people_by_id.get(g.person_id)
        display_name = primary.display_name if primary else "someone"
        # Calendar date in the user's TZ (week_start came from
        # datetime.now(tz).date()). Name the weekday from that date
        # so it matches /today's greeting, not the container clock.
        day_date = week_start + timedelta(days=int(g.weekday))
        day = day_date.isoformat()
        day_name = day_date.strftime("%A")
        # Prefer the concrete title ("Grocery run", "Nova ballet") over
        # the coarse category label ("Personal", "Sports"). The category
        # on its own is too abstract to be a useful nudge - "Josh's
        # personal is missing this week" reads like a therapist joke.
        # Falls back to category when the underlying usual didn't carry
        # a title (badly seeded data or LLM-less local dev).
        activity_label = (g.title_hint or g.category.label).strip()
        # Strip the owner's name from the front of the label - the
        # possessive template below already carries it. Otherwise a
        # title_hint like "Helen weekly grocery drop" produces
        # "Helen's helen weekly grocery drop is missing".
        activity_label = _strip_owner_prefix(activity_label, display_name)
        # Median start/end from the source usuals' historical events.
        # Falls through to a time-less body when there are no timed
        # source events (badly-seeded data or all-day usuals).
        typical_start, typical_end = typical_time_range(g, usuals, events_by_id, tz)
        time_range = _format_time_range(typical_start, typical_end)
        time_clause = f" (usually {time_range})" if time_range else ""
        if primary and primary.is_self:
            # Own-usual phrasing: "Your grocery run is missing" reads
            # better than "Josh's grocery run is missing" when it's
            # the caregiver themselves.
            body_text = (
                f"Your {activity_label.lower()} is missing this {day_name}"
                f"{time_clause}. Want me to put it back?"
            )
        else:
            body_text = (
                f"{display_name}'s {activity_label.lower()} is missing this {day_name}"
                f"{time_clause}. Want me to put it back?"
            )
        # ``group_id`` mirrors the format ``_decorate_missing_group``
        # emits for missing_usuals_week rows (``{weekday}:{category}``).
        # The frontend uses it to (a) hide the corresponding missing-
        # week row so the two sections don't duplicate the same nudge,
        # and (b) call ``/missing-week/put-back`` with the same id so
        # one click resolves both surfaces.
        group_id = f"{int(g.weekday)}:{g.category.value}"
        cards.append(
            {
                "card_id": f"missing:{group_id}",
                "group_id": group_id,
                "kind": "missing_usual",
                "week_start": week_start.isoformat(),
                "day": day,
                "weekday": int(g.weekday),
                "category": g.category.value,
                "category_label": g.category.label,
                "title_hint": g.title_hint,
                "person_id": g.person_id,
                "person_name": display_name,
                "text": body_text,
                # Structured time endpoints so the frontend can render
                # them in its own layout (chip, meta line, tooltip)
                # without re-parsing the free-form ``text``.
                "typical_start": typical_start,
                "typical_end": typical_end,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )

    # ``update_fields`` merges the single key into the existing
    # profile without a read-then-write cycle - matters on Firestore
    # where each round trip is ~50-150ms of pure latency (the demo
    # seeder calls this inline).
    await store.profile.update_fields(
        **{
            PROACTIVE_CARDS_KEY: {
                "week_start": week_start.isoformat(),
                "generated_at": datetime.now(UTC).isoformat(),
                "cards": cards,
            }
        }
    )
    logger.info(
        "proactive.cards_written",
        user_id=store.user_id,
        count=len(cards),
        week_start=week_start.isoformat(),
    )
    return len(cards)
