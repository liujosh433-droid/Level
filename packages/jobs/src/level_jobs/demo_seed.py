"""Load Alpha/Beta demo family so judges see a populated UI immediately.

Ground rule kept from v1: no real people names in seed data.
Run with `make demo-seed`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from level_core.observability import get_logger
from level_core.schemas import (
    ActivityType,
    CachedEvent,
    CareRelation,
    Contact,
    ContactKind,
    EventTime,
    HourBand,
    UsualStatus,
    Weekday,
)
from level_core.storage.care_store import (
    add_priority,
    add_reminder,
    new_id,
    propose_person,
    propose_usual,
    set_person_status,
    set_usual_status,
)
from level_core.storage.factory import get_store

logger = get_logger("demo_seed")

DEMO_USER = "u_demo_alpha"


async def main() -> None:
    store = get_store(DEMO_USER)
    await store.profile.write({"user_id": DEMO_USER, "email": "demo@level.local", "tz": "America/Los_Angeles"})

    self_p = await propose_person(
        store, display_name="You", relation=CareRelation.SELF, is_self=True
    )
    await set_person_status(store, self_p.person_id, "kept")

    alpha = await propose_person(store, display_name="Alpha", relation=CareRelation.CHILD)
    beta = await propose_person(store, display_name="Beta", relation=CareRelation.CHILD)
    elder = await propose_person(store, display_name="Elder One", relation=CareRelation.ELDER)
    await set_person_status(store, alpha.person_id, "kept")
    await set_person_status(store, beta.person_id, "kept")
    await set_person_status(store, elder.person_id, "kept")

    for weekday in (Weekday.MON, Weekday.WED, Weekday.FRI):
        u = await propose_usual(
            store,
            person_id=alpha.person_id,
            weekday=weekday,
            hour_band=HourBand.AFTERNOON,
            activity_type=ActivityType.SCHOOL_PICKUP,
            display_summary="Alpha school pickup",
            source_event_uids=[],
            confidence=0.9,
        )
        await set_usual_status(store, u.usual_id, UsualStatus.KEPT)

    u = await propose_usual(
        store,
        person_id=beta.person_id,
        weekday=Weekday.TUE,
        hour_band=HourBand.EVENING,
        activity_type=ActivityType.SPORTS_SOCCER,
        display_summary="Beta soccer practice",
        source_event_uids=[],
        confidence=0.85,
    )
    await set_usual_status(store, u.usual_id, UsualStatus.KEPT)

    await add_priority(
        store, text="Never miss elder's Wednesday therapy",
        weight=5, activity_types=[ActivityType.MEDICAL_THERAPY]
    )
    await add_priority(
        store, text="Keep afternoons open for kids", weight=4,
        activity_types=[ActivityType.SCHOOL_PICKUP, ActivityType.FAMILY],
    )

    await add_reminder(
        store,
        text="Bring soccer shoes",
        person_id=beta.person_id,
        activity_type=ActivityType.SPORTS_SOCCER,
    )

    await store.contacts.upsert(
        Contact(
            contact_id=new_id("con"),
            person_id=alpha.person_id,
            kind=ContactKind.TEACHER,
            name="Ms. Rivera",
            email="teacher@example-school.org",
            notes="Alpha's homeroom teacher",
        )
    )
    await store.contacts.upsert(
        Contact(
            contact_id=new_id("con"),
            person_id=elder.person_id,
            kind=ContactKind.DOCTOR,
            name="Dr. Chen",
            email="drchen@example-clinic.org",
        )
    )

    tz_now = datetime.now(UTC)
    for i in range(-14, 15):
        day = tz_now + timedelta(days=i)
        if day.weekday() in (0, 2, 4):
            await store.agenda.upsert(
                CachedEvent(
                    event_id=f"seed-alpha-pickup-{i}",
                    calendar_id="primary",
                    summary="Alpha school pickup",
                    time=EventTime(
                        start=day.replace(hour=15, minute=0),
                        end=day.replace(hour=16, minute=0),
                        tz="America/Los_Angeles",
                    ),
                    activity_type=ActivityType.SCHOOL_PICKUP,
                    matched_person_ids=[alpha.person_id],
                    classified_at=datetime.utcnow(),
                    origin="google",
                )
            )
        if day.weekday() == 1:
            await store.agenda.upsert(
                CachedEvent(
                    event_id=f"seed-beta-soccer-{i}",
                    calendar_id="primary",
                    summary="Beta soccer practice",
                    time=EventTime(
                        start=day.replace(hour=17, minute=30),
                        end=day.replace(hour=19, minute=0),
                        tz="America/Los_Angeles",
                    ),
                    activity_type=ActivityType.SPORTS_SOCCER,
                    matched_person_ids=[beta.person_id],
                    classified_at=datetime.utcnow(),
                    origin="google",
                )
            )
        if day.weekday() == 2:
            await store.agenda.upsert(
                CachedEvent(
                    event_id=f"seed-elder-therapy-{i}",
                    calendar_id="primary",
                    summary="Elder therapy appointment",
                    time=EventTime(
                        start=day.replace(hour=10, minute=0),
                        end=day.replace(hour=11, minute=0),
                        tz="America/Los_Angeles",
                    ),
                    activity_type=ActivityType.MEDICAL_THERAPY,
                    matched_person_ids=[elder.person_id],
                    classified_at=datetime.utcnow(),
                    origin="google",
                )
            )

    logger.info("demo_seed.done", user_id=DEMO_USER)


if __name__ == "__main__":
    asyncio.run(main())
